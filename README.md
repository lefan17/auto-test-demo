# 接口 + UI 自动化测试框架

[![Auto Test](https://github.com/你的用户名/auto-test-demo/actions/workflows/test.yml/badge.svg)](https://github.com/你的用户名/auto-test-demo/actions/workflows/test.yml)

基于 Python + pytest 的自动化测试框架，覆盖 **接口自动化** 与 **UI 自动化**，
支持数据驱动、JSON Schema 契约校验、Allure 报告和 GitHub Actions 持续集成。

> 说明：把上面两处 `你的用户名/auto-test-demo` 换成你自己的仓库地址，徽章才会变绿。

## 技术栈

| 用途 | 选型 |
|---|---|
| 测试框架 | pytest 8 + pytest-xdist（并行）+ pytest-cov（覆盖率） |
| 接口测试 | requests + jsonschema + PyYAML |
| UI 测试 | Playwright 1.48（sync API） |
| 报告 | Allure |
| 持续集成 | GitHub Actions |

## 分层设计

```
api/          接口封装层   —— BaseApi 统一超时/日志/Allure 步骤，UserApi 定义业务接口
testcases/    用例层       —— 只写业务断言，不出现 requests 和 selector
  api/        接口用例
  ui/         UI 用例
ui/pages/     Page Object  —— 元素定位与页面操作
data/         数据层       —— config.yaml 配置、users.json 测试数据、schemas.py 契约
common/       工具层       —— YAML 读取、统一断言封装
conftest.py   全局 fixture —— 配置、接口对象、报告钩子
```

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
pytest -n 4                 # 4 进程并行
pytest -k "login"           # 按关键字筛选用例
pytest --lf                 # 只重跑上次失败的用例
```

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
| 接口-用户 | 15+ | 列表/详情/创建/更新/过滤，状态码、Schema 契约、字段类型、唯一性、404 异常分支 |
| 接口-文章 | 8+ | 创建回显、数据驱动、关联过滤、分页参数、更新 |
| 接口-性能基线 | 2 | 响应时间阈值（可用 `MAX_RESPONSE_MS` 调整）、请求头校验 |
| UI-登录 | 6 | 登录成功、4 种失败场景（参数化）、退出登录 |
| UI-商品/购物车 | 8 | 列表渲染、加购角标、多件参数化、购物车内容一致性、标题 |

## 设计要点（面试会问）

- **数据驱动**：用例逻辑与测试数据分离，`@pytest.mark.parametrize` 从 `data/` 取数据，加数据不改代码。
- **契约测试**：用 JSON Schema 校验响应结构，接口"删字段/改类型"能被立刻发现。
- **异常分支覆盖**：不存在的资源返回 404、错误密码提示文案，比正向用例更容易抓到 bug。
- **显式等待**：UI 用例全程使用 Playwright 自动等待和 `expect` 断言，没有一处 `sleep`。
- **fixture 作用域**：`browser` 用 session（启动浏览器最贵，整个会话只启一次），
  **`browser_context` 必须用 function**——浏览器上下文是 cookie/localStorage 的隔离边界。
  saucedemo 的购物车就存在 localStorage 里，context 一共用，上一条用例加的商品就会串到下一条，
  表现为「加了 1 个，角标显示 2」这种假失败。创建 context 只要几十毫秒，省这个钱不值。
- **失败留证**：UI 用例失败自动截图到 `ui/artifacts/`，CI 里作为 artifact 上传。
- **失败重试**：接口层对连接失败、读超时、429 和 5xx 自动重试（退避 0.5s/1s），
  避免公网或测试环境抖动产生"假失败"——没有重试的自动化，团队很快就不再信任它。
  但**只重试幂等请求**：GET/PUT/PATCH/DELETE 可以重放，POST 只在 429（请求没被处理）时重试，
  因为创建接口在 5xx 后重放可能造出重复订单、重复用户这类脏数据。
- **代理自动回退**：传输失败后的重试会临时绕过系统代理再试一次
  （`proxies={"http": None, "https": None}`）。本机开着 Clash/v2ray 时，`requests`
  会自动走它，而这类代理对某些域名会直接掐断 TLS，报
  `SSLError: UNEXPECTED_EOF_WHILE_READING`——直连反而是通的。
  这条回退让"代理抽风"不至于被误判成"用例写错了"。设 `API_TRUST_ENV=0` 可关闭。
- **耗时口径**：`response.elapsed_ms` 只统计**产出响应那一次请求**的耗时，
  不含之前失败重试的等待。否则一次成功的重试会因为把上回超时的 30 秒也累加进来，
  被判成"接口变慢"——恢复成功反而报失败。

## 当前实测结果

```text
46 passed in 50.17s                    # pytest（全量，Windows 本机实测）

pytest -m api    32 passed, 14 deselected in 16.00s
pytest -m ui     14 passed, 32 deselected in 27.26s
python tools/check_retry.py            8/8 通过（重试与代理回退的路径覆盖）
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
报告在 Actions 页面的 Artifacts 里下载（`allure-report`）。

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
