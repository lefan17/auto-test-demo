# 接口 + UI + 业务全链路自动化测试框架

[![Auto Test](https://github.com/lefan17/auto-test-demo/actions/workflows/test.yml/badge.svg)](https://github.com/lefan17/auto-test-demo/actions/workflows/test.yml)

基于 Python + pytest 的自动化测试框架，覆盖 **接口自动化**、**UI 自动化** 与
**带本地 Mock 后端的业务全链路测试**（状态机、乐观锁、幂等、并发竞态、Decimal 金额精度），
支持数据驱动、JSON Schema 契约校验、Allure 报告和 GitHub Actions 持续集成。

## 技术栈

| 用途 | 选型 |
|---|---|
| 测试框架 | pytest 8 + pytest-xdist（并行）+ pytest-cov（覆盖率） |
| 接口测试 | requests + jsonschema + PyYAML |
| UI 测试 | Playwright 1.48（sync API） |
| Mock 后端 | FastAPI + SQLite（WAL 模式），仅测试用 |
| 报告 | Allure |
| 持续集成 | GitHub Actions |

## 分层设计

```
apis/         接口封装层   —— BaseApi 统一超时/重试/日志/Allure 步骤，
                            UserApi / PostApi / MesApi 定义业务接口
testcases/    用例层       —— 只写业务断言，不出现 requests 和 selector
  api/        接口用例（打公网练习站 jsonplaceholder）
  ui/         UI 用例（打 saucedemo）
  mes/        业务全链路用例（自己起本地 Mock 服务 + 独立 SQLite）
ui/pages/     Page Object  —— 元素定位与页面操作
mock_server/  Mock 后端    —— 工单状态机 / 库存 / 权限 / 幂等，仅测试用
data/         数据层       —— config.yaml 配置、users.json 测试数据、schemas.py 契约
common/       工具层       —— YAML 读取、统一断言封装
conftest.py   全局 fixture —— 配置、接口对象、报告钩子
```

> **为什么源码包叫 `apis/` 而不是 `api/`**：测试目录里有 `testcases/api/`。
> 如果源码包也叫 `api`，pytest 的路径插入规则会让 `testcases/api/__init__.py`
> 抢先注册 `api` 这个名字，于是 `from api import MesApi` 会**静默**解析到测试目录，
> 只报一句看不懂的 `ImportError: cannot import name 'MesApi' from 'api'`。
> 源码包与测试目录不同名，是避免这类事故最省事的办法。

**为什么这么分层**：接口地址变了只改 `data/config.yaml`；接口字段变了只改 `data/schemas.py`；
页面元素变了只改 `ui/pages/`；用例本身稳定不动。这是自动化框架能长期维护的前提。

## 快速开始

```powershell
# 1. 建虚拟环境并激活（Windows PowerShell）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 装依赖（国内建议加 -i 用清华源，见下方"国内环境"）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 装浏览器（UI 用例需要，只装 chromium 约 140MB）
playwright install chromium
```

> **一条命令搞定全部**：直接跑 `.\setup.ps1`，脚本会自动建环境、装依赖（含国内镜像）、
> 装浏览器并跑一遍接口用例验证。**但注意**：依赖必须先装好，
> **国内环境**下 PyPI 和 Playwright CDN 都需要换镜像，否则会"看起来卡死"：
>
> ```powershell
> # PyPI：清华源（实测同一份依赖：清华源 50 秒 / 官方源 20 分钟无进展）
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> # 想永久生效：把 pip.ini.example 复制到 %APPDATA%\pip\pip.ini
>
> # Playwright 浏览器：用 npmmirror（官方 cdn.playwright.dev 在国内常被 TLS 阻断）
> $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
> playwright install chromium
> ```

### 运行测试

```powershell
pytest                      # 全部用例
pytest -m api               # 只跑接口（快，日常开发用这个）
pytest -m ui                # 只跑 UI（需要浏览器）
pytest -m smoke             # 只跑冒烟
pytest -n 4 --dist loadfile # 4 进程并行（见下方说明）
pytest -k "login"           # 按关键字筛选用例
pytest --lf                 # 只重跑上次失败的用例
```

> **关于 `-n` 并行**：加 `--dist loadfile`（按文件分发），不要用默认的按用例分发。
> 原因是默认分发会把同一个文件里的用例拆到不同进程，fixture 的 session 级复用
> 就失效了（UI 那边每个进程各启一个浏览器），而且接口用例分散到多个进程后会
> **同时**打公网接口，更容易触发限流——并行度越高越容易出假失败。
> 在 CI 或内网环境上跑 `-n auto` 是合理的；对着公共练习站跑并行收益有限。

### 生成报告

```powershell
# Allure 报告（需要先装 allure 命令行工具）
pytest --alluredir=allure-results
allure serve allure-results

# 轻量 HTML 报告，不需要额外装东西
pytest --html=report.html --self-contained-html
```

## 用例覆盖

| 模块 | 用例数 | 覆盖内容 |
|---|---|---|
| 接口-用户 | 15 | 列表/详情/创建/更新/过滤，状态码、Schema 契约、字段类型、唯一性、邮箱格式、404 异常分支 |
| 接口-文章 | 8 | 创建回显、数据驱动、关联过滤、分页参数、更新 |
| 接口-性能基线 | 2 | 响应时间阈值（可用 `MAX_RESPONSE_MS` 调整）、请求头校验 |
| UI-登录 | 6 | 登录成功、4 种失败场景（参数化）、退出登录 |
| UI-商品/购物车 | 8 | 列表渲染、加购角标、多件参数化、购物车内容一致性、标题 |
| 业务-MES 全链路 | 22 | 工单状态机贯通、乐观锁并发、幂等键（回放/冲突/失败释放/超长拒绝）、并发领料不超卖、权限矩阵双向断言、令牌校验、Decimal 精度、探针、额外字段拒绝 |
| 数据隔离 | 会话级 1 项 | 会话结束时各表行数与库存可用量合计必须回到基线（见下方"数据隔离"一节） |

业务全链路那些用例**不依赖公网**：`testcases/mes/` 的 session 级 fixture 会自己
拉起一个本地 FastAPI + 独立 SQLite（端口由系统分配、库在 pytest 临时目录里），
跑完自动 kill。所以它既能离线跑，也不会污染开发者手工起的那个实例。

## 设计要点（面试会问）

- **数据驱动**：用例逻辑与测试数据分离，`@pytest.mark.parametrize` 从 `data/` 取数据，加数据不改代码。
- **契约测试**：用 JSON Schema 校验响应结构，接口"删字段/改类型"能被立刻发现。
- **异常分支覆盖**：不存在的资源返回 404、错误密码提示文案，比正向用例更容易抓到 bug。
- **显式等待**：UI 用例全程使用 Playwright 自动等待和 `expect` 断言，没有一处 `sleep`。
- **fixture 作用域**：`browser` 用 session（启动浏览器最贵，整个会话只启一次），
  **`browser_context` 必须用 function**——浏览器上下文是 cookie/localStorage 的隔离边界。
  saucedemo 的购物车就存在 localStorage 里，context 一共用，上一条用例加的商品就会串到下一条，
  表现为「加了 1 个，角标显示 2」这种假失败。创建 context 只要几十毫秒，省这个钱不值。
- **失败留证**：UI 用例失败自动截图到 `testcases/ui/artifacts/`，CI 里作为 artifact 上传。
- **失败重试**：接口层对连接失败、读超时、429 和 5xx 自动重试（退避 0.5s/1s），
  避免公网或测试环境抖动产生"假失败"——没有重试的自动化，团队很快就不再信任它。
  但**只重试幂等请求**：GET/PUT/DELETE 可以重放，PATCH 与 POST 不重放
  （PATCH 在 RFC 意义上不保证幂等），POST 只在 429（请求没被处理）时重试，
  因为创建接口在 5xx 后重放可能造出重复订单、重复用户这类脏数据。
- **代理自动回退**：传输失败后的重试会临时绕过系统代理再试一次
  （`proxies={"http": None, "https": None}`）。本机开着 Clash/v2ray 时，`requests`
  会自动走它，而这类代理对某些域名会直接掐断 TLS，报
  `SSLError: UNEXPECTED_EOF_WHILE_READING`——直连反而是通的。
  这条回退让"代理抽风"不至于被误判成"用例写错了"。设 `API_TRUST_ENV=0` 可关闭。
- **耗时口径**：`response.elapsed_ms` 只统计**产出响应那一次请求**的耗时，
  不含之前失败重试的等待。否则一次成功的重试会因为把上回超时的 30 秒也累加进来，
  被判成"接口变慢"——恢复成功反而报失败。

## 数据隔离（自动化能不能长期活下去的关键）

测试用例造数据不收尾，是自动化项目最常见的死因：第一次跑全绿，第二次开始出现
莫名其妙的失败，三个月后没人敢删用例、也没人敢信它的结论。这套用例把"干净"
做成了**会红的断言**，而不是一句口头约定：

- **会话级基线断言**：`testcases/mes/conftest.py` 的 `data_isolation_check`（autouse，session 级）
  在会话开始抓一次快照、结束时再抓一次，两者必须完全相等。挂在 session 级而不是每条用例结束，
  是因为工厂造完数据、下一条用例还没跑时行数本来就是"临时脏"的；逐条查会把正常流程判成污染。
- **两个指标，缺一不可**：`testcases/mes/data_isolation.py` 同时统计**各表行数**和**库存可用量合计**。
  只断行数会漏掉最阴的一类泄漏——领料只 `UPDATE` 一行、行数完全不变，但账实已经不符。
- **清理不依赖用例正文走完**：`factory` fixture 在 teardown 里回收，`take_material` fixture
  在 teardown 里自动反向退料。断言失败的用例也会把数据还回去，否则一条失败的用例会留下脏数据、
  把后面几条一起带红，最后没人分得清哪个才是真正的失败点。
- **登记式回收**：用例里直接发请求造的数据（绕开工厂方法的那些）要显式登记，
  漏登记的表现是"用例全绿、会话收尾报差异"。这类差异信息被刻意写成排查方向而不是结论：

  ```text
  会话结束时数据没有回到基线 —— 有用例造了数据没清理，或改了库存没退回去。
    表 materials: 基线 5 行 -> 现在 7 行（差 +2）
    库存可用量合计: 基线 3270.000 -> 现在 3280.000（差 +10.000）
  排查方向：用了 factory 却绕开它直接发请求造数据（没登记回收）；
  或者领料用例忘了反向退料（见 test_mes_business.py 的 take_material fixture）。
  ```

- **只增不改的表怎么回收**：`stock_ledger` 作为审计真相来源没有删除接口，
  所以它只能由测试在收尾时直连数据库回收；判据是 `X-Work-Order-No` 请求头声明的
  **归属标识**，而不是服务端签发的单号——那个字符串在流水里可能是 NULL、空串或用例随手写的标记。
  这里踩过两个坑，都留在代码注释里：`WHERE work_order_no = ?` 对 NULL 永远匹配不到
  （SQL 里 `NULL = NULL` 是 UNKNOWN，得写 `IS NULL ?`）；以及工单已被别的用例删掉时，
  早期代码直接 `continue` 跳过了整条回收流程，连带它关联的库存行也永远清不掉。

## 当前实测结果

```text
76 passed in 63.00s                    # pytest（全量，Windows 本机实测）

pytest -m api    33 passed, 43 deselected in 25.85s
pytest -m ui     14 passed, 62 deselected in 27.26s
pytest -m mes    29 passed, 47 deselected in  7.16s
python tools/check_retry.py           12/12 通过（重试与代理回退的路径覆盖）
```

> 接口用例跑的是公网 `jsonplaceholder.typicode.com`。这个站会**限流**：
> 短时间内连续跑多轮全量用例，会开始返回 429 或直接读超时。
> 传输层参数可用环境变量调，不用改代码：
>
> ```powershell
> $env:API_TIMEOUT = "15"        # 单次请求超时秒数（默认 15）
> $env:API_RETRIES = "2"         # 重试次数（默认 2）
> $env:MAX_RESPONSE_MS = "5000"  # 响应时间断言阈值（默认 5000，公网接口要放宽）
> $env:API_TRUST_ENV = "0"       # 关掉"失败后绕过代理重试"
> ```
>
> 换成自己公司的内网接口后，`MAX_RESPONSE_MS` 应收到 200-500ms 才有性能门禁的意义。

## 持续集成

推送到 `main` 分支自动触发：安装依赖 → 跑接口用例 → 跑 UI 用例 → 生成 Allure 报告。
报告在 Actions 页面的 Artifacts 里下载（`allure-report`），失败截图在 `ui-screenshots`。

两个刻意的设计：

- **UI 用例标了 `continue-on-error`**：它们打的是公共 demo 站，外部因素多。
  但失败不阻断流水线 ≠ 可以不管——报告和截图照样产出，需要人去看。
  真实项目里这个开关要慎用，它很容易变成"红灯没人管"的起点。
- **Allure 用官方命令行生成，不用第三方 Docker action**。原来用的
  `simple-elf/allure-report-action@v1.9` 依赖镜像 `openjdk:8-jre-alpine`，
  该镜像已被 Docker Hub 下架，报 `failed to resolve source metadata`，
  结果整个 job 在第一步就失败、连测试都没跑到。第三方 action 的这种腐烂
  是真实工程里常见的一类风险：**引用别人的东西，就要承担它某天消失的代价。**

## 常见问题

**1. `pip install` 卡住不动（CPU 跑满但没输出）**
国内直连 PyPI 会导致依赖解析极慢。用清华源，或把 `pip.ini.example` 复制到 `%APPDATA%\pip\pip.ini` 永久生效。

**2. `playwright install` 报 TLS/证书错误**
官方 CDN 在国内常被阻断，设置 `PLAYWRIGHT_DOWNLOAD_HOST` 指向 npmmirror（见上方安装步骤）。

**3. 浏览器启动报 `EPERM: operation not permitted, mkdtemp ...\Temp\playwright-artifacts`**
系统临时目录受限（企业电脑、沙箱环境常见）。把临时目录指到项目内：

```powershell
New-Item -ItemType Directory -Force -Path .\.tmp | Out-Null
$env:TEMP = (Resolve-Path .\.tmp).Path
$env:TMP = $env:TEMP
```

**4. 中文输出乱码**
PowerShell 默认 GBK。设置 `$env:PYTHONIOENCODING = "utf-8"`，或在 VS Code 里用已配置好的终端（`.vscode/settings.json` 已设）。

**5. UI 用例想看着浏览器跑**
把 `testcases/ui/conftest.py` 里 `p.chromium.launch(headless=True)` 改成 `headless=False`。

**6. `playwright install chromium` 完全没输出、永远不动（0 下载）**
少数机器上 Playwright 自带的下载器会静默卡死。判断方法：看 `%USERPROFILE%\AppData\Local\ms-playwright`
下有没有新建目录、有没有增长。绕开办法是手工下载 + 解压（镜像直连实测 8 秒下完 139MB）：

```powershell
# 1. 先问清楚它期望放到哪、从哪下
python -m playwright install chromium --dry-run

# 2. 用 curl 直连镜像下载（7 秒 / 139MB，比自带下载器可靠）
curl.exe -L -o .\.tmp\chromium-win64.zip `
  https://cdn.npmmirror.com/binaries/playwright/builds/chromium/1140/chromium-win64.zip

# 3. 解压到 Playwright 期望的目录（zip 顶层已经是 chrome-win）
$dest = "$env:USERPROFILE\AppData\Local\ms-playwright\chromium-1140"
Expand-Archive -Path .\.tmp\chromium-win64.zip -DestinationPath $dest -Force

# 4. 补两个标记文件，Playwright 靠它们判断"已安装且依赖已校验"
New-Item -ItemType File -Force -Path "$dest\INSTALLATION_COMPLETE" | Out-Null
New-Item -ItemType File -Force -Path "$dest\DEPENDENCIES_VALIDATED" | Out-Null

# 5. 验证
python -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); print('OK', b.version); b.close(); p.stop()"
```

> 注意：**这一步要用 PowerShell 做，不要用 Python 脚本做**。受限环境下 Python 进程写工作区外
> 会直接 `PermissionError: [WinError 5] 拒绝访问`，而 PowerShell 可以。
> 版本号 `1140` 是 Playwright 1.48 对应的 Chromium 版本号，换版本时以 `--dry-run` 的输出为准。
