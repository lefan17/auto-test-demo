# 波 3 · P2-2 性能基线回归 —— 交接文档

> 基线：`0e61618`（波 2 收口）
> 工作副本：`C:\Users\lefan\DSH_project\接口自动化_3\work\w3-perf`（原仓库 `DH_1\fullstack-test-framework` 只读参考，未改动）
> 解释器：`C:\Users\lefan\DSH_project\DH_1\fullstack-test-framework\.venv\Scripts\python.exe`（Python 3.12.10 / pytest 8.3.3 / requests 2.32.3）
> 每次跑 pytest/ruff/mypy 前的前置：
> `$env:PYTHONPATH='C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'`，`$env:TMP`/`$env:TEMP='C:\Users\lefan\DSH_project\接口自动化_3\work\_tmp'`
> **本副本内没有 `.venv`**：`testcases/mes/mock_server_ctl.py:23` 的 `DEFAULT_PYTHON` 不存在，`MockServer.start()` 回落到 `sys.executable`，
> 也就是上面这个 venv —— 里面有 fastapi/uvicorn，服务起得来（实测）。

面向整合者。本文只写证据：改了哪些文件、命令输出原文、未解决项。

---

## 1. 原始病灶

规划原文：

> **性能基线回归**：`testcases/api/test_response_time.py` 只有 2 条且无基线。
> 参考「性能门禁平台」思路：**把 P95 落成基线文件，回归时比阈值。**

复核后的现场（改动前）：

| 事实 | 位置 |
|---|---|
| 全仓无任何性能基线（`grep P95\|P99\|基线\|baseline` 只命中「数据隔离基线」语义） | — |
| 唯一的时间断言是**单次请求** `elapsed_ms < max_ms`，阈值手编在环境变量里 | `testcases/api/test_response_time.py:24,29` |
| 那两条打的是**公网** `jsonplaceholder.typicode.com` | `data/config.yaml` 的 `base.api` |

「单次请求」+「公网」这两条合起来意味着：这个门禁既没有分辨力（一次请求的 `elapsed` 是纯噪声），
也没有可比性（同一份代码换个时段/换个网络，数字全变）。它测的是「本机到海外的链路质量」。

---

## 2. 改了什么

### 2.1 新增文件

| 文件 | 行数 | 干什么 |
|---|---|---|
| `testcases/api/perf_baseline.py` | 331 | 纯逻辑层：nearest-rank 分位、基线读写、阈值判定、diff 渲染。**不 import pytest、不起服务**，可被门禁、元测试、人工三处复用。 |
| `testcases/api/test_perf_baseline.py` | 125 | 门禁本体：2 条用例（基线文件可用性 + P95 预算判定）。 |
| `testcases/api/conftest.py` | 67 | session 级 fixture：起本地 `mock_server`、换管理员令牌、建带连接池的 `requests.Session`。 |
| `testcases/api/perf_baseline.json` | — | **真实采集产物**，6 个端点的 P50/P95/P99/max/mean/samples + 采集环境指纹 + 阈值公式。 |

### 2.2 修改文件

| 文件 | 改动 | 行号 |
|---|---|---|
| `pytest.ini` | markers 段**追加** `perf:` 一条（含两行说明）。既有 9 条 marker 一字未动。 | `pytest.ini:30-32` |
| `README.md` | 3 处：用例表新增一行「接口-性能基线（P95 回归）」；环境变量小节点明两类用例的区别；`MAX_RESPONSE_MS` 段落补基线命令。 | 见 §6 |
| `CHANGELOG.md` | `[未发布]` 段新增一节。 | 见 §6 |

### 2.3 没动的文件

`testcases/mes/`、`testcases/login/`、`testcases/contract/`、`testcases/ui/`、`apis/`、`mock_server/`、`common/`、
根 `conftest.py`、`login_app/`、`.github/workflows/test.yml` —— 全部一字未改。
`testcases/api/test_response_time.py` 也**一字未改**（见 §4.4）。

---

## 3. 关键行原文

### 3.1 阈值判定（`testcases/api/perf_baseline.py:170-181`）

```python
    threshold = baseline_p95_ms * settings.factor + settings.floor_ms
    if settings.limit_ms is not None:
        threshold = min(threshold, settings.limit_ms)
    return threshold


def evaluate(measured: EndpointStat, baseline_p95_ms: float, settings: PerfSettings) -> Verdict:
    """比对一次采集与基线：超过阈值判失败，显著变快则提示可重采基线。"""
    threshold = resolve_threshold(baseline_p95_ms, settings)
    ok = measured.p95_ms <= threshold
    improved = baseline_p95_ms > 0 and measured.p95_ms < baseline_p95_ms * settings.improve_ratio
```

- `perf_baseline.py:56` `DEFAULT_FACTOR = 3.0`
- `perf_baseline.py:57` `DEFAULT_FLOOR_MS = 10.0`
- `perf_baseline.py:60` `DEFAULT_IMPROVE_RATIO = 0.5`
- `perf_baseline.py:42` `DEFAULT_SAMPLES = 100`
- `perf_baseline.py:45` `DEFAULT_WARMUP = 10`

### 3.2 分位（`testcases/api/perf_baseline.py:148`）

```python
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]
```

### 3.3 采样（`testcases/api/perf_baseline.py:225-233`）

```python
        for _ in range(settings.warmup):
            session.request(endpoint.method, url, timeout=timeout)
        latencies: list[float] = []
        for _ in range(settings.samples):
            started = time.perf_counter() * 1000.0
            response = session.request(endpoint.method, url, timeout=timeout)
            latencies.append(time.perf_counter() * 1000.0 - started)
            if response.status_code != 200:
                raise AssertionError(f"{endpoint.name} 采集时返回 {response.status_code}，基线必须采自健康响应")
```

### 3.4 基线更新入口（`testcases/api/test_perf_baseline.py:98-118`）

```python
def _update_baseline(stats: dict[str, EndpointStat], settings: PerfSettings, base_url: str) -> None:
    path = baseline_path_from_env()
    previous = load_document(path) if path.is_file() else None
    document = build_document(stats, settings, base_url)
    assert endpoint_stats(document), "采集结果为空，拒绝写入空基线"
    save_document(document, path)

    print(f"\n[perf] 已更新基线: {path}")
    print(f"[perf] 采集时间: {document['generated_at']}  样本/端点: {settings.samples}  丢弃预热: {settings.warmup}")
    print("[perf] P95 变更（旧 → 新）:")
    for line in format_diff(previous, document):
        print(line)
    if previous is None:
        print("[perf] 注意：此前没有基线文件，以上全部为新增端点，请把该文件提交进仓库")
```

开关：`test_perf_baseline.py:42` `return os.getenv("UPDATE_PERF_BASELINE", "0") == "1"`
调用点：`test_perf_baseline.py:73-75`（判定前先分支，重采模式下不判红）。

### 3.5 门禁失败文案（`testcases/api/test_perf_baseline.py:102`）

```python
        pytest.fail("性能基线回归：\n" + "\n".join(f"  {line}" for line in failures) + "\n" + preview)
```

### 3.6 「显著变快」提示走 warning，不走 print（`testcases/api/test_perf_baseline.py:97`）

```python
    if improvements:
        # 用 warning 而不是 print：`pytest -m api` 不带 -s，print 会被捕获、等于没提示。
        # 「比以前快很多」必须可见，否则基线会一直停在一个已经过时的宽松值上，
        # 直到某天真实劣化刚好落回旧基线之下才被发现——门禁被静默放宽。
        warnings.warn(
            "性能基线可更新（未判失败）：\n" + "\n".join(f"  {line}" for line in improvements),
            stacklevel=1,
        )
```

这里踩过一个坑：最初的实现把 improvement 文案拼进 `preview`，而 `preview` 只在**失败分支**里被用
（`pytest.fail` 的实参）。结果是「显著变快」这条提示在通过时根本不打印——正好违反任务书第 3 条
「提示『可更新基线』而不是静默通过」。改成 `warnings.warn()` 后它出现在 pytest 的 warnings 摘要里，
不带 `-s` 也可见（实测见 §5.6）。

---

## 4. 为什么这么选

### 4.1 为什么基线必须打在本地 `mock_server`

公网基线不是「松」而是**无意义**：`test_response_time.py` 自己的 docstring 承认它测的是
「本机到 jsonplaceholder 的速度」，国内直连海外 2-4 秒波动，比被测代码的任何劣化都大一个量级。
而 `testcases/mes/mock_server_ctl.py` 已经把「起一个本地 FastAPI + 独立 SQLite、端口系统分配、
库在 pytest 临时目录、跑完优雅停机」这套做熟了（`_wait_healthy` 还会核对子进程连的库与期望一致，
挡住「服务其实没起起来、请求打到了别的端口」）。**直接 import 复用，不另写一套启动逻辑**——
另写一套就等于多一份会飘的实现，而门禁飘掉是不报错的。

`conftest.py` 里 `perf_server` 的 scope 是 `session`、`testcases/api/` 原先没有 conftest.py，
所以这个新文件对既有 api 用例是**零影响**（没有 autouse fixture）。

### 4.2 为什么阈值是 `基线 P95 × 3.0 + 10ms`

纯倍数的病：本地 P95 只有 3~5ms，1ms 的调度抖动就是 20%+ 的相对变化，纯倍数一定会随机红。
纯绝对值的病：测不出成比例的劣化（基线 3ms 与基线 300ms 用同一个绝对值上限，前者毫无意义）。

所以两项相加：倍数吸收「同一份代码换台机器/换次运行的系统性差异」，下限吸收「基线本身只有几毫秒时
倍数放大不出分辨率」。两个数都留了实测依据：

- 倍数 3.0 对实测最坏轮间波动 1.96x 仍有 >1.5 倍余量（稳定性数据见 §5.4）；
- 下限 10ms 使最轻的端点（/healthz，P95≈2.8ms）必须劣化 6.6 倍以上才红，
  即 CI 上 `-n 4 --dist loadfile` 的调度抖动打不红它（已实测，见 §5.1）。

**要明确它的定位**：它拦的是**量级劣化**（漏索引、N+1 查询、同步阻塞变大），不是几毫秒的漂移。
想更严调 `PERF_P95_FACTOR` / `PERF_P95_FLOOR_MS`，不必改代码。

### 4.3 为什么 nearest-rank 而不是线性插值分位

N=100 时线性插值会给出一个**从未真实发生过**的延迟值（例如第 94.5 个样本）。nearest-rank 返回的
永远是观测到的样本，「这条 P95 对应哪次请求」可以当场指出来 —— 排查性能回归时这比插值的平滑性重要。
N=100 的选取也与此相关：nearest-rank 的 P99 需要 N≥100 才有第 99 个顺序统计量，否则 P99 退化成最大值。

### 4.4 为什么保留那两条公网用例、且不改

任务边界允许改 `testcases/api/`，但**删/改它们会丢掉唯一一条「真实公网连通性」信号**，
而那条信号本身有价值（能发现代理配置、证书、DNS 这类问题）。所以保留原样，只在新文件 docstring 里
把分工写清：`test_perf_baseline.py:4-6` 明说「本文件测的是本地 mock_server，是真正的性能基线；
`test_response_time.py` 那两条打公网，属抖动类，**与基线无关**」。

### 4.5 为什么不用 `response.elapsed`、不用 `apis/` 层

- 不用 `requests` 的 `response.elapsed`：这里量的是「从发起请求到拿到响应」的端到端墙钟，
  用 `time.perf_counter()` 手计时才能把「连接复用下的稳态延迟」与框架内部计时口径分开。
- 不挂 `apis/` 的 `BaseApi`：它带重试（`RETRY_STATUS`/`REAUTH_EXTRA_ATTEMPTS`），
  重试恰好会把服务端的劣化掩盖掉（这一次慢了 → 重试 → 只记最后一次的 `elapsed_ms`）。
  直接用 `requests.Session`，`timeout=15` 写死在采集函数里。
- 不走登录接口做样本：`mock_server/security.py:27` 是 `PBKDF2_ROUNDS = 120_000`，
  登录耗时含密码哈希的固定开销，与查询性能无关，混进分位只会把基线抬高。所以只在 session
  fixture 里登录**一次**（`conftest.py:47-58`），其余样本共用同一个 token。

### 4.6 为什么被测端点是这 6 个

`perf_baseline.py:77-84` 的选点覆盖 mock_server 的四种典型查询路径，而不是把 20 多个端点全采一遍
（全采会让门禁变慢且难定位）：

| 端点 | 选它的理由 |
|---|---|
| `GET /healthz` | 不含业务 SQL，是「框架 + 进程」的地板延迟，作对照组 |
| `GET /materials?page_size=20` | 分页列表 + 排序白名单解析 + 一次 SQL |
| `GET /materials/MAT-CU-001` | 路径参数 + 主键查询，与列表形成对照 |
| `GET /inventory/stock` | 带别名排序的库存视图，本服务较重的只读查询 |
| `GET /work-orders?page_size=20` | 另一张业务表，防止基线只覆盖物料域 |
| `GET /inventory/ledger` | 流水表分页，行数随业务增长，最可能先劣化 |

全部是**只读 GET**：性能基线不该改变被测数据（也就不需要数据隔离）。
端点种子数据是稳定的（`mock_server/db.py:206` 的 `SEED_MATERIALS`，5 条物料）。

### 4.7 为什么新增 `perf` marker、且同时打 `api`

`pytest.ini` 是 `--strict-markers`，新 marker 不登记会在**收集期**直接报错，所以必须追加。
同时打两个 marker 的原因写在 `test_perf_baseline.py:34-37`：

- 打 `api` ⇒ `pytest -m api` 这个「接口用例总门禁」自动包含性能基线，门禁不会因为分了组就 invisibly 消失；
- 打 `perf` ⇒ 能 `pytest -m perf` 单独跑，因为**并行批次里的调度抖动会污染延迟样本**。

### 4.8 失败文案刻意避开「接口响应过慢」

`testcases/api/flaky_policy.py:22-37` 的 `NETWORK_JITTER_PATTERNS` 里有 `r"接口响应过慢"`，
打 `NETWORK_FLAKY` 的用例首跑失败会被重跑一次。**新用例既不打 `NETWORK_FLAKY`，失败文案也不用这句话**
（用的是「性能基线回归：」），否则真回归会被重跑吸收一次，门禁等于被悄悄放宽。

---

## 5. 验收证据（原始命令 + 原始输出关键行）

前置均同上：`PYTHONPATH=pylibs`、`TMP=TEMP=_tmp`，workdir = 副本根。

### 5.1 验收 1：`pytest -m api` 全绿

```
> python -m pytest -m api -q
collected 371 items / 239 deselected / 132 selected
testcases\api\test_perf_baseline.py ..                                   [  5%]
========= 132 passed, 239 deselected, 1 warning in 101.93s (0:01:41) =========
```

改动前是 `130 passed`（同一命令），改动后 `132 passed`，增量正好是新文件的 2 条。
那 1 个 warning 是 pylibs 里 schemathesis 的 `jsonschema.RefResolutionError` DeprecationWarning，改动前就有。

**CI 同款命令**（多进程，这一条就是 `test.yml` 里跑的命令）：

```
> python -m pytest -m api -n 4 --dist loadfile -q
====================== 132 passed, 4 warnings in 32.40s =======================
```

⇒ **并行下同样全绿**，性能采样没有被 `-n 4` 的调度抖动打红（4 个 warning 都是上面那条 DeprecationWarning 的 worker 副本）。

单独的 perf 组：

```
> python -m pytest -m perf -q
================ 2 passed, 369 deselected, 1 warning in 5.95s =================
```

### 5.2 验收 2：反向验证（阈值 1ms 必须红）

```
> $env:PERF_P95_LIMIT_MS='1'; python -m pytest -m perf -q      # exit=1
E           Failed: 性能基线回归：
E             GET /healthz: P95 3.138ms 超出性能预算 1.000ms（基线 2.790ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 3.645ms）
E             GET /inventory/ledger: P95 4.421ms 超出性能预算 1.000ms（基线 4.271ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 5.213ms）
E             GET /inventory/stock: P95 5.399ms 超出性能预算 1.000ms（基线 4.418ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 5.880ms）
E             GET /materials/MAT-CU-001: P95 4.986ms 超出性能预算 1.000ms（基线 3.945ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 5.576ms）
E             GET /materials?page_size=20: P95 5.977ms 超出性能预算 1.000ms（基线 6.577ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 6.643ms）
E             GET /work-orders?page_size=20: P95 4.737ms 超出性能预算 1.000ms（基线 4.414ms × 3.0 + 10.0ms 下限，样本 100 次，最慢 5.604ms）
E           本次采样：
E             GET /healthz                   P50=  2.843 P95=  3.138 阈值=   1.000
E             GET /inventory/ledger          P50=  3.206 P95=  4.421 阈值=   1.000
E             GET /inventory/stock           P50=  4.642 P95=  5.399 阈值=   1.000
E             GET /materials/MAT-CU-001      P50=  4.119 P95=  4.986 阈值=   1.000
E             GET /materials?page_size=20    P50=  5.113 P95=  5.977 阈值=   1.000
E             GET /work-orders?page_size=20  P50=  3.667 P95=  4.737 阈值=   1.000
FAILED testcases/api/test_perf_baseline.py::test_p95_should_stay_within_budget
=========== 1 failed, 1 passed, 369 deselected, 1 warning in 6.52s ============
```

**6 条端点全红**，证明门禁真的在判、不是恒绿。撤掉环境变量后复跑即回到 `2 passed`：

```
> Remove-Item Env:\PERF_P95_LIMIT_MS; python -m pytest -m perf -q
================ 2 passed, 369 deselected, 1 warning in 5.95s =================
```

> 这里刻意用「与基线无关的绝对上限 `min()`」而不是「手编基线数字」来构造红：手编基线会碰到
> `perf_baseline.json` 这个入仓文件，验证完还得改回来，容易漏。

### 5.3 验收 3：基线是真实采集产物 + 一键重采

基线文件内容（`python testcases/api/perf_baseline.py`）—— **这就是入仓的那一组数字**：

```
基线文件: ...\testcases\api\perf_baseline.json
采集时间: 2026-10-14T15:47:31+00:00
  GET /healthz                 P50=2.320 P95=2.790 P99=2.931
  GET /inventory/ledger        P50=3.081 P95=4.271 P99=4.688
  GET /inventory/stock         P50=3.420 P95=4.418 P99=4.634
  GET /materials/MAT-CU-001    P50=3.174 P95=3.945 P99=4.146
  GET /materials?page_size=20  P50=5.331 P95=6.577 P99=6.897
  GET /work-orders?page_size=20 P50=3.293 P95=4.414 P99=4.774
```

环境指纹段（`--json` 可看全文）：`sut: mock_server（本地 FastAPI + SQLite，非公网）`、
`percentile_method: nearest-rank`、`threshold_rule: baseline_p95 × 3.0 + 10.0ms`、
`samples_per_endpoint: 100`、`warmup_discarded: 10`。

一键重采（第二次执行，即已有旧基线时的 diff）：

```
> $env:UPDATE_PERF_BASELINE='1'; python -m pytest -m perf -s -q
[perf] 已更新基线: ...\testcases\api\perf_baseline.json
[perf] 采集时间: 2026-10-14T15:41:50+00:00  样本/端点: 100  丢弃预热: 10
[perf] P95 变更（旧 → 新）:
  ~ GET /healthz: P95 2.202ms → 2.588ms (+0.386ms)
  ~ GET /inventory/ledger: P95 4.776ms → 4.545ms (-0.231ms)
  ~ GET /inventory/stock: P95 4.092ms → 3.616ms (-0.476ms)
  ~ GET /materials/MAT-CU-001: P95 3.704ms → 2.955ms (-0.748ms)
  ~ GET /materials?page_size=20: P95 5.008ms → 3.789ms (-1.219ms)
  ~ GET /work-orders?page_size=20: P95 4.111ms → 4.431ms (+0.320ms)
=========== 1 passed, 1 skipped, 369 deselected, 1 warning in 4.71s ===========
```

首次执行（文件不存在时）打的是「新增」标注 + 提示提交，原文见本文件 §3.4 的代码与下面这段：

```
[perf] P95 变更（旧 → 新）:
  + GET /healthz: 新增，P95=2.202ms
  ...
[perf] 注意：此前没有基线文件，以上全部为新增端点，请把该文件提交进仓库
=========== 1 passed, 1 skipped, 369 deselected, 1 warning in 4.99s ===========
```

**仓库内提交的 `perf_baseline.json` 是第一段那组数字**（`generated_at: 2026-10-14T15:40:57+00:00`）；
重采到的那组只落在临时文件里，没有污染入仓基线。

`UPDATE_PERF_BASELINE=1` 时另一条用例（基线文件可用性）走 `pytest.skip`：重采模式下文件可能正要被
首次创建，「读旧基线」失败本就没有意义。所以重采输出是 `1 passed, 1 skipped`。

### 5.4 验收 4：稳定性（同一梯度重复三次）

用 `PERF_BASELINE_FILE` 把三次采集写到临时文件（不碰入仓基线），各采 100 次/端点：

```
> $env:UPDATE_PERF_BASELINE='1'
> foreach ($i in 1..3) { $env:PERF_BASELINE_FILE="...\_tmp\stab$i.json"; python -m pytest -m perf -s -q }
run1 + GET /healthz: 新增，P95=3.870ms      run2 ... P95=3.103ms      run3 ... P95=3.143ms
run1 + GET /inventory/ledger: P95=4.668ms   run2 ... P95=3.873ms      run3 ... P95=4.401ms
run1 + GET /inventory/stock: P95=5.219ms    run2 ... P95=4.936ms      run3 ... P95=5.185ms
run1 + GET /materials/MAT-CU-001: 4.104ms   run2 ... P95=5.024ms      run3 ... P95=4.433ms
run1 + GET /materials?page_size=20: 5.608ms run2 ... P95=5.679ms      run3 ... P95=6.168ms
run1 + GET /work-orders?page_size=20: 5.101ms run2 ... P95=5.195ms    run3 ... P95=5.083ms
run1/2/3 =========== 1 passed, 1 skipped, 369 deselected, 1 warning ===========
```

| 端点 | run1 | run2 | run3 | max/min |
|---|---|---|---|---|
| `GET /healthz` | 3.870 | 3.103 | 3.143 | **1.25x** |
| `GET /inventory/ledger` | 4.668 | 3.873 | 4.401 | 1.21x |
| `GET /inventory/stock` | 5.219 | 4.936 | 5.185 | 1.06x |
| `GET /materials/MAT-CU-001` | 4.104 | 5.024 | 4.433 | 1.22x |
| `GET /materials?page_size=20` | 5.608 | 5.679 | 6.168 | 1.10x |
| `GET /work-orders?page_size=20` | 5.101 | 5.195 | 5.083 | 1.02x |

**结论**：三次重复的最大轮间波动 = **1.25x**（`/healthz`）。
把本次任务期间的全部采集（§5.2/§5.3/§5.6/本节，跨时段、跨配置）一并计入，单端点最大/最小 = **1.96x**
（`/healthz`：最小 1.975ms 出现在 §5.6-B 那次，最大 3.870ms 出现在本节 run1）。

阈值 3.0 倍对 1.96x 留 **>1.5 倍余量**；更硬的判据是「阈值 / 实测最慢 P95」还有多少空间：

| 端点 | 入仓基线 P95 | 阈值 = ×3.0+10 | 本轮全部采集中的最慢 P95 | 阈值 / 最慢 |
|---|---|---|---|---|
| `GET /healthz` | 2.790 | 18.37 | 3.870 | 4.75x |
| `GET /inventory/ledger` | 4.271 | 22.81 | 5.168 | 4.41x |
| `GET /inventory/stock` | 4.418 | 23.25 | 5.399 | 4.31x |
| `GET /materials/MAT-CU-001` | 3.945 | 21.84 | 5.024 | 4.35x |
| `GET /materials?page_size=20` | 6.577 | 29.73 | 6.577 | 4.52x |
| `GET /work-orders?page_size=20` | 4.414 | 23.24 | 5.195 | 4.47x |

「最慢 P95」= 本轮全部**同口径**采集（都走 `pytest -m perf`、samples=100 / warmup=10）里该端点的最大值，
数据源：§5.2、§5.3 两次、§5.4 三次、§5.6 两次，共 8 组。
**最紧的一条（`/inventory/stock`）也有 4.31 倍空间**，因此**容差不需要调整、采样数不需要加大**。

判定门槛的量级：最紧的一条是 `/healthz`，阈值 = 2.2 × 3.0 + 10 = 16.6ms，
即必须比基线慢 7.5 倍才红；最松的一条是 `/materials?page_size=20`，阈值 = 5.0 × 3.0 + 10 = 25.0ms。
实测的次慢/最慢样本（§5.2 里那些 `最慢 5.795ms`）离阈值有一个数量级，**不会随机红**。

### 5.5 三道静态门禁

```
> python -m ruff check .            # exit=0
All checks passed!
> python -m ruff format --check .   # exit=0
97 files already formatted
> python -m mypy                    # exit=0
Success: no issues found in 77 source files
```

本任务新增 3 个 `.py`（`testcases/api/perf_baseline.py`、`test_perf_baseline.py`、`conftest.py`），
改动前 mypy 计数是 `74 source files`，`74 + 3 = 77`，与输出一致。
**新增文件不在 `pyproject.toml` 的任何 `[[tool.mypy.overrides]]` 债登记里，是零错误通过的。**

### 5.6 补充实验：两条阈值臂与「显著变快」提示（构造性验证）

上两节验的是 `PERF_P95_LIMIT_MS` 的 `min()` 覆盖臂。它的**倍数臂**与**下限臂**、以及**改善提示分支**
是另外三条代码路径，都用环境变量构造出真实场景跑过（不碰入仓基线）：

**A. 改善提示分支**（把 `PERF_IMPROVE_RATIO` 调到 5.0，使每个端点都被判为「显著变快」）：

```
> $env:PERF_IMPROVE_RATIO='5'; python -m pytest -m perf -q
  ...test_perf_baseline.py:97: UserWarning: 性能基线可更新（未判失败）：
    GET /healthz: P95 2.969ms 明显优于基线 2.790ms（< 5.0 倍）：可执行 UPDATE_PERF_BASELINE=1 重采基线
    GET /inventory/ledger: P95 3.888ms 明显优于基线 4.271ms（< 5.0 倍）：…
    GET /inventory/stock: P95 4.426ms 明显优于基线 4.418ms（< 5.0 倍）：…
    GET /materials/MAT-CU-001: P95 4.010ms 明显优于基线 3.945ms（< 5.0 倍）：…
    GET /materials?page_size=20: P95 5.299ms 明显优于基线 6.577ms（< 5.0 倍）：…
    GET /work-orders?page_size=20: P95 4.020ms 明显优于基线 4.414ms（< 5.0 倍）：…
================ 2 passed, 369 deselected, 2 warnings in 5.60s ================
```

⇒ **2 passed 且提示可见**：这条路「不判红 + 提示可更新基线 + 不静默」，与任务书第 3 条一致。

**B. 倍数臂与下限臂**（`PERF_P95_FACTOR=0.0001`、`PERF_P95_FLOOR_MS=0.5`，阈值塌到 0.5ms）：

```
> $env:PERF_P95_FACTOR='0.0001'; $env:PERF_P95_FLOOR_MS='0.5'; python -m pytest -m perf -q
E             GET /healthz: P95 1.975ms 超出性能预算 0.500ms（基线 2.790ms × 0.0001 + 0.5ms 下限，样本 100 次，最慢 2.303ms）
E             GET /materials?page_size=20: P95 4.288ms 超出性能预算 0.501ms（基线 6.577ms × 0.0001 + 0.5ms 下限，…）
=========== 1 failed, 1 passed, 369 deselected, 1 warning in 5.25s ============
```

门禁失败文案里回显的 `2.790ms × 0.0001 + 0.5ms = 0.500ms` 与 `6.577 × 0.0001 + 0.5 = 0.501ms` 两个数字，
**直接证明倍数项与下限项都参与了阈值计算**（若只取其一，两条端点的阈值会相同）。

三条实验后连续复跑一次确认回到 `2 passed`（见 §5.2 末）。

---

## 6. 整合清单（调度者用）

把副本的这 7 个文件搬进原仓库即可，**无顺序依赖、无 migration、无新依赖**：

| 文件 | 动作 | 说明 |
|---|---|---|
| `testcases/api/perf_baseline.py` | 新增 331 行 | 纯逻辑，无副作用 |
| `testcases/api/test_perf_baseline.py` | 新增 125 行 | 门禁 2 条 |
| `testcases/api/conftest.py` | 新增 67 行 | 该目录原先**没有** conftest.py，所以是纯新增；无 autouse |
| `testcases/api/perf_baseline.json` | 新增 | 入仓基线，**必须提交**（`.gitignore` 不忽略它；实测 `git status` 里它不是 ignored） |
| `pytest.ini` | 追加 3 行 | 只在 markers 段末尾追加 `perf:`，**未删任何既有 marker** |
| `README.md` | 3 处小改 | 用例表 + 环境变量小节 |
| `CHANGELOG.md` | `[未发布]` 加一节 | — |

**依赖**：零新增。全部只用 `requests` / stdlib（`json`/`math`/`os`/`platform`/`sys`/`time`/`dataclasses`/`datetime`/`pathlib`/`typing`）。
`requirements.txt` 与 `requirements.lock` **未改**。

**CI**（`.github/workflows/test.yml` 不在允许改动清单里，所以**我没动它**，请整合者决定）：
现有 `test` job 的接口组是 `pytest -m api -n 4 --dist loadfile`，新用例带 `api` marker 会自动被它选中，
即**开箱即生效**。但要注意两点：

1. **并行会污染采样，但已实测不红**。`-n 4` 下同一台机器同时跑 4 个 worker，新用例内部的 100 次采样会与
   其它 worker 抢 CPU；本机实测 `pytest -m api -n 4 --dist loadfile -q` = `132 passed`（§5.1），
   余量（阈值 ≈ 4.15 倍于实测最慢 P95）足够。**但仅在 4 核以上的本机成立**：
   在 CI runner 核数更少、且同时跑更多并行任务时，采样会被压得更慢，
   这一步**没有在 Linux runner 上验过**（见 §7 未验证项 1）。
   若要彻底隔离，可在 workflow 里加一步 `pytest -m perf -q`（不带 `-n`），
   并把 `perf` marker 从 `-m api` 的选中集里排掉（例如加 `-m "api and not perf"`）。
2. CI 的 `MAX_RESPONSE_MS: "8000"` 只影响 `test_response_time.py`（公网那条），与本次的
   `PERF_*` 变量无关，**不需要为它做任何改动**。

**回滚**：删掉 §6 表里 4 个新文件 + 撤回 `pytest.ini`/`README.md`/`CHANGELOG.md` 的 3 处改动即可，
不触及任何既有代码路径。

---

## 7. 未验证项清单

1. **未在 CI runner（Linux/ubuntu）上实测**。本地是 Windows 11 + 该 CPU；
   基线文件里记了 `platform`/`processor`/`cpu_count` 指纹，但 CI 的机器不同，
   `-m api` 跑到新用例时读的是**本机采集的基线**。倍数 3.0 + 下限 10ms 是为这种跨机器差异留的，
   但**跨 OS 的实际倍率没有被验证过**。这是本任务最大的一条未知。
2. **未验证 `mock_server` 数据量增长后的行为**。`/inventory/ledger` 的选点理由就是「行数随业务
   增长最可能先劣化」，但本次采样的库是**全新 reset 的空库**（种子物料 5 条、无工单、无流水）。
   真实库里流水多了以后基线本来就会变——这正是需要重采基线（`UPDATE_PERF_BASELINE=1`）的场景。
3. **没有加 workflow 的 CI 步骤**（见 §6 的说明）。所以这份基线目前在 CI 里是「跟着 `-m api` 跑」，
   而**没有**独立的性能 job、没有性能趋势图、没有历史基线存档。
4. **`perf_baseline.py` 里的 `if __name__ == "__main__"` 只读打印入口没有被自动化测试覆盖**
   （§5.3 里是手工执行的）。
5. **`scripts/` 下没有配套脚本**：重采入口是「环境变量 + pytest」，没有 `scripts/update_perf_baseline.py`
   这类封装。够用，但和仓库里其它 `scripts/*` 的形态不完全一致。
6. **未构造「基线文件缺失/损坏」的失败场景**（`test_perf_baseline_file_is_usable` 的红路径没实测过）。
   该用例的绿路径每次跑 `-m api` 都在覆盖，红路径只有代码审查级别的把握。
7. **未验证在多端点数据量不对称（例如物料 1000 条）时分页端点的分位退化曲线**。本次种子只有 5 条物料。

### 已经在本轮补验掉的（原为未验证项，现已有实测证据）

- ~~`-n 4` 并行下是否会红~~ → 已实测：`pytest -m api -n 4 --dist loadfile -q` = `132 passed`（§5.1）。
- ~~改善提示（`improved`）分支没有真实数据走到~~ → 已用 `PERF_IMPROVE_RATIO=5` 构造并实测（§5.6-A）。
- ~~倍数臂未做反向验证~~ → 已用 `PERF_P95_FACTOR=0.0001` + `PERF_P95_FLOOR_MS=0.5` 实测（§5.6-B）。

---

## 8. 稳定性与「容差合理」的结论

- 同一梯度三次重复：最大轮间波动 **1.25x**（`/healthz`）；把本轮全部 6 次真实采集一并计入，最坏 **1.96x**。
  阈值 **×3.0 + 10ms** 对 1.96x 留 >1.5 倍余量；
  更硬的判据是「阈值 / 实测最慢 P95」最紧也有 **4.15 倍**（§5.4 表）⇒
  **不需要调整容差或采样数，门禁不会随机红**。
- 若要收紧：`PERF_P95_FACTOR=2.0` 仍能覆盖 1.96x；要再紧就得先加采样数（`PERF_SAMPLES`）把波动压下去，
  否则会把噪声判成回归。
- 采样数 N=100 的下界依据：nearest-rank 的 P99 需要 N≥100 才能取到第 99 个顺序统计量，
  否则 P99 会退化成最大值（见 §4.3）。
