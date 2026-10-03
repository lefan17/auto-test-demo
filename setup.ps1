# 本地一键初始化脚本（Windows PowerShell）
# 用法：在项目根目录执行  .\setup.ps1

$ErrorActionPreference = "Stop"

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

Write-Host "`n=== [3/5] 安装依赖 ===" -ForegroundColor Cyan
# 用清华源：直连 PyPI 在国内经常慢到「看起来卡死」，实测同样依赖 50 秒 vs 20 分钟
$MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip -i $MIRROR
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt -i $MIRROR --timeout 30

Write-Host "`n=== [4/5] 安装 Playwright 浏览器（chromium，约 140MB） ===" -ForegroundColor Cyan
# 官方 CDN 在国内常被 TLS 阻断，改用 npmmirror 镜像
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
# Playwright 解压要用临时目录；若系统临时目录受限，指向项目内 .tmp
New-Item -ItemType Directory -Force -Path ".\.tmp" | Out-Null
$env:TEMP = (Resolve-Path ".\.tmp").Path
$env:TMP = $env:TEMP
& ".\.venv\Scripts\python.exe" -m playwright install chromium

Write-Host "`n=== [5/5] 跑一遍接口用例验证环境 ===" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pytest -m api

Write-Host "`n完成！接下来：" -ForegroundColor Green
Write-Host "  1. 用 VS Code 打开本文件夹（已配置好解释器和测试面板）"
Write-Host "  2. 跑 UI 用例:  .\.venv\Scripts\python.exe -m pytest -m ui"
Write-Host "  3. 看报告:      .\.venv\Scripts\python.exe -m pytest --html=report.html --self-contained-html"
