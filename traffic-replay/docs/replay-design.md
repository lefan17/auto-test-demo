# 回放与差异比对设计

回放端要做的事一句话概括：**把录制下来的基线流量在目标环境重放一遍，把响应差异压到人眼能看完的规模，并按优先级排好。**

链路是四段纯函数式流水线，每段可单独测：

```
baseline(exchanges) ──┐
                      ├─ diff_all ─→ classify_all ─→ build_risk ─→ ReplayReport
ExchangePlayer.run ───┘   (结构化 diff)   (降噪/四分类)   (接口级聚合)
```

对应文件：`replay/player.py`（重放）、`replay/differ.py`（比对）、`analyze/classify.py`（分类）、`analyze/risk.py`（排序）、`analyze/runner.py`（编排）。

---

## 1. 比对判定标准

### 1.1 三档差异级别

`DiffLevel` 只有三档来自 differ，第四档 `NOISE` **由 analyzer 赋予**——differ 永远不产出 `NOISE`。

| 级别 | 触发条件 | 权重 | 为什么不降级 |
| --- | --- | --- | --- |
| `STRUCTURE` | 对象键新增/删除；数组长度变化（按位比时尾部多出/缺失的元素） | 1000 | 字段没了就是契约破坏，跟值抖不是一回事 |
| `TYPE` | 同一路径上 `type_name` 不同（`1` vs `"1"`、`null` vs `{}`、JSON vs 非 JSON） | 100 | 类型变了下游解析就崩，比值变严重 |
| `VALUE` | 类型相同、值不同（含状态码、header 值、非 JSON body 长度） | 10 | 最轻，且大部分会被降噪吃掉 |
| `NOISE` | 仅由 analyzer 的噪声规则命中后 `replace()` 得到 | 1 | — |

`type_name` 刻意把 `bool` 与 `int` 分开（Python 里 `True == 1`），否则 `{"flag": true}` → `{"flag": 1}` 会被当成值相等漏掉。

### 1.2 JSONPath 风格路径

统一 `$.data.items[0].qty` 形式，由 `child_path` / `index_path` 生成：

- 合法标识符键：`$.a.b`
- 非法标识符键（带连字符、空格等）退化为括号形式：`$.headers["content-type"]`，用 `json.dumps` 转义避免歧义。
- 数组下标一律 `[i]`。

聚合时再走一层 `canonical_path()` 把 `$.items[3].qty` 归一成 `$.items[].qty`，这样不同下标的同名字段才会被算作同一个字段。

### 1.3 可忽略字段（`IgnoreRules`）

| 字段 | 语义 |
| --- | --- |
| `ignored_paths` | 精确命中即**整段不比**：命中的子树直接 `return`，其下所有差异**一条都不产出**（不是产出后降级）。例如 `("$.data.created_at",)` 会让 `$.data.created_at` 及其整个子树消失。 |
| `ignored_path_prefixes` | 前缀匹配，同一语义。给 `("$.data.trace",)` 能一次盖掉 `$.data.trace_id`、`$.data.trace.parent`。 |
| `unordered_paths` | 列出的数组路径**按集合比**，与顺序无关。 |
| `float_tolerance` | 绝对值容差：`abs(expected - actual) <= tolerance` 视为相同，**不产出** `FieldDiff`。注意 `0.0` 是「关闭容差」而不是「零容差」，因此判断写成 `if tolerance and ...`。 |
| `ignored_headers` | 默认 `("date", "content-length", "server", "set-cookie")`——这些头在回放环境里必然不同，比它们只会制造噪声。比较前 header 名统一小写。 |
| `volatile_value_fingerprints` | 不参与 differ，是给 analyzer 的降噪规则白名单（见 §2.2）。 |

`ignored_paths` 与「降噪」的关键区别：**忽略 = 差异不存在（条数不计），降噪 = 差异存在但归为噪声（计入 `diffs_before_noise`）**。前者改判定，后者改分类。这一点决定了「原始差异条数」这个指标的口径。

### 1.4 顺序无关数组

`unordered_paths` 命中时走 `_walk_unordered`：两侧各自按 `_sort_key()`（稳定序列化字符串）排序成规范序，先比规范序是否完全相等，相等就直接返回；不等才退回逐位比对报差异。

选「先比规范序、再逐位比」而不是做集合差值，是因为**逐位比能复用有序数组的全部递归逻辑**（内层对象仍按路径展开），实现成本和认知成本都更低；代价是元素发生重排又增删时，报出的路径位置可能不完全对应业务语义——但级别（STRUCTURE/TYPE/VALUE）仍是对的，评审时看的是字段路径而不是下标。

`is_unordered` 对子路径也生效：规则给 `$.data.items`，则 `$.data.items`、`$.data.items[0]`、`$.data.items[0].x` 都算命中。

### 1.5 非 JSON 响应

- 两侧都是 JSON（`body_json is not None`）→ 递归比 JSON 树。
- 一侧是 JSON、另一侧不是 → 报 `$.body` 的 **TYPE** 差异，`expected`/`actual` 用 `_describe_body()` 描述成 `"json"` / `"bytes(text/html)"`，而不是把整个 body 塞进报告。
- 两侧都不是 JSON（纯文本/二进制）→ **只比状态码与长度**，不比内容。长度差报 `$.body` 的 VALUE 差异，`expected`/`actual` 是长度数字。

不比非 JSON 内容是有意的：HTML 错误页、二进制附件的内容差异通常只是环境指纹（构建号、时间戳），逐字节 diff 既无信号又会让报告爆炸。要更严的判定应该走 JSON 契约，而不是靠文本 diff。

### 1.6 header 比对

header 差异统一记成 `$.headers["<小写名>"]`，级别一律 `VALUE`（含新增/缺失——header 增删不值得占用 STRUCTURE 的信号量，因为 header 集合本就比 body 松）。

### 1.7 分页差异

分页是这一层最容易误报的地方，处理原则：

- **不比分页参数本身**。录制的 `page`/`limit`/`cursor` 已在 `request.query` 里，回放时原样带上（`build_params` 摊平多值 query），因此分页行为由服务端决定，不靠测试端猜。
- **同一路径的多页响应会被正确聚合**。`normalize_path()` 只归一化「看着像 ID」的路径段（纯数字、UUID、长 hex），`/api/items/1001` 与 `/api/items/1002` 聚成 `GET /api/items/{id}`；query string 不参与聚合键，所以 `?page=1` 与 `?page=2` 是同一条接口。这条规则的效果是「分页漂移不会把一个接口拆成 N 条」，见 `test_analyze_risk.py` 的 `test_ids_in_path_do_not_split_one_endpoint` 与 `test_query_string_does_not_split_endpoint`。
- **分页导致的数组长度变化报 STRUCTURE**。第 2 页从 20 条变 19 条会被如实报成 `$.data.items[19]` 的 STRUCTURE 差异——这是期望行为，因为「页大小静默变化」本身就是需要人看一眼的信号。
- 如果确认某接口的分页抖动不可信，用 `ignored_paths` 摘掉该数组，或用 `unordered_paths` 让顺序变化不报差异。

### 1.8 配对与新增流量

`diff_all()` 按 `exchange_id` 与基线配对：

- 配不上基线的 outcome → `new_traffic_verdict()`，`matched_baseline=False`，`diffs=()`。**不做比对**（没有 expected 可比）。
- 传输失败（超时/连接错）→ `ExchangeVerdict.error` 非空、`diffs=()`。**也没有可比差异**——请求根本没打通，任何字段结论都不可信。

这两类都天然**不进评审队列**（见 §3），因为它们的 `diffs` 是空的。

---

## 2. 降噪：把易变值踢出评审队列

### 2.1 降噪规则

| 规则 | 命中条件 | reason 形态 |
| --- | --- | --- |
| `TimestampRule` | 两侧都是 ISO 8601 / RFC 1123 字符串；或两侧都是 ≥1e9 的 epoch 数字；或字段名（去下标后的最后一段）命中 `_TIME_LEAF_RE`（`created_at`/`updated_at`/`expires`/`ts`…） | `noise:timestamp(iso8601)` |
| `UuidRule` | 任一侧文本匹配 UUID4 形态 | `noise:uuid(uuid4-text)` |
| `AutoIncrementIdRule` | 字段名命中 `_ID_LEAF_RE`（`id`/`no`/`seq`/`index`）且两侧都是整数且差值 ≤ 100 | `noise:autoincrement_id(delta<=100)` |
| `FloatEpsilonRule` | 两侧都是数字且相对误差 ≤ 1e-9 | `noise:float_epsilon(rel<=1e-09)` |

`FieldDiff.reason` 只在 `NOISE` 时填充，格式固定为 `noise:<rule>(<detail>)`，所以报告里能直接统计「噪声都来自哪条规则」（`CategorySummary.noise_reason_counts`）。

### 2.2 结构/类型永不降级

`NoiseRule.hit()` 第一件事就是：

```python
if diff.level in (DiffLevel.STRUCTURE, DiffLevel.TYPE):
    return None
```

理由：字段消失或类型翻转**不会**是环境噪声，把它降级等于把真回归埋掉。这是「降噪」这条防线的边界——降噪只敢动 `VALUE`。

### 2.3 降噪规则的显式白名单

`IgnoreRules.volatile_value_fingerprints` 非空时按名字**只启用点名的规则**（`"ping:id"` → 只要 `id` 后缀匹配的规则）。默认空 = 全部规则生效。这让「某项目只想忽略时间戳、不想忽略 ID 漂移」这种诉求不用改代码。

---

## 3. 聚合口径：为什么报「接口数」而不是「差异条数」

**这是本项目降低人工评审成本的核心决定。**

`RiskSummary` 同时给两组数字，但**评审队列的口径是「有差异的接口数」**：

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `diffs_before_noise` | 降噪**之前**的原始差异条数 | 展示降噪效果的分母 |
| `noise_diffs` | 被降级为噪声的条数 | 展示降噪效果 |
| `total_raw_diffs` | 降噪后仍需人看的差异**条数** | 明细参考 |
| `total_diff_exchanges` | 有差异的 exchange 条数 | 明细参考 |
| `endpoints_with_diff` | **有差异的接口数** | **评审队列长度** |
| `endpoints_noise_only` | 仅有噪声差异的接口数 | 直接从队列剔除 |
| `endpoints_total` | 回放到的接口总数 | 覆盖率 |

聚合键是 `endpoint_of(method, path)` = `方法 + normalize_path(path)`，即 `GET /api/v1/resource17` 这一类**按模板聚合**。

实测（`tests/test_analyze_risk.py::test_800_raw_diffs_collapse_to_single_digit_endpoints`）：100 个接口 × 4 条回放，每条含 时间戳/UUID/自增 ID 三类抖动，其中 3 个接口各有一处真实值回归——

```
diffs_before_noise  = 1203   (1200 条噪声 + 3 条真实回归)
noise_diffs         = 1200
total_raw_diffs     = 3
endpoints_with_diff = 3      ← 评审队列
endpoints_noise_only= 97
convergence_ratio   = 401.0 : 1
```

**1203 条原始差异 → 3 个待评审接口。** 同一个接口抖 50 次也仍然只是 1 个接口（`test_raw_diff_count_and_endpoint_count_are_different_units`）。

### 3.1 优先级排序

`score = call_count × LEVEL_WEIGHT[worst_level]`。取 `worst_level`（该接口所有差异里最重的一档）而不是平均值：一个接口上任何一处 STRUCTURE 差异都比 20 处 VALUE 差异更该先看，平均会把结构问题稀释掉。

### 3.2 四类差异

`DiffCategory`：`REGRESSION` / `NEW_CONTRACT` / `NOISE` / `ENV_FAILURE`（外加 `CLEAN`）。`categorize()` 的优先级是**环境失败 > 契约内新增 > 真实回归 > 噪声**：

- 环境失败优先，因为「请求根本没打通」时一切字段结论都不可信，硬按字段报会制造假的回归。
- 契约内新增（`matched_baseline=False`）不叫回归——基线里没有这条流量，说明是新增业务而不是老接口变了。
- 只有全部差异都是 `NOISE` 才算噪声类。

排除了环境失败与零差异接口后，`build_risk` 只把**至少有一条可评审差异**的接口放进 `items`。

---

## 4. 回放器

### 4.1 并发与限流

`asyncio.Semaphore(concurrency)` 管并发，`QpsLimiter` 管最小间隔（`1/qps`）。两者都自己实现，不引第三方限流库。

`QpsLimiter.wait()` 用「下一个允许时刻」而不是「上次请求时刻 + 间隔」：

```python
now = time.monotonic()
delay = self._next_at - now
if delay > 0: await asyncio.sleep(delay); now = time.monotonic()
self._next_at = max(now, self._next_at) + self.min_interval
```

`max(now, self._next_at)` 保证即使某次睡眠被拉长（事件循环被占住），后续间隔也不会「追赶式」连发，从而把瞬时 QPS 顶上去。`qps=None/0` 表示不限速。

**一个实测坑**：断言并发不能看墙钟。本机 `httpx.AsyncClient` 首次启动 + `asyncio.run` 有约 0.2–0.24s 固定开销，4 条 0.2s 延迟的请求实测 0.44s——看起来像串行，其实服务端 4 条是同时 `recv` 的。所以测试改成断言**服务端同时处理的条数**（`recorder.max_concurrent`）+ 一个宽松的墙钟上界。

### 4.2 请求头白名单

录制的头**只有白名单内的字段会透传**（`DEFAULT_HEADER_ALLOWLIST`：`accept`、`accept-language`、`content-type`、`user-agent`、`x-api-version`、`x-app-id`、`x-correlation-id`、`x-request-id`、`x-trace-id`），其余一律丢弃。`FORBIDDEN_HEADERS` 再兜一层，即使出现在白名单或注入头里也丢掉。

**`Host` / `Content-Length` / `Authorization` 三条是硬要求**：

- `Host` 必须由 httpx 按真实目标生成，透传录制值会把请求打到生产主机。
- `Content-Length` 必须按真实 body 重算，透传会导致 body 被截断或挂起。
- `Authorization` 必须由调用方经 `ReplaySources.headers_override` 重新注入——录制里的 token 早已过期，且脱敏后是占位符。

实测证据：

```
服务端实际收到 method = POST  body = b'{"qty":1}'
  authorization: Bearer NEW-INJECTED      ← 调用方注入，不是录制的 Bearer OLD-RECORDED-TOKEN
  content-length: 9                        ← 按真实 body 重算，不是录制的 999
  host: 127.0.0.1:14558                    ← 真实目标，不是录制的 prod.example.com
  x-env: shadow / x-shadow: 1              ← 写方法的影子头
```

### 4.3 写方法影子模式

`WRITE_METHODS = {POST, PUT, PATCH, DELETE}` 默认走影子路径：`build_headers` 只在方法属于写方法时附加 `ReplaySources.shadow_headers`。读方法（GET）**不附加**影子头——实测的对照用例证明 GET 上没有 `x-shadow`/`x-env`。

影子头是「路由标识」而不是「改目标」：真正的隔离靠 `base_url` 指向影子环境，`shadow_headers` 只是在同一入口下让网关把写请求分流。因此 `shadow_headers` 里也不允许出现 `Host`/`Content-Length`。

### 4.4 单条失败隔离

`_one()` 里整个请求过程包在 `try/except Exception` 里，任何异常（超时、连接错、DNS、协议错）都降级成一条带 `error` 的 `ReplayOutcome`（`status=None`），`asyncio.gather` 因此不会因单条失败而抛。整轮回放不中断，失败条数由 `ReplayReport.failed` 报出，分类落在 `ENV_FAILURE`。

### 4.5 同步包装

`run()` 检查当前线程是否已有运行中的事件循环：没有就 `asyncio.run()`；有（例如被 pytest-asyncio 或上层异步框架调用）就把协程丢到独立线程里跑，避免 `asyncio.run() cannot be called from a running event loop`。

---

## 5. 编排与落库

`analyze/runner.py`：

- `run_replay(exchanges, player, rules=, baseline_id=, policy=)` → `ReplayRun`（报告 + verdicts + 风险汇总 + 分类账 + outcomes）。
- `replay_baseline(db_path, baseline_id, base_url, ...)` → 从库里读基线、回放、比对、分类、`save_report()` 落库。
- `ReplayRun.to_json()` / `render_markdown()` 给 CI 与人看的两份产出。

`recorder.store` 由录制端提供，回放端**只调用不实现**，且对它做**懒加载降级**：

- `load_repository()` 在 `recorder.store` 导入失败（模块还没写好、ImportError）时返回 `None`，让「只出内存报告、不落库」这条路径可用，而不是让整条回放链路崩掉。
- 但 `replay_baseline()` 在拿不到 repository 时**抛 `RuntimeError` 而不是返回空报告**——「读不到基线」和「基线是空的」必须在调用方眼里可区分，静默返回空报告就是让人误以为回放干净。

---

## 6. 面试速答

- **怎么比？** 递归 JSON 树，键增删=`STRUCTURE`，类型变=`TYPE`，值变=`VALUE`；只看这三档，降噪交给独立的 analyzer。
- **怎么降噪？** 时间戳/UUID/自增ID/浮点尾差四条规则把 `VALUE` 降成 `NOISE` 并写 reason；**结构/类型永不降级**。
- **怎么降人工成本？** 评审口径是「有差异的接口数」而不是「差异条数」，接口按 ID 归一化的模板聚合。实测 1203 条原始差异 → 3 个待评审接口。
- **可忽略字段？** `ignored_paths` 是判定层（整段不比，条数不产生），降噪是分类层（条数计入 `diffs_before_noise`）。两者口径不同，报告里能分别看到。
- **顺序无关数组？** `unordered_paths` 命中后两侧排序成规范序再逐位比，能复用有序数组的全部递归逻辑。
- **浮点容差？** `float_tolerance` 是绝对值容差，`0.0` 表示关闭；另有 1e-9 的相对误差降噪规则兜住序列化尾差。
- **分页差异？** 不比分页参数本身；query string 不进聚合键，所以分页不会把接口拆开；页大小变化如实报 `STRUCTURE` 让人看一眼。
- **为什么不能透传 Host/Content-Length/Authorization？** 分别会把请求打到生产主机、导致 body 截断、用过期 token 鉴权；三者都必须由传输层或调用方重新生成。
- **写方法怎么保护生产？** 默认影子模式（写方法附加影子路由头），且读方法不附加；真正的隔离靠 `base_url` 指向影子环境。
