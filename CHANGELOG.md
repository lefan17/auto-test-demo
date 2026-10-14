# 更新日志

本文件记录这个仓库的**真实**变更历史。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，但有一条必须说清的差异：

> **本仓库目前没有任何 git tag**（`git tag` 输出为空），所以下面**不存在版本号**。
> 与其编一个 `0.1.0` 出来假装有发布流程，不如按**提交日期**分组并附上真实 SHA——
> 这份文件里每一行的 SHA 都能用 `git show <sha>` 直接查到。
> 等真正打了第一个 tag，再把版本号补上。

---

## [未发布]

### 新增 —— 性能基线回归（波 3 · P2-2）

病灶：`testcases/api/test_response_time.py` 只有 2 条、且**没有基线**，唯一的时间断言是
「**单次**请求 `elapsed_ms < MAX_RESPONSE_MS`」，打的是公网 `jsonplaceholder`。
一次请求的耗时是纯噪声，公网链路又不可控 —— 这个门禁既没有分辨力，也没有可比性。

- **`testcases/api/perf_baseline.json`（新增）**：把 P95 落成入仓基线。6 个只读端点
  （`/healthz`、`/materials`、`/materials/{code}`、`/inventory/stock`、`/work-orders`、
  `/inventory/ledger`）各采 **100 次**、丢弃前 **10 次**预热，记录
  P50/P95/P99/max/mean/samples 与采集环境指纹（平台/CPU/Python/分位方法/阈值公式）。
  **文件里没有一个手编数字**，全部来自一次真实采集。
- **`testcases/api/perf_baseline.py`（新增，331 行）**：纯逻辑层——nearest-rank 分位、
  基线读写、阈值判定、diff 渲染。不 import pytest、不起服务，所以门禁用例与重采入口用的是
  **同一份**判定实现（两份实现迟早飘掉，而门禁飘掉是不报错的）。
  用 nearest-rank 而非线性插值：N=100 时插值会给出一个**从未真实发生过**的延迟值
  （例如第 94.5 个样本），排查回归时「这条 P95 对应哪次请求」必须能当场指出来。
- **阈值 = `基线 P95 × 3.0 + 10ms`**：倍数吸收跨机器的系统性差异，绝对下限吸收
  「基线只有几毫秒时倍数放大不出分辨率」。定位说清楚——它拦的是**量级劣化**
  （漏索引、N+1 查询、同步阻塞变大），不是几毫秒的漂移。要更严调
  `PERF_P95_FACTOR` / `PERF_P95_FLOOR_MS`，不必改代码。
- **`testcases/api/test_perf_baseline.py`（新增，2 条）**：基线文件可用性 + P95 预算判定。
  基线缺端点/缺字段/分位次序颠倒/样本数不足都单独判红 —— 「门禁没了基线」和
  「门禁发现回归」处置完全不同，必须能分辨。同时打 `api` 与 `perf` 两个标记：
  前者让它被 `-m api` 这个总门禁自动包含，后者让它能单独跑（并行批次的调度抖动会污染采样）。
- **`testcases/api/conftest.py`（新增）**：session 级起本地 `mock_server`，**复用**
  `testcases/mes/mock_server_ctl.MockServer`（含「子进程连的库是否与期望一致」的健康校验，
  正好挡住「服务没起起来、请求打到别的端口」）。基线**必须**打在本地：同一份代码、同一份
  种子数据、同一个 SQLite，只有这样「P95 变差了」才指向被测代码。该目录原先没有 conftest.py，
  且 fixture 无 autouse，对既有 api 用例零影响。
- **抖动类用例明确切割**：`test_response_time.py` **一字未改**保留（它还能发现代理/证书/DNS
  这类问题），但新文件 docstring 里写清「它测的是链路、属抖动类、**与基线无关**」。
  新用例**既不**打 `NETWORK_FLAKY`，失败文案也**刻意避开**「接口响应过慢」——
  那句话是 `flaky_policy.py` 的重跑指纹，撞上就等于把真回归吸收一次、门禁被悄悄放宽。
- **显式重采入口**：`UPDATE_PERF_BASELINE=1 python -m pytest -m perf -s`。两条硬约束：
  只能由一次真实采集写文件；必须打印 `旧值 → 新值`（否则「重新采集」与「悄悄放宽门禁」
  的输出无法区分）。
- **`pytest.ini`**：markers 段**追加** `perf` 一条（`--strict-markers` 下不登记会在收集期报错），
  既有 9 条 marker 一字未动。
- **验收实测**（都带原始命令与关键输出行，见 `docs/wave3-perf-baseline-handoff.md`）：
  `pytest -m api` 由改动前 `130 passed` → **`132 passed, 239 deselected`**；
  **反向验证**把绝对上限设成 1ms → **6 条端点全红**（`Failed: 性能基线回归：...`），
  撤掉后回到 `2 passed`，证明门禁真的在判；
  **稳定性**同一梯度重复三次，单端点 P95 轮间波动最大 **1.25x**（把任务期间八次同口径采集一并计入 1.96x），
  阈值倍数 3.0 对实测最慢 P95 仍留 4 倍以上余量 ⇒ **容差无需调整、门禁不会随机红**；
  另外补验了两条阈值臂（`PERF_P95_FACTOR` / `PERF_P95_FLOOR_MS`）与「显著变快」提示分支，
  并在 CI 同款命令 `pytest -m api -n 4 --dist loadfile` 下实测 **132 passed**；
  `ruff check .` → `All checks passed!`、`ruff format --check .` → `97 files already formatted`、
  `python -m mypy` → `Success: no issues found in 77 source files`。
- **零新增依赖**：只用 `requests` + stdlib，`requirements.txt` / `requirements.lock` **未改**。
  `testcases/mes/`、`testcases/login/`、`testcases/contract/`、`testcases/ui/`、`apis/`、
  `mock_server/`、`common/`、根 `conftest.py`、`login_app/`、CI workflow **全部未动**。
- ⚠️ **未验证**：**没有在 Linux runner 上实测**（基线里的环境指纹是 Windows 本机，
  且 CI 的 `pytest -m api -n 4 --dist loadfile` 会在 4 核 runner 上与其它 worker 抢 CPU，
  采样被压慢的倍率无从得知）。详见
  `docs/wave3-perf-baseline-handoff.md` 的「未验证项清单」。

### 新增 —— 认证层与契约驱动（波 1）

这一批改动的主题是：**把「口令写死在代码里」和「接口契约靠人手抄」这两件企业里最先出事的事处理掉**。

#### 口令与密钥外置

- **`common/secrets.py`（新增）**：所有口令统一走一个取值入口。优先级是
  **已存在的环境变量 > `.env` 文件 > 本地 Mock 默认口令**，三者都没有时直接报错并指向 `.env.example`，
  不静默回落。
- **`apis/mes_api.py`**：原先 5 个角色的账号口令是明文常量（`ROLE_ACCOUNTS`），现在只保留
  「角色 → 账号名 + 口令环境变量名」的映射。实测 `git grep` 那六个口令字面量 → **0 命中**（改造前 22 行）。
- **`data/config*.yaml` 支持 `${VAR}` 引用**：由 `common/config.py` 的 `expand_placeholders()` 展开，
  UI 用例的口令因此也能走环境变量。
- **承重性有红证**：把 `mock_server/db.py` 的种子口令临时改回不认环境变量，用例立刻红
  （`AssertionError: 用自定义口令登录失败，说明种子口令没跟着 provider 走：401 {...INVALID_CREDENTIALS...}`）；
  **默认行为逐字节不变**：不设任何变量时，7 个本地 Mock 默认口令与改造前完全一致。

#### 令牌生命周期

- **`apis/token_provider.py`（新增）**：令牌带过期时间与刷新余量，`RoleClient` 改为按需取令牌而非缓死。
- **`apis/base.py` 新增 401 单次重放**：四条条件缺一不可——
  `status_code == 401` **且** 本次请求用的是令牌 **且** 本请求还没重放过 **且** 方法是幂等的。
  403 不重放（权限不足重放多少次都一样）、POST 不重放（可能已经产生副作用）、
  调用方自传 `Authorization` 时不重放（尊重显式意图）。
  重放额度**独立于 `self.retries`**：`MesApi` 默认 `retries=0`，共用额度则重放永不发生。
- **承重性有红证**：删掉刷新时的代次比对 → `4 个线程撞同一个过期令牌，只应登录 1 次，实际 4 次`。

#### 契约驱动：自动导出 + 属性测试

- **`scripts/export_openapi.py`（新增）**：从 FastAPI 应用自动导出 `docs/openapi.json`（29 operations）
  与 `docs/openapi-login_app.json`。入库的 spec **逐字节等于 `app.openapi()`**，
  任何用例都不许改它——否则漂移门禁就退化成「文件和自己一致」。
  `--check` 模式（CI 用的那个）比对文件字节，不一致就红。
- **`testcases/contract/`（新增，224 条）**：schemathesis 按 spec 生成请求打真实服务。
  另建一份**内存态补全视图**（`spec_contract.py:build_test_view()`）补上 spec 缺失的
  security 语义与错误响应声明，而**不动入库文件**。
- **扫出 6 类真实契约缺陷**，其中 DEF-05 / DEF-06 **本次已修**：
  - **DEF-05**：`{"qty": "INFINITY"}` → **500** 而非 422。`mock_server/quantity.py` 里
    `Decimal(str(value))` 构造成功，异常实际抛在 `quantize()`，而 `try` 只包住了构造那一行。
    修法：显式挡非有限值**并**把 `quantize` 收进 try——两者缺一不可
    （`'1e999999'` 构造成功、`is_finite()` 为真，却仍在 `quantize` 抛 `InvalidOperation`）。
  - **DEF-06**：`page=461168601842738792` → **500**。`page` 无上界，`(page-1)*page_size`
    越过 SQLite INTEGER 上限，绑参时抛 `OverflowError`，被兜底 handler 变成 500。
    修法：`mock_server/deps.py` 用 `MAX_SQLITE_INTEGER = 2**63 - 1` 在拼 OFFSET 前挡住，
    返回 **400 `PAGINATION_OUT_OF_RANGE`**（复用既有错误类，与 `page<1` 同类同码）。
    **刻意没给 Query 加 `le`**：加了超界会变成 FastAPI 的 422，破坏既有 `page=0 → 400` 的约定。
  - 这两条都是**属性测试扫出来的**，手写用例基本想不到：`"INFINITY"` 语法上是合法字符串、
    `page=461168601842738792` 语法上是合法整数。
  - 其余 4 类（DEF-01/02a/02b/03/04，共 **11 条**）**不修，用 `xfail(strict=True)` 钉住**：
    修好会立刻 XPASS 变红，缺陷单会自己找上门。
- **门禁的意义在于确定性**：schemathesis 默认按时间预算自适应采样，机器快慢不同就得到不同样例集，
  于是「这次绿下次红」成为常态。CI 里把样例数钉死为 `CONTRACT_MAX_EXAMPLES=8`。
  实测默认样例连跑 2 次均 `213 passed, 11 xfailed`；放大到 50 样例（`CONTRACT_FULL=1`）
  仍是 `213 passed`，耗时 58s（默认约 22s）。
  **契约套件早期并不确定**：`.hypothesis/` 里的样例数据库会重放历次找到的失败样本，
  表现为「清空后绿、样本留着就红」——当时正是靠这个现象顺藤摸到 DEF-06。
  `.hypothesis/` 已加入 `.gitignore`。

#### 一起补齐的工程化

- **`requirements.lock` 重建**（123 → 220 行）：纳入 schemathesis 及传递依赖。
  已有的 pin 全部未被顶版（`pytest==8.3.3` / `requests==2.32.3` / `starlette==0.41.3` /
  `jsonschema==4.23.0` 实测一致）。
- **CI 的 `test` job**：新增契约漂移检测与契约属性测试两步；注入 7 个口令 secret；
  在检出后固定 `core.autocrlf false`（`--check` 比字节，runner 上 autocrlf 会把行尾差异误报成契约漂移）。
- **数字全面更新**：用例 106 → **354**（33 接口 + 14 UI + 54 MES + 29 登录 + 224 契约）；
  `-m api` 92 → 116；`-m mes` 30 → 54；mypy 54 → **65 source files**；
  覆盖率 43.89% → **74.30%**（全量 `-m "not ui"`，3953 statements / 1016 miss）。
- ⚠️ **如实登记两件事**：
  1. `mock_server/*` 的覆盖率**不再显示为 0%**，但那不是"跑着的 Mock 服务被 HTTP 打到"的
     行为覆盖——`--collect-only` 时它仍全部为 0%，实测数字来自导出脚本子进程的**模块级执行**
     被 pytest-cov 记账。由 fixture 拉起的真服务子进程仍未被统计，`services.py`（359 条）是最大盲区。
  2. `-m api` 里有 33 条打**公网**（jsonplaceholder），实测 4 轮里 3 轮撞上
     `502 Bad gateway`，单轮 1~5 条失败。这不是代码问题了，**处置留到波 2 的 flaky 治理**，
     本轮不掩盖也不修。

### 新增 —— 子进程服务的覆盖率采集（波 2）

被测服务由 fixture 以常驻子进程拉起，收尾走 `terminate()`；Windows 上那是
`TerminateProcess`，不跑 `atexit`，而 coverage 的数据正是在 `atexit` 落盘——
所以 `mock_server/*` 的覆盖率恒为 0%（Handler 路径一行都没记上）。

修法不是给子进程套 `coverage run --parallel-mode`，而是给它一条**能正常返回**的退出路径：
新增 `scripts/service_launcher.py`，在进程内装一个"吸收信号"的 handler 后再
`runpy.run_module(<服务模块>)`。uvicorn 关闭时会 `signal.raise_signal(原信号)` 把信号
重发给自己（默认处置终结进程、不跑 atexit），launcher 把它接住，于是 `serve()` 正常返回、
`atexit` 执行、分片落盘；`stop()` 同时改成先礼后兵（优雅信号 → `terminate()` → `kill()`）。
新增 `.coveragerc`（`[run] parallel = true`）承载分片模式，分片由 pytest-cov 在 session
结束时自动 combine。

实测：`-m mes --cov=mock_server` 由 9% 升到 **83%**（routers 0% → 84%、services 0% → 70%）；
`-m login --cov=login_app` 由 31% 升到 **85%**；全量由 74.30% 升到 **89.70%**，
CI 那一组由 52.23% 升到 **80.31%**。耗时代价约 +4%。

### 新增 —— 限流退避：尊重 `Retry-After` 并加有界退避（波 2）

- **`apis/base.py` 的重试等待不再写死**：改造前两处（传输失败、命中重试码）都是
  `time.sleep(0.5 * 2**attempt)`，网关回 429 时我们照撞不误。
  现在优先读响应头 `Retry-After`，两种格式都支持：delta-seconds（`Retry-After: 2`）
  与 HTTP-date（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`），
  解析走 `email.utils.parsedate_to_datetime`（stdlib）。
- **退避有硬上限**：新增 `API_MAX_RETRY_WAIT`（默认 30 秒）。
  服务端若回 `Retry-After: 86400`，不会把 CI 挂在那里一整天，而是等到上限就重试或放弃。
- **`tools/check_retry.py` 从 12 条扩到 22 条**：新增 10 条覆盖 `Retry-After` 的解析、
  上限截断、无头回落指数退避，以及 429 + `Retry-After` 的端到端路径；原 12 条逐条不变全绿。
- **端到端证据用真 socket**：`testcases/api/test_retry_after.py` 里 9 条起本地
  `http.server` 返回 429 + `Retry-After`，请求真的过一遍 socket。
  Mock 证明的是「分支选对了」，证明不了「这个头是从真实响应里读出来的」
  （`CaseInsensitiveDict`、大小写、header 折行、HTTP 版本差异只在真过 socket 时才可能出现）。

### 新增 —— flaky 治理（波 2）

- `requirements.txt` 新增 `pytest-rerunfailures==16.7`。
- `pytest.ini` 新增 `flaky` 与 `quarantine` 两个 marker；`addopts` 由 `-ra` 改为 `-raR`
  （插件的 rerun 摘要只在 reportchars 含 r/R 时打印，否则重跑在汇总里是隐形的）。
  **`addopts` 里不加 `--reruns`**：全局重跑会把真回归一起吞掉。
- `testcases/api/flaky_policy.py`：`NETWORK_FLAKY` 用插件的 `only_rerun` 把重跑收窄到
  6 条抖动指纹，`testcases/api/` 的 33 条公网用例全部挂上。CI 实测 4 轮 3 轮撞上的
  502/429 抖动现在被重跑吸收，而契约断言红了仍当场硬失败。
- `conftest.py`：重跑留痕 + 隔离区两个钩子。重跑才通过 / 重跑仍失败 / 被隔离三类用例
  写进 `reports/flaky-governance.json`（junit-xml 本身不认 rerun，只能自己记）；
  隔离区用例在主门禁里 deselect，但清单逐条打印。
- `scripts/flaky_report.py`：从账本产出「重跑才通过 / 隔离区」清单，支持 `--markdown`
  （贴 step summary）与 `--json`（周报）。本地一条命令即可跑出。
- `testcases/api/test_flaky_governance_meta.py`：5 条元测试，开真 pytest 子进程验证
  「标了 flaky 的用例首跑失败会被重跑」「真回归不被重跑吞掉」「隔离区用例退出主门禁且清单被打印」
  「隔离区能单独跑」。三条机制各配一次反向对照（改坏 → 红 → 还原 → 哈希一致）。

### 修复 —— 依赖升级暴露的动态属性写法（requests 2.34 自带类型桩）

- **现象**：`dependabot` 的 http-stack 分组 PR（`requests==2.32.3` → `2.34.2`）在 `typecheck` job 变红：

  ```
  apis/base.py:111: error: "Response" has no attribute "elapsed_ms"  [attr-defined]
  apis/base.py:112: error: "Response" has no attribute "was_retried"  [attr-defined]
  Found 2 errors in 1 file (checked 54 source files)
  ```

- **根因**：`requests` 在 **2.34 首次带上 `py.typed`**（实测 wheel：2.32.3 无、2.33.0 无、2.34.2 有
  `requests/py.typed`）。此前 mypy 把 `requests` 当无类型库、`Response` 视作 `Any`，动态挂属性不报错；
  有了类型桩之后这两行被当真检查，而属性**确实不存在**——报得没错，是代码之前占了「没有类型标注」的便宜。
  本仓库 `pyproject.toml` 的 `ignore_missing_imports` 注释（「本仓库 import 的库都没有 py.typed 标记」）
  这一前提即由此被上游打破，注释已同步改写。
- **修法**：`apis/base.py` 改用 `response.__dict__["elapsed_ms"] = elapsed_ms` 写实例字典。
  为什么不是更常见的两种写法——两者都实测踩过：
  - 直接 `response.elapsed_ms = ...`：mypy `[attr-defined]` 判红。
  - `setattr(response, "elapsed_ms", ...)`：mypy 放行，但 **ruff 的 B010** 判红
    （常量属性名用 `setattr` 并不比直接赋值安全），等于把问题从一把刀挪到另一把刀下。
  实例字典写法的额外好处是**不动调用方**：`elapsed_ms` / `was_retried` 是
  `testcases/api/test_response_time.py` 与 `tools/check_retry.py` 依赖的公开接口，
  换成 dataclass 包装会牵动 30 处调用。
- **验证**（都在 `requests==2.34.2` 下跑，即 PR #13 的真实条件）：
  `python -m mypy` → `Success: no issues found in 54 source files`；
  `python -m ruff check .` → `All checks passed!`；
  `python -m pytest -q -m "not ui"` → **92 passed, 14 deselected**；
  `python tools/check_retry.py` → **12/12 通过**（第 11、12 条正是消费 `was_retried` 的检查）。
  本机 venv 事后回退到 `requests==2.32.3`（与 `requirements.txt` 一致），两种版本下结论相同。

### 新增 —— 工程化外壳（十项）

这一批改动的主题是：**把已经写在配置里、但从来没有被执行过的规矩真正跑起来**。
在此之前 `ruff.toml` 立在仓库里很久，却没有任何地方执行它。

- **依赖锁文件**：新增 `requirements.lock`（123 行，由 `pip-compile --strip-extras` 生成），
  把 13 个直接依赖展开成完整的传递依赖树（含 anyio / certifi / urllib3 / greenlet 等）。
  之前只锁直接依赖，传递依赖随环境漂移，本地与 CI 装的不是同一棵树。
- **类型检查**：新增 `pyproject.toml`，承载 `[tool.mypy]` 配置与 `requires-python = ">=3.10"`；
  CI 新增独立的 `typecheck` job（此前仓库完全没有类型检查）。
  实测 `--strict` 会报 **342 个错误**，当前配置按"能通过且不撒谎"的标准收敛到
  `Success: no issues found in 54 source files`——已知技术债在配置里**逐模块登记**
  （用 `disable_error_code` 而非 `ignore_errors`，也不用 `# type: ignore`），
  为什么这样选写在 `pyproject.toml` 的注释里。
- **覆盖率门禁**：CI 接入 `pytest-cov`（此前装了但从未调用），
  `--cov-fail-under=40`。实测 `-m "not ui" --cov=.` → **2889 statements / 1621 miss / 43.89%**，
  门槛留 ~4pp 余量，理由是门禁的意义在于拦**退化**而不是卡住当前值。
  已如实登记一个盲区：`mock_server/*` 恒为 0%（1358 条 statements），
  原因是它由 fixture 以 subprocess 拉起，coverage 采集不到子进程——
  而这 1358 条恰是被测得最狠的代码，所以 43.89% 是**低估**。
- **并行执行**：`-m api` 那一步接上 `-n 4 --dist loadfile`，
  实测墙钟 **26.42s → 12.03s**（约 2.2×）。**必须是 `loadfile` 不是默认的 `load`**：
  实测 `-m mes -n 4 --dist load` → `30 passed, 1 error`（退出码 1），
  原因是 session 作用域的数据隔离基线与按用例分发错配。
  `-m mes` / `-m login` / `-m ui` **刻意不开**并行，理由写在 workflow 注释里。
- **容器化**：新增 `Dockerfile`（`python:3.12-slim`，先 COPY requirements 再安装以吃到依赖层缓存）
  与 `docker-compose.yml`（`mock-server` + `login-app` 两个被测服务 + 按需启动的 `tests`）。
  ⚠️ **未验证**：本机 Docker daemon 无法启动（WSL `E_ACCESSDENIED`），
  只做了静态校验 `docker compose config --quiet` → 退出码 0。
  **从未在本机执行过 `docker build`，不得声称镜像构建成功。**
- **安全扫描**：新增 `.github/dependabot.yml`（`pip` + `github-actions` 两个 ecosystem，
  每周一）与 CI 的 `security` job（`pip-audit --requirement requirements.lock --strict`，
  非阻塞）。非阻塞的理由：pip-audit 查的是漏洞库的当前状态而非这份代码的质量，
  设成阻塞等于让"上游某天披露 CVE"变成"所有人的 PR 都过不去"，最后一定被删掉。
- **提交规范工具化**：新增 `scripts/check_commit_msg.py`（Conventional Commits 校验，
  纯 Python 不依赖 Node）+ `.pre-commit-config.yaml` 的 `commit-msg` hook，
  以及 CI 的 `commit-convention` job（拿最近 30 条真实提交给这个脚本做回归测试——
  否则脚本本身写错会变成"拦住所有人提交、但 CI 一片绿"的隐形故障）。
- **pre-commit**：新增 `.pre-commit-config.yaml`，hooks 用仓库**已有且已绿**的 ruff
  （`rev` 与 CI 钉在同一版本 `v0.16.10`，参数一致）加基础文件检查。
  ⚠️ **未验证**：本机装不上 pre-commit（沙箱拒绝 `ensurepip`，`PermissionError: [WinError 5]`），
  此配置**仅 CI 可验证**。
- **协作模板**：新增 `.github/PULL_REQUEST_TEMPLATE.md`、`.github/ISSUE_TEMPLATE/`
  （`bug_report.md` + `feature_request.md` + `config.yml`）、`CODEOWNERS`、`CONTRIBUTING.md`。
- **CI 定时与失败通知**：workflow 新增 `schedule`（每周一 UTC 18:00 夜间回归）、
  `concurrency`（PR 上取消排队中的旧任务，main 上的 push 全跑）、
  各 job 的 `timeout-minutes`、`lint` job 补上 pip 缓存。
  失败通知走**不需要任何外部 secret** 的两条路：`if: failure()` 写 step summary，
  以及用内置 `GITHUB_TOKEN` 自动开/更新 issue（靠固定 label 去重）。
- **环境分层**：新增 `common/config.py`（`TEST_ENV` 选择 dev/staging/prod 档案）、
  `data/config.staging.yaml`、`data/config.prod.yaml`。
  **默认行为逐字节不变**：未设 `TEST_ENV` 时 `get_config()` 的返回值与旧的
  `common/yaml_util.get_config()` 直接读 `config.yaml` 完全一致（已用 JSON 对比验证）。
- **文档**：新增本文件与 `CONTRIBUTING.md`；`README.md` 补上上述能力与**实测**数字。

### 修复 —— 补齐容器化与 CI 的两处不一致

这一批只做两件事，都是把上面那批工程化外壳里**漏掉的一致性**补齐：
同批次的其他仓库都有的东西，这里缺了两样。

- **容器化**：新增 `.dockerignore`。之前 Dockerfile 靠精确 `COPY` 顶住了镜像内容，
  但 `docker build` 打包 context 时与 COPY 写什么**无关**——实测仓库 361.6 MB
  （`.venv/` 197.5 MB、`.tmp/` 146.1 MB、`.mypy_cache/` 10.4 MB、
  `.piptools-cache/` 5.0 MB，`.git/` 也在），这一份每次构建都要整体传给 daemon。
  两者职责不同，所以精确 COPY **保留不动**（它是白名单，管镜像内容；
  `.dockerignore` 是黑名单，管上下文体积），只把 Dockerfile 里那句
  「本仓库没有 .dockerignore」的注释改成与新事实一致，技术理由原样保留。
  ⚠️ **未验证**：本机 Docker daemon 起不来（WSL `E_ACCESSDENIED`），
  这份文件**没有**在真实 `docker build` 上观测过。已做的替代校验：
  逐条对照 Dockerfile 的全部 COPY 源（135 个文件），确认无一条源码/配置/数据文件被排除
  （被命中的 65 个**全部**是 `__pycache__/*.pyc` 本机字节码缓存）；
  用 gitignore 语义库对全仓文件跑匹配回归；`docker compose config --quiet` → 退出码 0。
- **CI 钉版本**：`security` job 的 `pip install pip-audit` 与 `pre-commit` job 的
  `pip install pre-commit` 改为 `pip-audit==2.10.1` / `pre-commit==4.3.0`。
  同一文件里 ruff（`0.16.10`）和 mypy（`2.4.0`）都钉了版本，只有这两条漏了；
  未钉版本意味着上游某天发新版就可能让 job 因为"工具自身行为变了"而变色。
  两个版本号均由 PyPI JSON API 查证存在，并**实际下载 wheel 校验 sha256 后离线安装成功**。
  pre-commit 刻意不取当时的 latest（4.7.0）：这个 job 是验证配置有没有腐烂，
  钉在已知可用版本上，红色才只反映配置本身。

---

## 2026-10-13

### 新增
- 补齐 `LICENSE`，并把 ruff 真正接进 CI。（`3b587d0`）

### 修复
- `fix(mes)`: 消灭并发/隔离/幂等用例的假绿，补承重断言。（`bd107fd`）
  这一条是本仓库最有价值的一次修复，值得单独说明：在此之前，并发用例用
  **耗时**来判断"请求是否真的并发"，而实测发现同一对审核请求
  **并发墙钟 25.4ms / 串行 4.8ms**——并发反而更慢（因为 `mock_server/db.py`
  的 `write_tx` 用 `BEGIN IMMEDIATE` 把写请求串行化了）。
  也就是说"耗时短 = 真并发"这个判据本身是错的，它一直在给绿。
  现在改成用**请求时间区间是否交叠**来判定（`testcases/mes/concurrency.py`
  的 `overlap_ms` / `assert_requests_overlapped`），串行时交叠恒 ≤ 0，无法伪造。

## 2026-10-10

### 新增
- 合并登录服务契约用例，并补文档与设计方法说明。（`cca518b`）
  `testcases/login/` 的 29 条用例用 5 种设计方法（等价类 / 边界值 / 判定表 /
  场景法 / 错误推测）从规格推出，并钉住被测服务**当前行为**——包括 6 个已知缺陷
  （FIND-01~06，见 `docs/login-contract.md` 的"契约 vs 实现"差异表）。

## 2026-10-03

### 新增
- 接入 MES 业务全链路用例与数据隔离检查。（`59aef0a`）
- 传输层代理回退与耗时口径修正。（`2d64011`）
- 接口 + UI 自动化测试框架初始版本。（`366a20a`）

### 修复
- 换掉已腐烂的 Allure action，修正截图上传路径。（`1a79a7d`）
  原来用的 `simple-elf/allure-report-action@v1.9` 依赖镜像 `openjdk:8-jre-alpine`，
  该镜像已被 Docker Hub 下架，报 `failed to resolve source metadata`，
  导致整个 job 在第一步就失败、连测试都没跑到。改用官方 Allure 命令行。
  同时修正截图上传路径（实际落在 `testcases/ui/artifacts/`，原写的是 `ui/artifacts/`，
  截图从未被上传过）。
- 重试只针对幂等请求，并统一超时来源。（`2a205de`）

### 文档
- 订正 README 里的三处过期数字。（`2e9a5c4`）
- 填入真实仓库地址，徽章生效。（`372673f`）

### 维护
- 统一换行符，消除 CRLF 警告。（`8da0118`）

---

## 尚未修复的已知问题

这些不是"待办清单上的装饰"，是**当前真实存在**的问题，写在这里以免被忘掉：

- **`login_app/app.py` 的 6 个已知缺陷**（FIND-01~06）**有意保留**：
  契约用例钉的是"当前行为就是这样"。修好它们会让 `-m login` 变红，
  届时需要同步改用例——顺序是先看差异表，再决定改哪边。
- **`apis/base.py` 的代理回退判据有缺陷**：`last_error is not None` 记的是
  "这个请求曾经出过传输失败"，而不是"上一次尝试就是传输失败"。
  于是传输失败之后，**由 5xx 触发的重试也会绕过用户显式配置的代理**。
  这条缺陷目前**还没有用例钉住**——原计划的「第 13 条」在波 2 之后已被
  `Retry-After` 的用例占用，补这条缺陷用例时应接在末尾（当前 22 条，补后为 23 条）。
- **`free_port()` 有一个 TOCTOU 端口竞态**（Windows 本机偶发，非本次改造引入）：
  它是 bind→close→把端口交给服务，中间有窗口；偶发失败于 `winerror 10013`
  （端口落在 Windows 动态段 1024–15000 内）。建议后续给它加「启动失败换端口重试」。
- **mypy 未做到全仓零错误**：`--strict` 下 342 个错误。
  当前配置通过中把已知技术债**逐模块登记**在 `pyproject.toml` 里，
  而不是消灭它们。这是一份有账可查的分阶段方案，不是"已经零错误"。
- **`-m ui` 在本机沙箱无法运行**：14 条用例在 setup 阶段全失败
  （`PermissionError: [WinError 5]`，asyncio 建管道被禁），属环境限制，非用例缺陷。
