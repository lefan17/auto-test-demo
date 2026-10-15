# 波 3 · P2-3 压测/容量 —— 交付说明

> 文件名按派发者最新要求；原始任务书里写作 `wave3-load-capacity-handoff.md`，是同一份内容。
> 全文严格区分三类陈述：**【实测】**＝有原始命令与输出行；**【推断】**＝从实测往里推一步，
> 未直接测到；**【未验证】**＝本环境没条件测，接手的人必须自己确认。

---

## 1. 现状：为什么需要这一项

`testcases/mes/concurrency.py` 是**竞态判定**，不是压测。它的三个函数回答的是
"会不会超卖/丢更新"，不是"能扛多少并发"：

- `overlap_ms(spans) -> float`：两两时间区间取交集最大值
- `race_timeline(outcome, *, workers)` / `assert_requests_overlapped(spans, *, workers, elapsed_ms, action)`
- 调用方是 `testcases/mes/conftest.py:139` 的 `race` fixture，内部 `threading.Barrier(workers, timeout=30)`
  对齐启动，返回 `(results, elapsed_ms, spans)`。

所以本项新增 `perf/`：用 locust 施加**按用户数分梯度的持续负载**，给出 RPS / P50 / P95 / P99 /
错误率与**拐点**。两者不可互相替代，边界见第 5 节。

---

## 2. 做了什么（文件 : 行号）

### 2.1 `perf/locustfile.py`（198 行，新增）

| 位置 | 内容 |
|---|---|
| `perf/locustfile.py:1-22` | 文件 docstring：与 `concurrency.py` 的边界、两个 User 类的用途、并发语义 |
| `perf/locustfile.py:52` | `THINK_TIME_MS = int(os.getenv("LOAD_THINK_TIME_MS", "0"))` |
| `perf/locustfile.py:64` | `INCLUDE_REVERSE = os.getenv("LOAD_INCLUDE_REVERSE", "0").strip() not in ("", "0", "false", "False")` |
| `perf/locustfile.py:66` | `PAGE_QUERY: dict[str, int] = {"page": 1, "page_size": 20}` |
| `perf/locustfile.py:67-71` | `WRITE_BODY = {"material_code": "MAT-SC-003", "warehouse": "WH-01", "qty": "0.001"}`（可用 `LOAD_WRITE_*` 覆盖） |
| `perf/locustfile.py:120-137` | `class _AuthMixin`：优先吃 `LOAD_TOKEN_POOL` 预热令牌，无池则现场登录；**故意不继承 HttpUser**（空 User 子类会白占并发名额） |
| `perf/locustfile.py:140` | `class MesApiUser(_AuthMixin, HttpUser)`：读写混合 |
| `perf/locustfile.py:160-173` | 四个只读任务，各 `@task(2)`：`/inventory/stock`、`/materials`、`/work-orders`、`/inventory/ledger` |
| `perf/locustfile.py:176-184` | `@task(1)` `issue_material`（POST `/inventory/issue`）；`reverse_material` 仅当 `INCLUDE_REVERSE` 为真才定义 |
| `perf/locustfile.py:187-198` | `class LoginOnlyUser(HttpUser)`：只打 `POST /auth/login`，量 PBKDF2 的 CPU 天花板 |

读写权重合计 read 8 : write 2；关掉退料后是 8 : 1。

### 2.2 `perf/run_load.py`（701 行，新增）：梯度定义 / 拐点判定 / 结果落盘

| 位置 | 内容 |
|---|---|
| `perf/run_load.py:50-54` | `DEFAULT_USERS = "5,10,20,50"`、`DEFAULT_RUN_TIME = "20s"`、`DEFAULT_REPEAT = 2`、`DEFAULT_PORT = 8321`、`DEFAULT_USER_CLASSES = "MesApiUser"` |
| `perf/run_load.py:58-62` | 拐点判据常量：`P95_DEGRADE_RATIO = 2.0` / `P99_ABSOLUTE_MS = 1000.0` / `ERROR_RATE_LIMIT = 0.01` / `SERVER_ERROR_LIMIT = 0` / `RPS_SATURATION_GAIN = 0.20` |
| `perf/run_load.py:232-280` | `_start_service()`：起被测服务（走 `scripts/service_launcher.py`）、`--concurrency-window-ms 0`、`/readyz` 就绪等待 |
| `perf/run_load.py:283-298` | `_warm_token_pool()`：预热令牌，避免 N 个用户同时跑 PBKDF2 造 CPU 风暴 |
| `perf/run_load.py:370-463` | `_run_one()`：拼 locust 命令、抓 `Aggregated` 原始行、解析 CSV |
| `perf/run_load.py:505-568` | `_detect_knee()`：三类拐点判定 |
| `perf/run_load.py:655-658` | 落 `samples.jsonl`（逐轮明细） |
| `perf/run_load.py:691-692` | 落 `summary.json`（梯度聚合 + 拐点 + 回收证据） |

拐点判定三条独立结论（`perf/run_load.py:527-567` 原文）：

```python
baseline = gradients[0]
base_p95 = float(baseline["p95_ms"])
for grad in gradients[1:]:
    users = grad["users"]
    p95 = float(grad["p95_ms"])
    p99 = float(grad["p99_ms"])
    reasons = []
    if base_p95 > 0 and p95 > base_p95 * P95_DEGRADE_RATIO:
        reasons.append(f"P95 {p95:.0f}ms > {baseline['users']}u 的 {base_p95:.0f}ms × {P95_DEGRADE_RATIO}")
    if p99 > P99_ABSOLUTE_MS:
        reasons.append(f"P99 {p99:.0f}ms > {P99_ABSOLUTE_MS:.0f}ms")
    if reasons:
        verdict["latency_knee_users"] = users
        ...
for grad in gradients:  # 稳定性拐点单列，含最低档
    err = float(grad["error_rate"])
    faults = int(grad.get("server_faults") or 0)
    reasons = []
    if faults > SERVER_ERROR_LIMIT:
        reasons.append(f"服务端故障 {faults} 次（5xx / 连接被重置），阈值 {SERVER_ERROR_LIMIT}")
    if err > ERROR_RATE_LIMIT:
        reasons.append(f"总错误率 {err * 100:.2f}% > {ERROR_RATE_LIMIT * 100:.0f}%")
for prev, grad in zip(gradients, gradients[1:], strict=False):  # 吞吐饱和
    gain = (float(grad["rps"]) - prev_rps) / prev_rps if prev_rps else 0.0
    if gain < RPS_SATURATION_GAIN:
        verdict["throughput_plateau_users"] = grad["users"]
```

**稳定性拐点为什么不和延迟拐点合并**（`perf/run_load.py:541-543` 注释原文）：
两者本仓库实测**不同档**（P95 在 10u 就翻倍，服务端 5xx 到 50u 才出现），合并会丢掉
"哪一档开始出错"这个对容量规划更硬的信息。

**为什么另设 `SERVER_ERROR_LIMIT = 0`**：50u 的总错误率只有 0.02%~0.06%，按 1% 门限
"看着完全健康"，但其中是服务端 500（`database is locked`）。总错误率会把它稀释掉，
所以把 5xx/连接重置单独数出来，阈值取 0。`_parse_server_faults()`（`perf/run_load.py:348`）
按 `_failures.csv` 的 `Occurrences` 列**求和**而不是数行，同一错误在 CSV 里聚合在一行。

### 2.3 `perf/run_load.ps1`（98 行，新增）：一键入口

`param` 在 `perf/run_load.ps1:17-27`；PYTHONPATH/PYTHONUTF8 注入在 `:46-56`；转发给 runner 在 `:80-94`。

### 2.4 `requirements-perf.txt`（新增，1 行有效内容）

```
locust==2.46.7
```

**为什么故意不写进 `requirements.txt`**（文件头注释原文要点）：`requirements.txt` 是
每个 CI job 都 `pip install` 的**门禁依赖集**。locust 会拖进 gevent / geventhttpclient /
pyzmq / pywin32 一整套，其中 gevent 会 monkey-patch 标准库。"让所有单元测试与 UI 测试
跑在一个被 monkey-patch 的运行时上"是给测试环境引入压测工具的风险，代价远大于收益。
要跑压测的人显式装这一份即可。

---

## 3. 容量结论（**【实测】**）

### 3.1 复现命令（验收第 1 条）

```powershell
cd C:\Users\lefan\DSH_project\接口自动化_3\work\w3-load
pwsh -File perf/run_load.ps1 -Users "5,10,20,50" -RunTime 20s -Repeat 2 -Port 8321 -Label final-a
```

这条命令从零做完：起被测服务（`--reset` 重建库、`--concurrency-window-ms 0`）→ 4 个梯度 ×2 次
发压 → 落 `reports/perf/<时间戳>-final-a/` → 回收服务进程。非 Windows 或要自己指定解释器时
用底层入口（runner 自己会拼 PYTHONPATH 与 PYTHONUTF8，不依赖调用者记得）：

```powershell
python perf/run_load.py --python <解释器> --users 5,10,20,50 --run-time 20s --repeat 2 --port 8322 --label final-b
```

### 3.2 两批实测数字

口径：ADMIN 角色、`MesApiUser`、`wait_time=constant(0)`（用户数≈在途请求数，量的是服务端
吞吐上限）、`--concurrency-window-ms 0`、每轮服务 `--reset`、单轮 20s。

**批次 A**（`reports/perf/20261014-235810-final-a/`，端口 8321，经 ps1 一键）：

| 并发 | RPS | P50 (ms) | P95 (ms) | P99 (ms) | 错误率 | 服务端故障 |
|---|---|---|---|---|---|---|
| 5 | 340.6 | 13.5 | 23.5 | 31.0 | 0.00% | 0 |
| 10 | 298.6 | 29.0 | 65.0 | 78.5 | 0.00% | 0 |
| 20 | 335.4 | 50.5 | 115.0 | 145.0 | 0.00% | 0 |
| 50 | 357.9 | 104.0 | 230.0 | 695.0 | 0.02% | **2** |

**批次 B**（`reports/perf/20261015-000114-final-b/`，端口 8322，裸 `run_load.py`）：

| 并发 | RPS | P50 (ms) | P95 (ms) | P99 (ms) | 错误率 | 服务端故障 |
|---|---|---|---|---|---|---|
| 5 | 419.1 | 10.5 | 22.0 | 30.0 | 0.00% | 0 |
| 10 | 377.4 | 22.5 | 53.5 | 66.5 | 0.00% | 0 |
| 20 | 371.4 | 44.5 | 104.0 | 125.0 | 0.00% | 0 |
| 50 | 338.9 | 105.0 | 205.0 | 970.0 | 0.06% | **6** |

两批都是 4 梯度 × 2 重复取中位数，`p95_spread_ratio` / `rps_spread_ratio` 即同梯度两次的
相对波动（见 3.4）。

### 3.3 locust `Aggregated` 原始行

列序为 `# reqs | # fails(%) | Avg | Min | Max | Med | req/s | fails/s`，与 CSV 的
`50%` 列对应 `Med`、`95%`/`99%` 列来自 CSV 的百分位列（locust 自带 JSON 只有 P95/P99，
**没有 P50**，这也是取数走 CSV 不走 `--json-file` 的原因之一；另一个原因是
`--json-file` 会抑制控制台表格，`locust/main.py:654-661`）。

批次 A：
```
Aggregated                                                                      6782     0(0.00%) |     14       2     108     13 |  349.57        0.00
Aggregated                                                                      6393     0(0.00%) |     15       3     101     14 |  328.05        0.00
Aggregated                                                                      5716     0(0.00%) |     33       6     221     29 |  293.51        0.00
Aggregated                                                                      5840     0(0.00%) |     33       6     573     29 |  300.68        0.00
Aggregated                                                                      6348     0(0.00%) |     61       8    1249     52 |  325.53        0.00
Aggregated                                                                      6603     0(0.00%) |     58       8    1035     50 |  342.75        0.00
Aggregated                                                                      6771     0(0.00%) |    140      32    7440    110 |  346.02        0.00
Aggregated                                                                      6470     2(0.03%) |    141       4   11062     98 |  368.35        0.11
```

批次 B：
```
Aggregated                                                                      8386     0(0.00%) |     11       2      76     10 |  429.90        0.00
Aggregated                                                                      7866     0(0.00%) |     12       2      87     11 |  403.18        0.00
Aggregated                                                                      7270     0(0.00%) |     26       3     126     23 |  372.77        0.00
Aggregated                                                                      7436     0(0.00%) |     25       3     154     22 |  383.60        0.00
Aggregated                                                                      6790     0(0.00%) |     56       7    1232     47 |  349.47        0.00
Aggregated                                                                      7642     0(0.00%) |     50       9     903     42 |  392.02        0.00
Aggregated                                                                      6828     6(0.09%) |    136       2   11011    100 |  349.91        0.31
Aggregated                                                                      6387     2(0.03%) |    144       5   10989    110 |  326.26        0.10
```

`Error report` 段（只在有失败时出现，`locust/stats.py:923-930`）。批次 B 50u r1
（`reports/perf/20261015-000114-final-b/u050-r1.locust.log:57-63`）：

```
Error report

# occurrences      Error
------------------|---------------------------------------
3                  POST POST /inventory/issue: HTTPError('500 Server Error: Internal Server Error for url: POST /inventory/issue')
```

同时段 `u050-r1_failures.csv` 里还有 2 条 `ConnectionResetError(10054, ...)`（`/inventory/stock`）
与 1 条（`/materials`）—— 供应商侧连接被重置也是服务端顶不住的信号，因此与 5xx 一起计数。

### 3.4 拐点与判定依据（**【实测】**）

`summary.json` 的 `knee` 字段原样输出（批次 B）：

```json
{"criteria": {"p95_degrade_ratio": 2.0, "p99_absolute_ms": 1000.0, "error_rate_limit": 0.01,
              "server_error_limit": 0, "rps_saturation_gain": 0.2},
 "latency_knee_users": 10, "latency_knee_reason": "P95 54ms > 5u 的 22ms × 2.0",
 "stability_knee_users": 50, "stability_knee_reason": "服务端故障 6 次（5xx / 连接被重置），阈值 0",
 "throughput_plateau_users": 10,
 "throughput_plateau_reason": "10u 的 RPS 相对 5u 只涨了 -10.0%（419.1 → 377.4），低于 20% 判据"}
```

批次 A 同构：`latency_knee_users=10`（`P95 65ms > 5u 的 24ms × 2.0`）、
`stability_knee_users=50`（`服务端故障 2 次`）、`throughput_plateau_users=10`
（`-12.3%（340.6 → 298.6）`）。

**结论（两批一致）**：

1. **吞吐在 10 并发就到顶**。5u→10u 的 RPS 不是变平，而是**掉**了 10%~12%（A 340.6→298.6，
   B 419.1→377.4）；10u→20u→50u 也再没超过 5u 的水平（A: 298.6/335.4/357.9 对 340.6；
   B: 377.4/371.4/338.9 对 419.1）。加人不加吞吐，判定饱和。
2. **延迟拐点在 10 并发**。P95 从 22~24ms 跳到 53~65ms，正好越过"最低档 × 2"的判据线。
   50u 时 P99 到 695~970ms（**接近 1s 判据线但未破**），P50 也从 10~14ms 涨到 104~105ms
   —— 长尾和中心值一起抬，说明不是个别慢请求。
3. **稳定性拐点在 50 并发**。5u/10u/20u 服务端故障为 0，50u 出现 2~6 次
   500 / 连接重置，服务日志对应 `sqlite3.OperationalError: database is locked`，
   栈顶是 `mock_server/db.py:296 conn.execute("BEGIN IMMEDIATE")`（批次 B 服务日志 8 次，
   批次 A 2 次）。总错误率只有 0.02%~0.06%，**只看错误率会误判为健康**，所以这一档的
   判定用的是"5xx 出现次数 > 0"。
4. **三个拐点不在一档**（10 / 10 / 50），这正是把三者分开报而不是合成一个"最大并发"的原因。

**单机容量结论（实测口径）**：这台 i5-1240P（16 逻辑核）、压测机与被测服务同机时的稳态吞吐
约 **350~420 req/s**，P95 可接受的并发档位是 **5**（P95 ≈ 22~24ms）；**10 并发起 P95 翻倍、
吞吐不再增长**；**50 并发起服务端开始出 5xx（SQLite 写锁）**。

### 3.5 重复性证据（验收第 4 条）

同一梯度跑两次的相对波动（`summary.json` 的 `p95_spread_ratio` / `rps_spread_ratio`，
即 `|r1-r2| / 中位数`）：

| 并发 | A: P95 波动 | A: RPS 波动 | B: P95 波动 | B: RPS 波动 |
|---|---|---|---|---|
| 5 | 4.3% | 6.3% | 9.1% | 6.2% |
| 10 | 3.1% | 1.9% | 1.9% | 3.1% |
| 20 | 8.7% | 4.6% | 11.5% | 11.7% |
| 50 | 8.7% | 5.7% | 4.9% | 6.6% |

**批次内波动 ≤ 11.7%** —— 结论（拐点落在哪一档）在两批之间稳定复现。

⚠️ 但**跨批次**同梯度差异更大：5u 的 RPS 是 340.6（A）对 419.1（B），差 23%；20u 的
P95 波动 12.3%。所以绝对 RPS 有批次间噪声（后台进程、CPU 频率、WSL/杀软等），
**结论要读趋势与拐点档位，不要读绝对数字**。

### 3.6 服务进程回收（验收第 5 条）

`summary.json` 的 `service_shutdown`（两批相同）：

```json
{"port": 8322, "graceful": true, "terminated": false, "killed": false, "exit_code": 0, "port_free_after": true}
```

- `graceful=true` 且 `terminated=false, killed=false`：是**真的**优雅关闭，不是强杀后记一笔。
  这里曾经撒过谎：原实现写 `send_signal(signal.SIGBREAK)`，而 `subprocess.Popen.send_signal`
  只认 `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`/`SIGTERM`，传 `SIGBREAK` 抛
  `ValueError: Unsupported signal: 21` 被 `except: pass` 吞掉，于是每次都掉到 `terminate()`
  （Windows 上是 `TerminateProcess`，不跑 shutdown），`exit_code` 却是 1 而 `graceful` 记成 True。
  现在改用 `signal.CTRL_BREAK_EVENT`，且证据字典是 `{graceful, terminated, killed}` 四态。
- `exit_code=0`：**必须**经 `scripts/service_launcher.py` 起服务才能拿到。直接
  `python -m mock_server.run` 实测 exit=3（uvicorn 关闭时把原 handler 装回去再
  `signal.raise_signal` 重发，默认处置直接终止进程），经 launcher 吸收重发信号后走完
  正常 shutdown 返回 0。原因写在 `perf/run_load.py:235-239`。
- `port_free_after=true`：`_port_is_free()`（`perf/run_load.py:157-178`）循环 12 次 × 0.25s
  用 `bind` 判定，任一成功即算释放。**不能用非阻塞 `connect_ex`**：TIME_WAIT 上它返回
  10035(WSAEWOULDBLOCK) 会被误判成"仍被占用"。
- 事后核对：`netstat -ano | Select-String ":8321|:8322" | Select-String "LISTENING"` 无输出
  （只剩客户端侧 TIME_WAIT），说明没有僵尸 uvicorn 占端口。**【实测】**

---

## 4. 局限：压测机与被测服务同机（**必读**）

`perf/run_load.py` 把这条写进了 `summary.json` 的 `environment.note`：
`"压测机与被测服务同机（127.0.0.1）"`。

两个方向相反的偏差同时存在：

**让它偏乐观的**（**【推断】**，未单独测过对照环境）：
- locust 与被测服务走 loopback，没有网卡、交换机、真实 RTT（本机 ≈0.05ms vs 真实部署 0.5~5ms）。
  真实跨网部署的 RPS 不会比这里**更高**。
- 没有网关/负载均衡/连接复用上限带来的额外失败。

**让它偏悲观的**（**【推断】**）：
- locust（gevent，50 个 greenlet 所在进程）与 uvicorn(uvloop) 抢同样的 16 个逻辑核，
  压测机自己的 CPU 消耗会挤占被测服务。真实部署里压测机是独立机器。
- SQLite 库文件与压测进程同一块盘。

**净结论**：这份数字应当读作**同机部署（单机跑服务 + 同机发压）的容量**，
- 绝对 RPS 350~420 是**乐观上界**：真实跨网部署只会更低；
- P95/P99 是**乐观下界**：真实网络下只会更差；
- **拐点档位（10 并发延迟恶化、50 并发出 5xx）同样是乐观的**：独立压测机下这两个档位
  可能提前，不会推后。

**要拿到可用于生产规划的容量数，必须换独立压测机 + 真实网络**，或至少：
① 把 locust 跑到另一台机器（`--host` 指向被测机 IP）；② 加 `LOAD_THINK_TIME_MS`
模拟真实思考时间（默认 0 量的是吞吐上限，不是业务流量）；③ 拉长单轮时长到 ≥5 分钟
观察是否有随时间劣化（本批只有 20s/轮）。这三条本环境都**没做**，列为未验证项。

---

## 5. 与 `testcases/mes/concurrency.py` 的边界（压测不替代竞态判定）

| 维度 | `testcases/mes/concurrency.py`（竞态判定） | `perf/`（容量压测） |
|---|---|---|
| 问的问题 | 会不会超卖 / 丢更新 / 两个请求互相踩 | 能扛多少并发；RPS 何时到顶、P95 何时恶化 |
| 负载形态 | `threading.Barrier` 对齐的**一次性脉冲**，并发 = 2~N 个线程 | locust 持续的、按用户数分梯度的负载，20s/轮 |
| 判据 | `spans` 时间线是否交叠（`overlap_ms`）+ 业务不变量（余额/流水） | RPS / P50 / P95 / P99 / 错误率 / 5xx 次数 对阈值 |
| 产出 | 布尔结论 + 时间线证据 | 容量数字表 + 拐点档位 |
| 敏感度 | 2 个并发就能红 | 50 并发也可能全绿（竞态没被踩中 ≠ 不存在） |
| 跑在 | pytest（`-m mes`，CI 每次提交都跑，阻塞门禁） | `perf/run_load.ps1`（手动/夜间，**不进阻塞门禁**） |

**两者不可互相替代的原因（本任务实测到的直接证据）**：设计混合场景时带上
`POST /inventory/reverse`，10 并发下稳定出 400（`u010-r1_failures.csv`：
`HTTPError('400 Client Error ... POST /inventory/reverse')`，13/3011 = 0.43%），
根因是 `mock_server/services.py:636` `if row["issued_milli"] < qty:` 的
check-then-act，且 `reverse_material`（`mock_server/services.py:627`）
**没有** `concurrency_window()` 保护（唯一调用点在 `issue_material`）。

也就是说：**压测的"错误率"会把竞态混进容量数字里**。所以默认关掉退料
（`LOAD_INCLUDE_REVERSE=0`，`perf/locustfile.py:64`），把"竞态"留给竞态用例去判；
要退料接口的**延迟**数字时用 `LOAD_INCLUDE_REVERSE=1` 单独采集，并在报告里说明
那一批的错误率含竞态。这是纪律，不是省事。

---

## 6. 不进 CI 阻塞门禁，以及手动 / 夜间触发命令

**默认不进阻塞门禁**，理由：单批 4 梯度 ×2 次 ×20s ≈ 200s 发压 + 起停服务，且数字有
10%+ 的批次间噪声（见 3.5）—— 拿它当 PR 门禁等于给每个 PR 加一个"随机红灯"。

`perf/run_load.ps1` 的进程退出码：`0`=跑完；`2`=缺 locust 依赖或端口被占；`1`=运行中出错。

```powershell
# 手动：完整批次（4 梯度 × 2 次 × 20s）
pwsh -File perf/run_load.ps1

# 手动：快速验证链路（约 20s）
pwsh -File perf/run_load.ps1 -Users "5,10" -RunTime 10s -Repeat 1 -Port 8331 -Label quick

# 手动：只看批次计划，不发压
pwsh -File perf/run_load.ps1 -ListOnly

# 手动：压登录接口（PBKDF2 120k rounds 的 CPU 天花板）
pwsh -File perf/run_load.ps1 -UserClasses LoginOnlyUser -Label login -Port 8341

# 解释器：默认仓库 .venv，可用 -Python 或 $env:PERF_PYTHON 覆盖；
# 缺依赖时脚本会打印：& "<python>" -m pip install -r requirements-perf.txt
```

**夜间 job**：`.github/workflows/test.yml` 新增非阻塞 job `perf-nightly`
（`continue-on-error: true`，且 `if` 限定 `workflow_dispatch` / `schedule`，push/PR **不触发**），
跑同样的 4 梯度批次并上传 `reports/perf/` 为 artifact。触发方式＝在 Actions 页面手动
`Run workflow`，或跟现有 `schedule: cron "0 18 * * 1"` 一起在周一 UTC 18:00 自动跑。

---

## 7. 整合清单（改了什么、各改了什么）

| 文件 | 状态 | 改了什么 |
|---|---|---|
| `perf/locustfile.py` | **新增** 198 行 | 两个 User 类（读写混合 / 只登录）、令牌池、退料开关、与竞态用例的边界 docstring |
| `perf/run_load.py` | **新增** 701 行 | 起服务 → 多梯度发压 → 解析 locust CSV → 三类拐点判定 → 落 `samples.jsonl` / `summary.json` → 回收服务（含证据） |
| `perf/run_load.ps1` | **新增** 98 行 | 一键入口：选解释器、注入 PYTHONPATH / PYTHONUTF8、探针 locust 可用性、转发参数 |
| `requirements-perf.txt` | **新增** | 仅 `locust==2.46.7`，故意不并入 `requirements.txt`（理由见 2.4） |
| `docs/wave3-load-handoff.md` | **新增** | 本文件 |
| `README.md` | 修改 | 新增「容量压测」小节：与竞态用例的分工、一键命令、本机实测数字、同机局限。未改动任何既有小节 |
| `CHANGELOG.md` | 修改 | `## [未发布]` 下新增 `### 新增 —— 容量压测与拐点判定（波 3）` |
| `.github/workflows/test.yml` | 修改 | 新增 job `perf-nightly`（`continue-on-error: true`，仅 `workflow_dispatch`/`schedule`，上传 `reports/perf/`）。既有 6 个 job 一行未动 |
| `pyproject.toml` | **未改** | `[tool.mypy] files` 不含 `perf`，故裸 `python -m mypy` 不检查 `perf/`。**建议**合入时把 `"perf"` 加进该列表（本任务把它列为可动边界之外，未擅自改；证据命令用 `python -m mypy perf`） |

没有改动：`testcases/`、`apis/`、`mock_server/`、`common/`、根 `conftest.py`、`pytest.ini`、
`login_app/`、`scripts/`、`requirements.txt`、`requirements.lock`。

---

## 8. 静态检查与自检原始命令

```powershell
cd C:\Users\lefan\DSH_project\接口自动化_3\work\w3-load
python -m ruff check .            # exit=0
python -m ruff format --check .   # exit=0
python -m mypy                    # exit=0（覆盖 pyproject 里的 files 列表）
python -m mypy perf               # exit=0（裸 mypy 不含 perf，这条才是 perf 的证据）
```

ruff 版本必须与 CI 一致：`ruff 0.16.10`（`.github/workflows/test.yml:60` 钉的版本）。

---

## 9. 未验证项清单（接手的人请自己确认）

1. **CI（ubuntu-latest）上 `perf-nightly` 从未真跑过**。本机是 Windows，`perf/run_load.ps1`
   是 PowerShell 脚本、CI 用的是裸 `python perf/run_load.py`；locust 在 Linux + gevent 下的
   行为、以及 SQLite `busy_timeout` 在 CI runner 磁盘上的表现都**未验证**。job 设了
   `continue-on-error: true`，即使红也不会拦流水线。
2. **`mypy` 未覆盖 `perf/`**：`pyproject.toml` 的 `[tool.mypy] files` 不含 `perf`。
   本机用 `python -m mypy perf` 单独验证通过（2 source files）。
3. **locust 的可获得性**：本机 locust **只**存在于 `C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs`
   （`pip --target` 安装，2.46.7），仓库 `.venv` 与系统 Python 里都没有。
   `perf/run_load.py`（`_py_libs_dir()` / `_pythonpath()`）与 `perf/run_load.ps1` 都会自动
   把它拼进 PYTHONPATH，所以**同一台机器**上两处路径不同不影响复现；换机器要么
   `pip install -r requirements-perf.txt`，要么设 `LOAD_PYLIBS`／`-Python`。
   已验证可用的解释器只有：`C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`
   （另一个 `C:\Users\lefan\AppData\Local\Programs\Python\Python312\python.exe` 会因
   `locust/env.py:17 from .web import WebUI` 断链失败，`work\.venv` 缺 urllib3）。
4. **同机局限的量化**：第 4 节的方向判断是**推断**，没有跑过"独立压测机 / 真实网络"的对照
   批次，所以"相差多少"没有数字。
5. **拐点在 >50 并发、以及长时段下的表现**：只测到 50u、单轮 20s。50u 的 P99 已到 695~970ms
   （判据线 1000ms），**60~80u 何时破线、以及是否有随时间的劣化（内存/连接泄漏）未测**。
6. **`--concurrency-window-ms 0` 对结论的影响面**：它只关掉 `issue_material` 里那个教学用
   人工 sleep（`mock_server/services.py:104-107`），没有改被测代码；但"关掉窗口后
   `issue_material` 的真实并发行为是否与窗口开启时一致"未单独验证 —— 压测里 500 来自
   SQLite 写锁而不是竞态窗口，这一点有日志证据，但两者叠加时的交互未隔离。
7. **退料场景（`LOAD_INCLUDE_REVERSE=1`）的容量数字未采集**：只跑过默认关闭的批次。
8. **`LoginOnlyUser` 场景的容量数字未采集**：类已实现（`perf/locustfile.py:187`），
   但本批只跑了 `MesApiUser`；`-UserClasses LoginOnlyUser` 这条命令**未实跑**。

---

## 10. 三类陈述的分布（照任务书要求明确区分）

- **【实测】有原始输出**：3.2 两批梯度数字、3.3 `Aggregated`/`Error report` 原始行、
  3.4 三类拐点及其 `knee` JSON、3.5 重复性波动比、3.6 回收证据（`exit_code=0` /
  `port_free_after=true` / netstat 无 LISTENING）、`database is locked` 8 次（批次 B）与
  2 次（批次 A）、退料 400 的 `failures.csv` 行、4 个静态检查的 exit=0。
- **【推断】往里推一步**：第 4 节同机偏差的方向与净结论；"SQLite 写锁是 50u 拐点的第一因"
  （有 `database is locked` 栈证据，但未做"去掉写负载只跑读"的对照批次）；
  "RPS 在 10u 饱和是因为服务端连接/线程模型而非压测端瓶颈"（未测压测端 CPU 占用）。
- **【未验证】**：第 9 节全部 8 条。
