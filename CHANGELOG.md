# 更新日志

本文件记录这个仓库的**真实**变更历史。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，但有一条必须说清的差异：

> **本仓库目前没有任何 git tag**（`git tag` 输出为空），所以下面**不存在版本号**。
> 与其编一个 `0.1.0` 出来假装有发布流程，不如按**提交日期**分组并附上真实 SHA——
> 这份文件里每一行的 SHA 都能用 `git show <sha>` 直接查到。
> 等真正打了第一个 tag，再把版本号补上。

---

## [未发布]

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

- **`mock_server/*` 覆盖率恒为 0%**（1358 条 statements）。需要给子进程加
  `-m coverage run --parallel-mode` 并在用例结束后 `coverage combine`。
  本机未做。这也是覆盖率门禁只能定在 40% 的根本原因。
- **`login_app/app.py` 的 6 个已知缺陷**（FIND-01~06）**有意保留**：
  契约用例钉的是"当前行为就是这样"。修好它们会让 `-m login` 变红，
  届时需要同步改用例——顺序是先看差异表，再决定改哪边。
- **`apis/base.py` 的代理回退判据有缺陷**：`last_error is not None` 记的是
  "这个请求曾经出过传输失败"，而不是"上一次尝试就是传输失败"。
  于是传输失败之后，**由 5xx 触发的重试也会绕过用户显式配置的代理**。
  `tools/check_retry.py` 的第 13 条用例钉住它（当前 12/13，故意红）。
- **mypy 未做到全仓零错误**：`--strict` 下 342 个错误。
  当前配置通过中把已知技术债**逐模块登记**在 `pyproject.toml` 里，
  而不是消灭它们。这是一份有账可查的分阶段方案，不是"已经零错误"。
- **`-m ui` 在本机沙箱无法运行**：14 条用例在 setup 阶段全失败
  （`PermissionError: [WinError 5]`，asyncio 建管道被禁），属环境限制，非用例缺陷。
