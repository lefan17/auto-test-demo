# 波 2 交接：P1-2 重试尊重 `Retry-After` + 有界退避

工作副本 `C:\Users\lefan\DSH_project\接口自动化_3\work\w2-ratelimit`（隔离仓库，无 remote）。
基线 commit `44fc60d`（`docs: 波 1 的口令、契约与实测数字同步到 README 与 CHANGELOG`）。
本次产出 2 条提交：

- `4780655` `feat(api): 重试等待优先尊重 Retry-After 并加有界退避` —— 代码与用例
- 第 2 条 `docs(handoff): …` —— 本文件（自己的 SHA 不写死在这里，见 `git log --oneline`）

引用行号时请以 `4780655` 为准：本文件不参与任何代码行号的计算。

解释器固定为 `C:\Users\lefan\DSH_project\DH_1\auto-test-demo\.venv\Scripts\python.exe`，
每次跑测试/静态检查前设：

```powershell
$env:PYTHONPATH = 'C:\Users\lefan\DSH_project\接口自动化_3\work\pylibs'
$env:TMP = $env:TEMP = $env:PIP_TMPDIR = 'C:\Users\lefan\DSH_project\接口自动化_3\work\_tmp'
$env:PYTHONIOENCODING = 'utf-8'
```

**代码与用例放在同一条提交里**，不是我不想拆，而是拆了会留下一条「红」的中间提交：
`tools/check_retry.py` 的第 13-22 条断言的是改造后的行为，若先提交用例、
它在下一条提交之前一直是红的。基线 `tools/check_retry.py` 里 12 条已全绿，
所以「先加用例、看它红、再改代码、再看它绿」这条链在**同一次提交内**用
反向对照实验（第三节）给出证据，而不是靠拆提交给出。
提交信息已通过 `scripts/check_commit_msg.py`（见第五节第 9 条）。
提交信息已通过 `scripts/check_commit_msg.py`（见第五节第 9 条）。

---

## 一、改了哪些文件、每个文件为什么改

### 新增文件

| 文件 | 行数 | 为什么新建 |
|---|---|---|
| `testcases/api/test_retry_after.py` | 201 | 端到端证据。用 stdlib `http.server.ThreadingHTTPServer` 起在临时端口（`:65` `("127.0.0.1", 0)`），真实返回 `429 + Retry-After`，让请求真的过一遍 socket。`tools/check_retry.py` 那些 Mock 证明的是「分支选对了」，证明不了「这个头是从真实响应里读出来的」——`requests` 的 `CaseInsensitiveDict`、大小写、header 折行、HTTP 版本差异只在真过 socket 时才可能出现。 |
| `docs/wave2-ratelimit-handoff.md` | 本文件 | — |

### 修改文件

| 文件 | 基线 → 现在 | 改了什么、为什么 |
|---|---|---|
| `apis/base.py` | 239 → 338 行（+102/-3） | 见下面的逐条 `:行号` 表 |
| `tools/check_retry.py` | 232 → 327 行（+96/-1） | 第 13-22 条新用例，见下面的逐条表 |

`apis/base.py` 逐条：

| 行 | 改动 | 为什么 |
|---|---|---|
| `:10` / `:12` / `:14` / `:15-16` | import `math` / `random` / `Callable` / `datetime, timezone` / `parsedate_to_datetime` | `math.isfinite` 挡 NaN/Inf；`random.Random` 做实例级 RNG；`parsedate_to_datetime` 解析 HTTP-date |
| `:40-55` | 新增三个模块常量 `RETRY_BACKOFF_BASE_SECONDS = 0.5`、`RETRY_BACKOFF_JITTER_RATIO = 0.25`、`DEFAULT_MAX_RETRY_WAIT = 30.0` | 理由都写在注释里（见第二节） |
| `:59` | `__init__` 签名加第 4 个参数 `max_retry_wait: float \| None = None` | 带默认值的关键字参数，`apis/mes_api.py:101-105` 的 `super().__init__(base_url, timeout=timeout, retries=...)` 不受影响 |
| `:65-69` | `self.max_retry_wait`：显式传参 > `API_MAX_RETRY_WAIT` 环境变量 > `DEFAULT_MAX_RETRY_WAIT` | 沿用该文件既有的「传参 > 环境变量 > 默认值」优先级（对照 `:63-64` 的 timeout/retries） |
| `:70-75` | `self._sleep: Callable[[float], None] = time.sleep`、`self._rng = random.Random()` | 等待函数与 RNG 做成**实例属性**，测试注入记录器即可断言「等了多久」，不必真的等下去。**没有用 monkeypatch 打补丁全局 `time.sleep`**：那是进程级的，一旦有测试忘了还原，「生产行为被静默改短」就成了没人能发现的事。RNG 也是实例级的，否则并发用例共用一条随机流，多线程下反而制造出新的同步点 |
| `:96-142` | 新增 `_retry_after_seconds(self, response) -> float \| None` | 见第二节 |
| `:144-157` | 新增 `_retry_wait(self, attempt: int, response=None) -> float` | 有头用头值、无头退回退避+抖动，最后统一 `min(wait, self.max_retry_wait)` |
| `:211-212` | 传输失败处 `time.sleep(0.5 * (2**attempt))` → `self._sleep(self._retry_wait(attempt))` | 传输失败没有响应可读，走退避分支 |
| `:244-246` | 命中重试码处 `time.sleep(0.5 * (2**attempt))` → `self._sleep(self._retry_wait(attempt, response))` | 只有这一条路径拿得到 `Retry-After`；429 走的正是这里 |

`tools/check_retry.py` 逐条：

| 行 | 改动 | 为什么 |
|---|---|---|
| `:25` | `make_response(status_code, text="{}", headers=None)` | 新用例要给响应带头 |
| `:30-33` | `r.headers = dict(headers or {})` | **必须显式给 `{}`**：`Mock(spec=requests.Response).headers` 是个 Mock，`headers.get(...)` 会返回 Mock 而不是 `None`，于是「没有 `Retry-After` 头」这条最常见的路径反而测不到、还恒真 |
| `:232-321` | 新增 `capture_waits(...)` 辅助函数 + 第 13-22 条 | 把 `api._sleep` 换成记录器，断言的是等待序列本身 |

第 13-22 条（`tools/check_retry.py`）：

| # | 用例 | 钉住什么 |
|---|---|---|
| 13 `:254` | `Retry-After: 2` 按头值等待 | 断言 `waits == [2.0]`。退避含抖动也不到 0.625s，所以这个断言**只在真读了头时才成立** |
| 14 `:261` | HTTP-date（未来 20s）解析出约 20s | 用 `format_datetime(..., usegmt=True)` 造头，覆盖第二种格式 |
| 15 `:268` | 已过去的 HTTP-date → 等待 0 | 标准允许的「不早于这一刻重试」；不是退回退避，更不是负数睡眠 |
| 16/17 `:275` | 非法值 `abc` / `-1` 退回退避 | 断言落在 `[0.5, 0.625]`，且**不抛异常** |
| 18 `:283` | `Retry-After: 3600` 被 cap | 传入 `max_retry_wait=5.0`，断言 `waits == [5.0]`（不是 3600） |
| 19 `:291` | 无头时回到退避（回归） | 改造前后都落在 `[0.5, 0.625]`，绝不更短 |
| 20 `:298` | 传输失败的等待 | 没有响应可看，只能走退避，不该因新逻辑变短 |
| 21 `:304` | 抖动有界且非定值 | 200 次采样全部落在闭区间 `[base, base*(1+ratio)]`，且不是同一个值 |
| 22 `:314` | 两实例持有独立 RNG | 否则并发用例会共用一条随机流 |

---

## 二、`Retry-After` 解析与 cap 的设计决定及理由

**解析**（`apis/base.py:96-142`）：先 `float(value)` 试 delta-seconds，抛 `ValueError` 再
`parsedate_to_datetime(value)` 试 HTTP-date——**顺序不能反**，因为 `parsedate_to_datetime("2")`
也会抛 `ValueError`，反过来试等于多绕一圈。用 `email.utils` 而不是自己写格式化函数，
是因为 HTTP-date 有 IMF-fixdate / RFC 850 / asctime 三种历史格式，标准库三种都认
（`:103-105` 注释里写了理由）；自己写只会漏掉后两种，而**漏掉的后果是「默默退回退避」，没人会注意到**。
一个容易踩的坑：`parsedate_to_datetime` 在 Python 3.10+ 对非法输入**抛 `ValueError` 而不是返回 `None`**
（3.9 及以前返回 `None`），所以两处都要 trap，`:122` 的 `except (TypeError, ValueError)` 与
`:124` 的 `when is None` 检查缺一不可（本仓 requires-python >= 3.10，实测 `'abc'`/`'-1'`/`'2'`/`''`
四种输入都是抛异常）。
日期分支还要处理**没有时区后缀**的情况（`:129-130` 按 UTC 补上）：HTTP-date 本来就该带 GMT，
但服务端漏了后缀时若按本地时区算，本机 UTC+8 会凭空多等 8 小时。

「非正数」分两种处理，不能合并（`:136-141`）：负的 delta-seconds（`-1`）是服务端给了个不合规的数值，
我们读不懂它，退回自己的退避更安全；而过去时刻的 HTTP-date 是标准允许的表述（「不早于这一刻重试」），
既然已经过了就是立刻可以重试，返回 `0.0`。

**抖动**（`:43-48`）：只往上加（`1 + uniform(0, 0.25)`）而不是以 base 为中心上下浮动。
理由是不让等待下界低于改造前的 0.5s / 1s——否则这次改动会**悄悄变成对服务端加压**。
为什么需要抖动：并发用例会在同一刻撞上 429，没有抖动时它们在同一刻醒来、再一起撞上去，
限流窗口反被持续打满，「退避」变成了「同步重试」。

**cap**（`:50-55`）：默认 30s，可用环境变量 `API_MAX_RETRY_WAIT` 覆盖，也可用构造参数
`max_retry_wait` 注入。选 30s 的理由：单次请求的 `timeout` 默认 15s，等待超过两个 timeout 之后，
对端要么已恢复、要么这条用例本来就该红，再等只是把失败往后拖。**没有 cap 的后果是具体的**：
服务端回一个 `Retry-After: 3600` 就能把一条用例挂住一小时，而那个 3600 是服务端替**自己**算的，
不是替「跑一次回归」算的。cap 对**两条路径都生效**（`:155-157` 的 `min()` 在分支之外），
所以 attempt 涨上去之后的大退避同样会被截断。截断而不是抛异常：被限流时多等一会儿仍然比直接失败好。

---

## 三、新用例「确实会红」的证据（反向对照）

### 3.1 破坏方式

在 `apis/base.py` 的 `raw = response.headers.get("Retry-After") if response.headers else None`
（`:107`）这一行**之前**插入一行 `        return None  # SABOTAGE`，
即让 `_retry_after_seconds()` 永远返回 `None`——等价于「代码回到了改造前，不读这个头」。
只改这一个函数，不动 `_retry_wait` / cap / 抖动。

### 3.2 红的时候（原始输出）

`& $PY -m pytest testcases/api/test_retry_after.py -q --no-header`：

```
=========================== short test summary info ===========================
FAILED testcases/api/test_retry_after.py::test_delta_seconds_wins_over_exponential_backoff
FAILED testcases/api/test_retry_after.py::test_http_date_format_is_parsed - A...
FAILED testcases/api/test_retry_after.py::test_past_http_date_retries_immediately
FAILED testcases/api/test_retry_after.py::test_huge_retry_after_is_capped - A...
FAILED testcases/api/test_retry_after.py::test_post_429_respects_retry_after
FAILED testcases/api/test_retry_after.py::test_gives_up_and_returns_last_response
=================== 6 failed, 3 passed, 1 warning in 5.01s ====================
exit=1
```

6 条断言原文（都是「实际等成了退避值」）：

```
E       AssertionError: 应等 2s，实际 [0.5814464597630536]
testcases\api\test_retry_after.py:93: AssertionError
E       AssertionError: 应等约 15s，实际 [0.5414691765455997]
testcases\api\test_retry_after.py:110: AssertionError
E       AssertionError: 过去的日期应立刻重试，实际 [0.5853496346070378]
testcases\api\test_retry_after.py:123: AssertionError
E       AssertionError: 应被 cap 到 3.0，实际 [0.592090685250119]
testcases\api\test_retry_after.py:136: AssertionError
testcases\api\test_retry_after.py:172: AssertionError
testcases\api\test_retry_after.py:184: AssertionError
```

同一次破坏下 `& $PY tools\check_retry.py`：

```
  [FAIL] Retry-After: 2 按头值等待: waits=[0.6150314372267915]
  [FAIL] Retry-After 为 HTTP-date(Wed, 14 Oct 2026 11:58:18 GMT) 解析出约 20s: waits=[0.5835729276047127]
  [FAIL] Retry-After 为过去时刻 → 不等待: waits=[0.619924212152493]
  [FAIL] Retry-After: 3600 被 cap 到上限: waits=[0.5871286909831503]

结果: 18/22 通过
exit=1
```

**注意 22 条里只红了 4 条**，这是刻意设计的对照：第 16/17（非法值）、19（无头回归）、
20（传输失败）三条**本来就该继续绿**——它们在改造前后都走退避分支，
如果它们在破坏后变红，说明用例测错了东西。第 21/22（抖动、RNG）同样不受影响。
「只红了 4 条」比「全红」更能证明这 4 条钉的正是新加的那段逻辑。

### 3.3 已还原（逐字节）

从破坏前备份还原 `apis/base.py`，两次 `Get-FileHash -Algorithm SHA256`：

```
破坏前 / 还原后（两次相同）
26164DD68BD1AD7BFEA50710E129C3E7658170F06E8A0284AB67980FC029BA11  apis\base.py

SABOTAGE残留=0
git status --porcelain  → 0 行（工作树与 HEAD 逐字节一致）
git hash-object apis/base.py                 9b19913bd77567792a768a3bf25ce346543a2e8c
git rev-parse  HEAD:apis/base.py             9b19913bd77567792a768a3bf25ce346543a2e8c   ← 相同
```

还原后重跑（全部回到绿）：

```
### ruff check .            All checks passed!                        exit=0
### ruff format --check .   83 files already formatted                 exit=0
### mypy                    Success: no issues found in 66 source files exit=0
### check_retry             结果: 22/22 通过                            exit=0
### 我的新用例               9 passed, 1 warning in 4.84s               exit=0
```

（`testcases/api/test_retry_after.py` 在破坏期间一行未动，所以 3.2 里的行号
`:93/:110/:123/:136/:172/:184` 与当前文件逐行对应。）

---

## 四、端到端证据的构成

`testcases/api/test_retry_after.py` 的 `scripted_server`（`:33-73`）用 stdlib 的
`ThreadingHTTPServer` 绑 `("127.0.0.1", 0)` 拿系统分配的临时端口，按脚本依次回响应；
`state["hits"]` 计的是**服务端真实收到的请求数**（socket 另一端数的），不是 Mock 的 `call_count`。
每条用例都把 `api._sleep` 换成记录器（`:76-81`），并且**额外断言墙上时钟**——
`assert wall < 1.0` 证明等待确实没发生，否则「等待可注入」就成了一句空话。

9 条：`Retry-After: 2` → 等 2s（`wait==[2.0]`）、HTTP-date → 约 15s、
过去的日期 → `[0]`、`3600` 被 cap 到构造参数给的 3.0、非法值 → 落回退避区间、
无头 → 回归、POST 429 → 吃头值且 `was_retried=True`、重试用尽 → 返回**最后一个**响应
且 `elapsed_ms < 1000`（只有真正产出响应那一次的耗时可进 `elapsed_ms`，前面两次的等待不能累加）、
5xx 无头 → 退避且 `was_retried` 语义不变。

**没有做到的**：真实 wall-clock 等待未被验证（9 条全部注入了 `_sleep`）。
这是刻意的取舍——真等一轮的总时长会是 `2+15+0+3+1+1 ≈ 22s` 且全是空转，
CI 里没人会为一条回归用例付这个钱。补偿方式有两条：
一是每条都断言 `wall < 1.0`，二是 `tools/check_retry.py` 的第 21 条对抖动做了 200 次采样。
`_sleep` 的默认值就是 `time.sleep`（`apis/base.py:74`），生产路径没有分支绕开它。

---

## 五、自检命令与原始输出

以下全部在**当前字节**（`git status --porcelain` 为空）上重跑，逐条给 exit code。

### 1. `& $PY -m ruff check .`

```
All checks passed!
exit=0
```

### 2. `& $PY -m ruff format --check .`

```
83 files already formatted
exit=0
```

基线（`git worktree add <tmp> 44fc60d` 后在同一台机器上跑同一条命令）是
`81 files already formatted`。**+2 正好是新加的两个文件**，而且 `+1` 来自 `.md`
——`ruff format` 把仓库里被它 `include` 的 `.md`（以及 `pyproject.toml`、`ruff.toml`）
也计入文件数，所以这个数字**不是** Python 文件数，别拿它当"新增了几个 .py"的证据。
用受控实验确认过（同一份树逐次删文件重跑，`ruff format --check .` 的计数）：

| 树 | 计数 |
|---|---|
| 完整（HEAD + 本文件） | `83 files already formatted` |
| 删掉 `testcases/api/test_retry_after.py` | `82 files already formatted` |
| 删掉 `docs/wave2-ratelimit-handoff.md` | `82 files already formatted` |
| 两个都删（＝真正的基线内容） | `81 files already formatted` |
| 基线 worktree `44fc60d` 直接跑 | `81 files already formatted` |

### 3. `& $PY -m mypy`（**无参数**，走 `pyproject.toml` 的 `files`）

```
Success: no issues found in 66 source files
exit=0
```

基线（`44fc60d` 的 worktree）是 `Success: no issues found in 65 source files`，
现在 **66**。**+1 就是新增的 `testcases/api/test_retry_after.py`**：
`files` 里列了 `testcases`，所以它被算进去；
`tools/` 不在 `files` 里，所以 `tools/check_retry.py` 的 96 行新增**不归 mypy 管**。
`pyproject.toml` 的 `overrides` 段**一行未改**，没有新增任何登记债。
⚠️ **`README.md:403` 的「实测数字」里现在写的是 `Success: no issues found in 65 source files`，
本波之后应为 66**（见 6.1 第 4 条）。

### 4. `& $PY tools/check_retry.py`

```
===== BaseApi 重试策略自检 =====
  [OK] GET 500 重试到用尽: 实际请求 3 次
  ... （前 12 条与改造前逐条一致）
  [OK] Retry-After: 2 按头值等待: waits=[2.0]
  [OK] Retry-After 为 HTTP-date(Wed, 14 Oct 2026 11:57:03 GMT) 解析出约 20s: waits=[19.360588]
  [OK] Retry-After 为过去时刻 → 不等待: waits=[0.0]
  [OK] Retry-After 非法值(abc) 退回退避: waits=[0.5753626110088121]
  [OK] Retry-After 非法值(-1) 退回退避: waits=[0.5990803218974382]
  [OK] Retry-After: 3600 被 cap 到上限: waits=[5.0]
  [OK] 无 Retry-After 头时退回退避(回归): waits=[0.570763394826727]
  [OK] 传输失败走退避: waits=[0.6019716177068637]
  [OK] 抖动有界且非定值: min=0.5011 max=0.6249 取值数=200
  [OK] 每个实例持有独立 RNG: True

结果: 22/22 通过
exit=0
```

基线实测 `结果: 12/12 通过`、exit=0。**12 → 22**，且原 12 条逐条不变全绿。
（第 14 条的等待值随运行时刻变化，属正常；断言用的是 ±2s 容差。）

### 5. `& $PY -m pytest -m api -n 4 --dist loadfile`

```
====================== 125 passed, 4 warnings in 17.02s =======================
exit=0
```

基线 `116 passed`（即 `-m api` 收集数 116）。**+9 即我的新文件**，用 collect-only 交叉验证：
`--ignore=testcases/api/test_retry_after.py` → `116/354`；单独收我的文件 → `9 tests`；全仓 → `363`。

### 6. `& $PY -m pytest -m mes`

```
=============== 54 passed, 309 deselected, 1 warning in 20.90s ================
exit=0
```

### 7. `& $PY -m pytest -m login`

```
================ 29 passed, 334 deselected, 1 warning in 2.34s ================
exit=0
```

### 8. `& $PY -m pytest -m contract`

```
======== 213 passed, 139 deselected, 11 xfailed, 4 warnings in 20.58s =========
exit=0
```

`xfailed` 恒为 11（DEF-01、DEF-02a、DEF-02b×6、DEF-03、DEF-04×2），与 `README.md:445` 一致——
本次改造没碰契约层。

### 9. 提交信息校验器

**结论先说：用「文件路径参数」，不要用 `-`（stdin）。**
`scripts\check_commit_msg.py:63-72` 里两条分支的编码行为不一样：
路径分支 `path.read_text(encoding="utf-8")` 显式 UTF-8；
stdin 分支 `sys.stdin.read()` 走的是**本地化编码**（本机 GBK），
中文提交信息会被解成乱码，正则匹配到的是乱码字节，于是**误报**。

最可靠的一条（就是这次提交实际用的）：

```
& $PY scripts\check_commit_msg.py C:\Users\lefan\DSH_project\接口自动化_3\work\_msg2.txt
[commit-msg] 提交信息格式检查通过。
exit=0
```

stdin 形式在**默认控制台编码下会误报**，我实测过三种组合，结论都在这里：

| 形式 | 命令 | 结果 |
|---|---|---|
| 路径参数 | `& $PY scripts\check_commit_msg.py <file>` | 通过，`exit=0` |
| `cmd /c` + stdin 重定向 | `cmd /c "<PY> scripts\check_commit_msg.py - < <file>"` | **误报**「subject 结尾不要加句号」，`exit=1` |
| 同上 + `$env:PYTHONIOENCODING='utf-8'` | 同上 | 通过，`exit=0` |

误报不是「传输坏了」：另跑一条把 stdin 原样读成字节的打点，得到
`228 3`（228 字节、3 个 LF）——**送达的字节是完好的**，问题只在解码。
所以上一版本文档里写的「必须走 `cmd /c` 的文件重定向」是**错的**，
正确说法是「走文件路径参数，或者先把 `PYTHONIOENCODING` 设成 `utf-8`」。

⚠️ 另一个坑照旧：`Set-Content -Encoding UTF8` 在 PowerShell 5.1 下写 BOM，
校验器的首行正则直接失配；无 BOM 写入要走
`[System.IO.File]::WriteAllText($p, $s, (New-Object System.Text.UTF8Encoding($false)))`。

顺带一条：**本仓库没有装 `.git/hooks/commit-msg`**（`.git/hooks` 下只有 `*.sample`，
`Test-Path .git/hooks/commit-msg` → `False`），所以校验器**不会自动跑**，
每次都要手动执行一次上面的路径参数命令。

---

## 六、给调度者的整合清单

以下文件在我的禁改清单里，需要你落地。每条都给建议原文。

### 6.1 `README.md` —— **必须改，否则文档与代码不符**

`:404`（在「实测数字」代码块里）：

```markdown
# 改前
python tools/check_retry.py             当前 12/12；补上第 13 条（见下文「为什么这里会短暂出现一次红」）后会先变 12/13（故意红）
# 改后建议原文
python tools/check_retry.py             当前 22/22（原 12 条 + 波 2 新增 10 条）；
                                        「代理回退判据」那条缺陷用例仍未补，补上后会先变 22/23（故意红）
```

`:461`（在「为什么这里会短暂出现一次红」引用块里）：

```markdown
# 改前
> 完整步骤：该自检脚本是 `tools/check_retry.py`（每条自检都有编号注释，现有 12 条），
> 第 13 条按同一格式补在末尾即可；它钉的分支在 `apis/base.py` 的 `request()` / `_should_retry()`。
# 改后建议原文
> 完整步骤：该自检脚本是 `tools/check_retry.py`（每条自检都有编号注释，波 1 收工时有 12 条，
> 波 2 之后是 22 条），补这一条时按同一格式接在末尾即可（编号会变成 23）；
> 它钉的分支在 `apis/base.py` 的 `request()` / `_should_retry()`。
```

`:472`（环境变量清单代码块的最后一行，`API_TRUST_ENV` 那行之后）建议补一行
（`API_MAX_RETRY_WAIT` 是本波新增的；清单在 `README.md:468-473`）：

```markdown
> $env:API_MAX_RETRY_WAIT = "30"  # 单次重试等待的硬上限秒数（默认 30）
```

### 6.2 `CHANGELOG.md` —— **必须改一处编号，否则与代码不符**

`:264` 的「已知问题」段现在写的是「`tools/check_retry.py` 的第 13 条用例钉住它（当前 12/13，故意红）」。
**本波之后「第 13 条」指的是我的 `Retry-After: 2` 用例，不再是那条缺陷用例**，必须改：

```markdown
# 改前
  `tools/check_retry.py` 的第 13 条用例钉住它（当前 12/13，故意红）。
# 改后建议原文
  这条缺陷目前**还没有用例钉住**——原计划的「第 13 条」在波 2 之后已被
  `Retry-After` 的用例占用，补这条缺陷用例时应接在末尾（当前 22 条，补后为 23 条）。
```

另建议在 `## [未发布]` 段内、波 1 那三节之后追加一节（原文可直接粘贴）：

```markdown
### 新增 —— 限流退避：尊重 `Retry-After` 并加有界退避（波 2）

- **`apis/base.py` 的重试等待不再写死**：改造前两处（传输失败、命中重试码）都是
  `time.sleep(0.5 * 2**attempt)`，网关回 429 时我们照撞不误。
  现在优先读响应头 `Retry-After`，两种格式都支持：
  delta-seconds（`Retry-After: 2`）与 HTTP-date（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`），
  解析走 `email.utils.parsedate_to_datetime`（IMF-fixdate / RFC 850 / asctime 三种历史格式都认）。
  头缺失、读不懂、NaN/负值一律退回退避，不会因为一个坏头把整条请求炸掉。
- **没有该头时退回指数退避 + 有界抖动**，抖动只往上加（`[base, base*1.25]`），
  保证等待下界不低于改造前——否则「退避」会悄悄变成对服务端加压。
- **有硬上限**：默认 30s（单次 `timeout` 默认 15s，等待超过两个 timeout 后对端要么已恢复、
  要么这条用例本就该红），可用 `API_MAX_RETRY_WAIT` 或构造参数 `max_retry_wait` 覆盖。
  没有上限时，服务端一个 `Retry-After: 3600` 就能把用例挂住一小时。
- **可测试但不改生产默认**：等待函数与 RNG 是实例属性（`self._sleep` / `self._rng`），
  测试注入记录器即可断言「等了多久」，不必 monkeypatch 进程级的 `time.sleep`。
- **三条既有语义一条都没动**：只统计真正产出响应那一次的 `elapsed_ms`、
  POST 只在 429 重试（5xx 不重试，避免重放造重复数据）、
  「401 → 换令牌 → 重放一次」的额度独立于 `self.retries`。
- **承重性有红证**：把 `apis/base.py` 的 `_retry_after_seconds()` 临时改成恒返回 `None`
  → `testcases/api/test_retry_after.py` `6 failed, 3 passed`、
  `tools/check_retry.py` `18/22`；还原后 `9 passed` / `22/22`，并用两次 SHA256
  相同 + `git status --porcelain` 为空证明逐字节回到原状。
- **端到端用例用真服务**：`testcases/api/test_retry_after.py`（新增 9 条）用 stdlib
  `http.server.ThreadingHTTPServer` 起在临时端口真实返回 `429 + Retry-After`，
  请求真的过一遍 socket。Mock 证明的是「分支选对了」，
  证明不了「这个头是从真实响应里读出来的」（`CaseInsensitiveDict`、大小写、
  header 折行、HTTP 版本差异只在真过 socket 时才可能出现）。
  `tools/check_retry.py` 从 12 条扩到 22 条，原 12 条逐条不变全绿。
```

### 6.3 新增 marker / `pytest.ini` / `requirements.txt` —— **不需要改**

我的新用例只用了既有的 `pytest.mark.api`（`testcases/api/test_retry_after.py:28`），
没有引入新 marker，因此 `pytest.ini` 的 `markers` 段不用动。
依赖也一个没加：`http.server` / `threading` / `email.utils` / `random` / `math` 全是 stdlib，
`requests` / `pytest` 是既有依赖。**若另一个子代理要动 `pytest.ini`，请让它知道这一条**。

### 6.4 整合顺序（`testcases/api/**` 有并发作者）

我的新文件是 `testcases/api/test_retry_after.py`（唯一的新增），
其余 `testcases/api/**` 归另一个子代理。两者无重叠：我**没有**创建
`testcases/api/conftest.py`，也没有改 `__init__.py`。合并时按路径分开即可，
不存在同文件冲突。

---

## 七、没做到 / 不确定的地方

1. **真实 wall-clock 等待没有端到端验证**。9 条端到端用例全部注入 `api._sleep`；
   真等一轮约 22s 且全是空转。补偿见第四节（每条断言 `wall < 1.0` + 第 21 条 200 次采样）。
   如果你认为必须有一条真的睡下去，请给一个可接受的时长上限，我加一条
   `Retry-After: 1` 的真等待用例（1s）。

2. **`_retry_after_seconds` 的 HTTP-date 容差是 ±2s**（`tools/check_retry.py:264`、用例 `:110`）。
   这个容差覆盖的是「构造头 → 解析」之间流逝的时间。机器极端卡顿时可能偶发红；
   我没见过（实测偏差 19.36s / 15s 附近），但这是理论上的 flaky 面。

3. **没有覆盖 `Retry-After` 头大小写变体**（如 `retry-after`）。
   `requests` 的 `headers` 是 `CaseInsensitiveDict`，真服务端写小写也能读到，
   但我是**按能读到写的**、没有专门造一条小写头的用例。若你认为需要，
   在 `testcases/api/test_retry_after.py` 的 `scripted_server` 里直接传小写头名即可。

4. **`Retry-After` 是 HTTP-date 时只支持到「秒」粒度**，`total_seconds()` 的浮点尾巴会带进等待值
   （例如 19.360588）。功能上无影响（`time.sleep` 接受浮点），但如果你要求等待值是整洁的整数秒，
   需要在 `_retry_after_seconds` 返回前 `round()`——**我没有自作主张加**，
   因为那会让「按头值等待」与头值不再逐字对应。

5. **`cap` 的默认值 30s 是我选的，任务书没规定**。依据写在 `apis/base.py:50-54`
   （对齐单次 `timeout` 默认 15s 的两倍）。这个数字是**可以被争论的**，
   改它只需改 `DEFAULT_MAX_RETRY_WAIT` 一处（注释里的理由也要跟着改）。

6. **`API_MAX_RETRY_WAIT` 读的是启动时的环境变量**（构造 `BaseApi` 时读一次），
   **不是每请求读一次**。这与既有 `API_TIMEOUT` / `API_RETRIES` 的读法一致（`apis/base.py:63-64`），
   所以我认为是对的；但如果有人期望「用例中途改环境变量即时生效」，那是不成立的。

7. **`CHANGELOG.md` 里登记的代理回退判据缺陷（`last_error is not None`）本次刻意未修**，
   任务书明令不改——改掉会让 CHANGELOG 与代码不符。它的行号被本波推后了：
   改造前在 `apis/base.py:87`，现在在 `apis/base.py:183`
   （同一行的判据本身一字未动，只是前面的新增代码把它往下推了 96 行）。
   6.2 第 1 条给的 CHANGELOG 建议原文里**没有引用行号**，就是为了避开这个漂移。
   `README.md:456-458` 也描述了这条缺陷（同样只写判据、不写行号）。

8. **`ruff format --check` 的计数从 `81` 变成 `83`，两个新增文件都被它计入了**
   （见第五节第 2 条的受控实验表）。所以我在实现过程中一度看到的 `82` 是**中间态读数**
   （当时只有 `testcases/api/test_retry_after.py`、本文件还没落盘），
   已按最终状态更正为 `83`。另外确认过：`ruff format .` 先跑过一次，
   修掉了 `apis/base.py` 的 `max_retry_wait` 三元表达式与 `tools/check_retry.py` 的一条长 print。

9. **`README.md:399-400` 的覆盖率数字我没有重测**（要求「六条自检命令」里没有 `--cov`）。
   我新增的 201 行测试代码会略微抬高 `-m api` 的覆盖率分子，
   但 `apis/base.py` 的新分支是否被 cover 到、TOTAL 从 `74%` 变成多少，**我给不出实测值**。
   如果你要更新那两个数字，请单独跑一次 `--cov`。

10. **没有跑 pre-commit**（本机装不上）。新文件我按规则写了
    （末尾单换行、行尾无空格、LF），但**未经 hook 实测**。
    `git hash-object` 与 `HEAD:` 逐字节相同可排除行尾差异。
