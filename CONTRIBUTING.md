# 贡献指南

这份文档写给**要改这个仓库的人**——包括三个月后的我自己。
它只写"这个仓库特有的、不照做就会出问题的事"，通用规范（写清晰的提交信息、加测试）
不在这里重复。

---

## 一分钟上手

```powershell
# 1. 建虚拟环境（仓库约定用 .venv，CI 与本地都认这个名字）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 装依赖
pip install -r requirements.txt

# 3. 装浏览器（只有 UI 用例需要）
playwright install chromium

# 4. 跑一遍确认环境是好的
pytest -m "not ui" -q          # 期望 509 passed, 11 xfailed（`-m "not ui"` 收集 520 条，全仓 554 条）
```

`setup.ps1` 把这四步包成了一个脚本，新机器上可以直接跑它。

---

## 改代码前必须知道的五件事

### 1. 有两组用例钉的是"当前行为"，不是"正确行为"

`-m login` 的 29 条与 `-m mes` 的 64 条里，有一部分断言刻意钉住了被测服务的
**已知缺陷**（登录服务的 6 个缺陷见 `login_app/app.py` 顶部注释与
`docs/login-contract.md` 的"契约 vs 实现"差异表）。

这意味着：**修好一个缺陷会让用例变红**，然后由人决定是改用例还是改实现。
这不是 bug，是这个仓库刻意的设计——先证明"现在的行为是什么"，
才有资格谈"应该是什么"。看到红的时候先读那份差异表，不要直接改断言让它变绿。

### 2. `conftest.py` 靠 `sys.path` 注入，不要"顺手清理"它

`conftest.py` 开头那段 `sys.path.insert` 看起来像是该被 `pyproject.toml` 或
可编辑安装替代的遗留写法。它确实不优雅，但**它有存在的理由**：
`testcases/mes/` 与 `testcases/login/` 里的用例用同级 import
（`from concurrency import ...`、`from login_server_ctl import ...`），
因为那些模块是"和用例放在一起的测试辅助件"，不是可安装的包。

改成包结构会让 import 路径变长、`conftest` 的 `pytest_plugins` 语义变化，
而收益只是"看起来更规范"。要动它，请先确保 `-m mes` 仍 64 passed、`-m login` 仍 29 passed。

后面那个 `# noqa: E402` 也是必需的（import 不在文件顶部），别删。

### 3. CI 的 `-n` 必须配 `--dist loadfile`

`pytest-xdist` 默认按**用例**分发（`--dist load`）。这个仓库**不能**用它：

实测 `-m mes -n 4 --dist load` → `64 passed, 1 error`，退出码 1（连跑 8 次里 7 次复现，
1 次侥幸全绿——**别把"这次没红"当成没问题**）。报错在
`testcases/mes/conftest.py:158` 的会话级数据隔离断言上（数据没回到基线）。
原因是数据隔离基线是 `scope="session"` 的，按用例分发会把同一个文件的用例
拆到不同 worker，"A worker 的收尾快照"对不上"B worker 造的脏数据"。

按文件分发（`--dist loadfile`）会让一个文件整体落在同一个 worker，作用域闭合。

### 4. `mock_server/*` 的覆盖率靠 launcher 才采得到，改启动方式等于把它打回 0%

它由 fixture 以 subprocess 拉起（`testcases/mes/mock_server_ctl.py`），
而 coverage 只统计自己所在进程。**波 2 · P1-3 之后这里已经不是 0% 了**（`-m mes` 下 83%），
靠的是 `scripts/service_launcher.py`：它在进程内装一个"吸收信号"的 handler 再
`runpy.run_module(<服务模块>)`，让 uvicorn 关闭时正常返回、走到 `atexit`、分片落盘
（`.coveragerc` 的 `[run] parallel = true`），由 pytest-cov 在 session 结束时自动 combine。

所以**改 `mock_server_ctl.py` 的启动方式时必须保住这条路径**：绕过 launcher 直接
`python -m mock_server.run`、或者把 `stop()` 改回直接 `terminate()`，覆盖率就会掉回 0%
而且**不会报错**——只会让门禁变成一个说谎的指标。

### 5. 环境分层用 `TEST_ENV`，默认档必须与历史行为一致

```powershell
pytest -m "not ui"                 # dev（默认）→ data/config.yaml
$env:TEST_ENV = "staging"; pytest  # → data/config.staging.yaml
$env:TEST_ENV = "prod"; pytest     # → data/config.prod.yaml
```

加载器是 `common/config.py` 的 `get_config()`。**改动它时有一条硬约束**：
`TEST_ENV` 未设置时的返回值必须与旧实现（`common/yaml_util.get_config()`
直接读 `config.yaml`）逐字节一致。这条已经有回归保护——
CI 里跑的就是默认档的 520 条用例（`-m "not ui"` 的收集数，全仓共 554 条）。

---

## 提交前

装了 pre-commit 的话（见下），`git commit` 会自动跑 ruff 与文件检查。
没装的话，手工跑这两条等价命令：

```powershell
python -m ruff check .            # 期望 All checks passed!
python -m ruff format --check .   # 期望 N files already formatted
python -m mypy                    # 期望 Success: no issues found
```

> **`traffic-replay/` 是例外**：它自带 `pytest.ini`、不受根 `ruff.toml` 管辖
> （根配置用 `extend-exclude` 排除它，理由见下），也没进 `mypy` 的 `files` 列表。
> 改到它时单独跑：
>
> ```powershell
> cd traffic-replay; python -m pytest -q   # 期望 173 passed
> ```

> **pre-commit 在本机装不上**：安装需要创建虚拟环境，受限环境会拒绝
> `ensurepip`（`PermissionError: [WinError 5]`）。`.pre-commit-config.yaml`
> 已经写好并且**只在 CI 里验证**（见 workflow 的 `pre-commit` job）。
> 装得上 pre-commit 的机器上执行 `pre-commit install --install-hooks` 即可启用。

### 提交信息格式

用 [Conventional Commits](https://www.conventionalcommits.org/)：
`<type>(<scope>): <subject>`，type 取 `feat / fix / docs / style / refactor /
perf / test / build / ci / chore / revert`。

仓库自带检查脚本，不依赖 Node：

```powershell
python scripts/check_commit_msg.py .git/COMMIT_EDITMSG
```

> 为什么不用 commitlint：本仓库全部依赖声明在 `requirements.txt`，
> `.venv` 是唯一环境来源。为了校验一条提交信息引入 npm + `node_modules`
> 不成比例。这个脚本覆盖了 commitlint 在本仓库真正会用到的规则。

---

## 分支与 PR

- 分支名：`feat/xxx`、`fix/xxx`、`chore/xxx`，与提交信息的 type 对齐。
- PR 描述按 `.github/PULL_REQUEST_TEMPLATE.md` 填。**"怎么验证的"这一栏不要留空**——
  写清你实际跑过的命令与输出，"应该没问题"不算验证。
- 涉及 `testcases/**` 断言改动的，PR 里必须说明**为什么改的是用例而不是实现**。
- CI 必须全绿才能合并。**哪些阻塞、哪些只警告，以 [`docs/质量门禁策略.md`](docs/质量门禁策略.md)
  为准**——那份文档与 `.github/workflows/test.yml` 由 `python scripts/check_gate_policy.py`
  逐条机器校验，改了 workflow 不改文档会红。一句话版：8 个 job 里 `lint` / `typecheck` /
  `pre-commit` / `commit-convention` 全阻塞，`test` 只放行 UI 那一步，`security`（pip-audit）
  整体非阻塞，但红了要有人看；`perf`（容量压测）只在手动与夜间触发、且 job 级非阻塞；
  `pages`（发布 qaplat 在线报表到 `gh-pages`）job 级非阻塞，且 PR 上只生成、不发布。

---

## 本地起被测服务

```powershell
# MES 用的 Mock 后端（FastAPI + SQLite）
python -m mock_server.run --host 127.0.0.1 --port 8000

# 登录契约用的服务（含 6 个已知缺陷）
python -m login_app.run --host 127.0.0.1 --port 5001
```

正常跑用例时**不需要**手工起它们——fixture 会自己拉起来并在结束时关掉。
手工起是为了调试（想看请求日志、想用 curl 直接打）。

`--reload` 这两个入口都**没有**提供：多次踩过 reload 与 SQLite 的
文件句柄释放打架，导致用例随机失败。

---

## 目录速查

| 路径 | 是什么 |
|---|---|
| `apis/` | 接口封装层（`requests` 会话、重试、代理回退、Allure 附件） |
| `common/` | 通用工具（YAML/JSON 处理、断言、环境分层配置加载 `config.py`） |
| `data/` | 各环境的配置档案（`config.yaml` / `config.staging.yaml` / `config.prod.yaml`） |
| `testcases/api/` | 打公网 `jsonplaceholder` 的接口用例 |
| `testcases/ui/` | Playwright 页面对象模式，打公网 `saucedemo` |
| `testcases/mes/` | 打本地 Mock 后端的业务全链路用例（含并发竞态） |
| `testcases/login/` | 钉住本地登录服务当前行为的契约用例 |
| `mock_server/` | 被测的 Mock 后端（FastAPI + SQLite，subprocess 启动） |
| `login_app/` | 被测的登录服务（含 6 个刻意保留的缺陷） |
| `ui/pages/` | Playwright 页面对象 |
| `scripts/` | 工程化脚本（提交信息校验等） |
| `qaplat/` | 平台层：门禁分层、度量库、覆盖矩阵、报表（自带用例在 `testcases/platform/`，`-m platform`） |
| `mes_web/` | 本地 Demo Web（UI 混合链路的被测站点，不依赖公网） |
| `traffic-replay/` | 流量回放（批次 2）：**独立子项目**，自带 `pytest.ini` 与 `conftest.py`，要在它自己的目录里跑 |
| `tools/` | 一次性排查/安装辅助脚本 |
| `docs/` | 设计说明、契约差异表、测试有效性说明 |
