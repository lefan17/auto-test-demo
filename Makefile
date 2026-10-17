# 一条命令入口。本仓文档全是 PowerShell（.gitattributes 给 *.ps1 锁了 CRLF），
# 但 Linux、macOS 与大多数 CI 上没有 powershell.exe。这里把 setup.ps1 与 README
# 里已经在用的命令原样收拢成 target，不发明任何新工具——"新同事照文档跑不起来"
# 不该取决于他用哪个 shell。
#
# 换行符用 LF，不跟 *.ps1 的 CRLF 走：仓库里 .gitattributes 的 `* text=auto`
# 本来就要求文本类文件在版本库里存 LF，这张表把 Makefile 当普通文本对待即可，
# 不必为它加一条 eol=crlf 的例外（CRLF 的 recipe 其实也能跑——实测 GNU make 4.4.1
# 会自己剥掉行尾的 \r——但既然没有需要保护的东西，就没理由开例外）。

# 解释器默认取 PATH 上的 python——venv 激活后它就是 venv 里的那个。
# 不想激活也可以显式传：make test PY=.venv/Scripts/python.exe
PY ?= python

.PHONY: help install lint typecheck test test-all cov gate clean

# 默认 target。不写这条，裸敲 `make` 会报 "No targets specified"，而 `make` 恰恰是
# 每个人第一次敲的那一条。
help:
	@echo "make help       列出全部 target（本行）"
	@echo "make install    安装运行期依赖 + 提示 Playwright 浏览器另装"
	@echo "make lint       ruff 静态检查 + 格式检查（两道都跑，与 CI 一致）"
	@echo "make typecheck  mypy（读 pyproject.toml 里的本仓配置，不带额外参数）"
	@echo "make test       pytest -m \"not ui\"：提交前的最小集，不含要浏览器的用例"
	@echo "make test-all   pytest：全量，含 ui 用例"
	@echo "make cov        本机全量覆盖率（不含 ui，-n 4 --dist loadfile）"
	@echo "make gate       qaplat 门禁 --layer pr（跑完与阈值比对，0 达标）"
	@echo "make clean      清覆盖率产物（不动 .coveragerc，理由见该 target 的注释）"

# 装依赖。刻意不带 -i 镜像参数：setup.ps1 里那个国内镜像是**本机**的选择，
# 写进 Makefile 会让海外 CI 反向变慢。
# Playwright 的浏览器是另一件事：pip 只装 Python 客户端，chromium 要单独下载
# （约 140MB），所以这里只提示、不代跑——install 不该顺带做一次大下载。
install:
	$(PY) -m pip install -r requirements.txt
	@echo "依赖装完。UI 用例还需要浏览器：$(PY) -m playwright install chromium"

# 两道都跑。check 管逻辑问题（未用变量、import 顺序、pytest 写法），
# format --check 管排版；只跑前者会出现"本地绿、CI 红"，因为 CI 两步都跑。
lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

# 不带任何参数。mypy 的配置全在 pyproject.toml（mypy_path、6 处逐模块 override
# 没有命令行等价物），带参数就是在绕开那份配置——绕开的后果是本地"修好了"、
# CI 依旧红。
typecheck:
	$(PY) -m mypy

# 提交前的最小集，不含 ui。ui 用例要真浏览器，其结果与机器状态强相关；
# 把它放进这里，等于让"本机没装浏览器"决定门禁红不红。
test:
	$(PY) -m pytest -m "not ui"

# 全量，含 ui。不加任何 -o 覆盖，走的就是 pytest.ini 里的默认 addopts。
test-all:
	$(PY) -m pytest

# 本机全量覆盖率。-n 4 --dist loadfile 让同一个文件内的用例留在同一个 worker：
# 会起服务器、建 SQLite 的 fixture 是按文件组织的，拆到不同进程里会重复起停，
# 数字一样但耗时翻倍。--cov=. 的分母见 .coveragerc 与本仓覆盖率文档。
cov:
	$(PY) -m pytest -m "not ui" -n 4 --dist loadfile --cov=. --cov-report=term

# 门禁的一条命令入口。qaplat gate 按层拼 -m 表达式、调 pytest、落库，再与阈值比对
# （0 达标 / 1 未达标 / 2 用法错误）。要这个 target 的理由很实在：README 里那条
# 门禁命令很长，抄错一个 marker 就是一次假绿，而假绿比红更贵。
gate:
	$(PY) -m qaplat.cli gate --layer pr

# 清覆盖率产物，供下一次干净采样。
# 用 `coverage erase` 而不是 `rm .coverage*`，两个理由：
#   1. `rm -f .coverage*` 会把 .coveragerc 一起匹配掉（.coverage + rc），删完之后
#      coverage 没有配置可读——本仓真发生过一次，事后手工还原（见 beaf73d 的提交
#      说明、docs/wave2-coverage-handoff.md:211）。
#   2. `rm` 在 Windows 的 cmd/PowerShell 里不存在。本仓的开发者主要用 Windows，
#      这条 target 在那边会直接 "rm 不是内部或外部命令"。coverage 是本仓必装的
#      依赖（pytest-cov 带的），`-m coverage erase` 三个平台都能跑，且它自己也
#      清楚「分片 + 合并结果」一共是哪些文件（.coveragerc 里 parallel = true）。
clean:
	$(PY) -m coverage erase
