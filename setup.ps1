# 本地一键初始化脚本（Windows PowerShell）
# 用法：在项目根目录执行  .\setup.ps1
#
# 如果提示"未对文件进行数字签名"，用这条命令绕过（只对这一次生效）：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
# 关掉进度条：在 PowerShell 5.1 里，进度条输出会拖慢甚至卡住子进程
$ProgressPreference = "SilentlyContinue"
# 禁止 pip 交互式提问，否则它可能在等一个永远没人回答的输入
$env:PIP_NO_INPUT = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

$LogDir = ".\.tmp"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 子进程输出重定向到文件再读回来，而不是直接让 PowerShell 接管管道。
# 原因：PowerShell（尤其 5.1）接管子进程管道时，缓冲区写满会和子进程互相卡住——
# 表现为「命令明明跑完了却永远不返回」。实测用文件重定向后 1.7 秒返回。
function Invoke-Logged {
    param([string]$Exe, [string[]]$Arguments, [string]$LogName)
    $log = Join-Path $LogDir $LogName
    & $Exe @Arguments > $log 2>&1
    $code = $LASTEXITCODE
    Get-Content $log -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
    return $code
}

Write-Host "=== [1/5] 检查 Python ===" -ForegroundColor Cyan
python --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "未找到 python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== [2/5] 创建虚拟环境 .venv ===" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "已创建 .venv"
} else {
    Write-Host ".venv 已存在，跳过"
}
$Py = ".\.venv\Scripts\python.exe"

Write-Host "`n=== [3/5] 安装依赖 ===" -ForegroundColor Cyan
# 用清华源：直连 PyPI 在国内经常慢到「看起来卡死」，实测同样依赖 50 秒 vs 20 分钟
$MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
$null = Invoke-Logged $Py @("-m", "pip", "install", "--upgrade", "pip", "-i", $MIRROR) "setup_pip_upgrade.log"
$code = Invoke-Logged $Py @("-m", "pip", "install", "-r", "requirements.txt", "-i", $MIRROR, "--timeout", "30") "setup_pip_install.log"
if ($code -ne 0) {
    Write-Host "依赖安装失败（退出码 $code），日志见 $LogDir\setup_pip_install.log" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== [4/5] 安装 Playwright 浏览器（chromium，约 140MB） ===" -ForegroundColor Cyan
# 官方 CDN 在国内常被 TLS 阻断，改用 npmmirror 镜像
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
# Playwright 解压要用临时目录；若系统临时目录受限，指向项目内 .tmp
$env:TEMP = (Resolve-Path $LogDir).Path
$env:TMP = $env:TEMP
$null = Invoke-Logged $Py @("-m", "playwright", "install", "chromium") "setup_pw.log"

# 校验：少数机器上自带下载器会「静默卡死」——不报错、也不下载。
# 只检查退出码会漏掉这种情况，所以直接看目录里有没有真的东西。
$browserRoot = "$env:USERPROFILE\AppData\Local\ms-playwright"
$installed = Get-ChildItem $browserRoot -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Where-Object { Get-ChildItem $_.FullName -Recurse -Filter "chrome.exe" -ErrorAction SilentlyContinue }
if (-not $installed) {
    Write-Host "`n[警告] 没检测到已安装的 chromium，可能下载器卡死了（表现为 0 输出、0 下载）。" -ForegroundColor Yellow
    # 注意这里用单引号包住提示语，避免里面引号被 PowerShell 解析器吃掉
    Write-Host '请按 README「常见问题 第 6 条」手工安装：用 curl 从镜像下载 zip 后 Expand-Archive 解压。' -ForegroundColor Yellow
    Write-Host "注意那一步要用 PowerShell 做，不要用 Python 脚本做。" -ForegroundColor Yellow
} else {
    Write-Host "chromium 已就绪: $($installed.FullName)" -ForegroundColor Green
}

Write-Host "`n=== [5/5] 跑一遍接口用例验证环境 ===" -ForegroundColor Cyan
& $Py -m pytest -m api

Write-Host "`n完成！接下来：" -ForegroundColor Green
Write-Host "  1. 用 VS Code 打开本文件夹（已配置好解释器和测试面板）"
Write-Host "  2. 跑 UI 用例:  .\.venv\Scripts\python.exe -m pytest -m ui"
Write-Host "  3. 看报告:      .\.venv\Scripts\python.exe -m pytest --html=report.html --self-contained-html"
