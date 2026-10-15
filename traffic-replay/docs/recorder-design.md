# 录制端设计说明（recorder）

覆盖 `traffic-replay/recorder/**`：可插拔流量源 → 采样 → 脱敏 → 入库。

## 1. 为什么流量源必须可插拔，且回放端零感知

回放端与比对端只 `import replay.model`。录制端两个来源的**唯一出口**都是
`TrafficSource.iter_exchanges() -> Iterator[RecordedExchange]`：

| 实现 | 构造 | 流量从哪来 |
|---|---|---|
| `LocalSynthetic(fixtures_dir)` | 纯本地，无依赖 | `fixtures/**/*.json` |
| `ProxyTap(host=..., port=...)` | 需 mitmproxy | mitmproxy 旁路抓包（`add_flow()` / `run()`） |

`ProxyTap` 的 `import mitmproxy` 写在 `_require_mitmproxy()` 与 `run()` 的**函数体内**。
本机未安装 mitmproxy；模块顶层一旦 import 它，`import recorder` 就会炸，
本地（无 mitmproxy）的整条录制→回放链路都会起不来。构造时缺依赖直接抛出：

```
未安装 mitmproxy，ProxyTap 旁路接入点不可用；需 pip install mitmproxy（原始错误: No module named 'mitmproxy'）
```

`flow_to_exchange(flow)` 用**鸭子类型**读 mitmproxy 的 `HTTPFlow`（只要
`flow.request.url/method/headers/content`、`flow.response.status_code/...` 就够），
所以测试可以用一个 20 行的假对象覆盖整条映射逻辑，不需要真的装 mitmproxy。

## 2. 采样为什么不能用 Python 的 `hash()`

确定性是硬要求：同一批输入 + 同一种子必须得到同一批输出。若采样在两次录制间抖动，
**同一条基线两次回放结果不同**的锅会扣到「被测服务有回归」头上，这是最贵的误报。

`Sampler` 因此用 `blake2b(f"{seed}:{exchange_id}", digest_size=8)` 取单位区间随机数，
而不是 `hash()` —— 后者带进程级随机化（`PYTHONHASHSEED`），跨进程不稳定。
三种策略可叠加：`sample_rate`（比例）、`path_weights`（路径权重，支持 `*` 前缀、
最长模式优先、权重 `0` = 一律不录）、`require_tags`/`forbid_tags`（标签过滤）、
`max_per_path`（逐路径限流，压制高频接口）。数组/字典一律按 sorted 键遍历，
避免「依赖输入顺序」造成的不确定。

## 3. 脱敏的两条硬要求

1. **同原值 → 同假值**。映射源是 `hmac_sha256(seed + 类别, 原值)`，
   只由 seed 与原值决定：跨 `RecordedExchange`、跨 `Scrubber` 实例、跨进程都一致。
   若同一用户在两次录制里变成两个不同账号，基线会全部错配。
2. **保留格式与长度**，脱敏不得破坏可回放性：

| 类别 | 保真约束 |
|---|---|
| 手机号 | 仍匹配 `1[3-9]\d{9}`，11 位；原串里的 `-`/空格分隔位置原样回贴 |
| 身份证 | 仍 18 位，且第 18 位满足 GB 11643 校验（加权和 mod 11 → `10X98765432`） |
| 银行卡 | 位数不变，且整串通过 Luhn |
| 邮箱 | 仍像邮箱；**域名保留**（域名不是个人数据，改掉会让回放命中错误的邮件域） |
| JWT | 仍是三段 base64url；header 段重新编码成 `{"alg":<原算法>,"typ":"JWT"}`，payload/sig 段长度不变 |
| token | `sk-` 前缀与总长度保留 |
| 地址 | 中文长度不变，从固定池里取字 |

`ScrubRule(name, pattern, rewriter=None, kind=None)` 支持可配置扩展；所有规则按注册顺序
拼成**一个** `re` 组合模式（命名组），所以一次 `sub` 完成、替换出的假值不会再被后续规则二次匹配。
顺序即优先级：`jwt → email → id_card → bank_card → phone → token → phone_loose → address`。
18 位身份证必须排在银行卡之前，否则会被 `\d{4}(...){3,4}` 吞掉。

内置规则只动**值**，不动结构：`scrub_exchange` 保留 `exchange_id` / `recorded_at` /
`method` / `path` / `status` / `elapsed_ms` / `tags` / 非敏感字段，`replace()` 出新对象，
原对象不被改动（frozen dataclass）。

## 4. 依赖表（`Dependency`）为什么是回放的关键

脱敏之后请求里只剩假值，而真实环境里没有这些假用户。`Dependency(kind, ref, scrubbed)`
是「脱敏后仍能命中真实业务数据」的唯一线索：回放前按 `ref` 造数，让假值 `scrubbed`
在目标环境里真的存在，否则一整批请求都会变成 404/空数据，被判成假回归。

- `scrubber.dependencies` 按首次出现顺序返回全局去重表（`(kind, ref)` 去重）。
- `scrubber.scrub_with_deps(exchange)` 把**本交换实际改写到的**依赖挂到该交换的 `deps` 上，
  并合并该交换原有的 `deps`。注意不能用「按首次出现切片」实现：同一个手机号在第 2、3 条
  交换里再次出现时会被去重掉，依赖表里就查不到归属，造数会漏掉后面的请求。

## 5. 入库（`store.py`）

严格按 `FROZEN_INTERFACES.md` §2 的签名；纯 `sqlite3`，无新依赖，`check_same_thread=False`，
写入走单事务（`with self._conn`），schema 版本写 `PRAGMA user_version`，`migrate()` 幂等。

round-trip 的铁律：`load_baseline` 必须还原到与原对象**相等**的 `RecordedExchange`。三处口径：

- `body`（bytes）存 BLOB，直接进出，**不做字符串转换** —— 否则 protobuf/gzip 体会被二次编码。
- `body_json` 存 JSON 文本；`recorded_at` 存 ISO 8601 带时区，`fromisoformat` 原样还原偏移。
- headers / query / tags 用 `sort_keys=True` 落盘。字典插入序不属于数据语义
  （`make_exchange_id` 也只按 sorted 键算 ID），`{"a":1,"b":2} == {"b":2,"a":1}` 恒真；
  规范化存储让外部写入的库也能还原出相等的对象。query 的**值列表**顺序保留
  （多值参数顺序可能影响服务端行为），只对键排序。

`save_baseline` 同名覆盖：先删旧基线的 exchanges 与 baselines 行再插新的，返回新的
`uuid4().hex[:16]`。`load_baseline` 未知 id 抛 `KeyError`；`latest_report(None)` 取全局最新。

## 6. 覆盖场景（`fixtures/`）

`users.json`：正常 GET、带分页列表（`page/size/keyword`）、POST 创建（201 + Location）、
审计日志列表（带 `noisy` 标签，用于验证标签过滤）。
`orders.json`：秒杀下单写入、幂等重放（`Idempotency-Key`）、204 空响应体。
刻意包含会暴露真实问题的东西：手机号/身份证/邮箱/token/银行卡/中文地址、
响应里的时间戳与自增 ID（`seq`/`order_id`/`created_at`）、多值 query（`tag=a&tag=b`）。

## 7. 已知边界

- `address` 规则是启发式（中文 + 省市区 + 路街号），非标准地址不会命中；这是正则的能力上限。
- `token` 规则 `[A-Za-z0-9_-]{24,}` 会吞掉**任何** ≥24 字符的 base64/hex 串，包括不含敏感信息的
  长 ID。宁滥勿缺：漏脱敏的代价远大于多脱一个。
- `scrub_exchange` 在 `body_json` 非空时用紧凑分隔符重写 `body`（`separators=(",",":")`），
  因此 body 字节可能与「录制时的原始字节」不同；`exchange_id` 由 `replace` 保留、不重算，
  基线配对不受影响。
- `ProxyTap.run()` 的 DumpMaster 路径本机无法执行（mitmproxy 未安装），只有 `add_flow()` +
  假 flow 的路径被真正跑过。
