#!/usr/bin/env pwsh
# 一键复现容量压测：起服务 → 多并发梯度发压 → 落结果 → 回收服务进程。
#
# 用法（在仓库根执行）：
#     pwsh -File perf/run_load.ps1
#     pwsh -File perf/run_load.ps1 -Users "5,10,20,50" -RunTime 20s -Repeat 2
#
# 为什么要有这个脚本，而不是只留 `python perf/run_load.py`：
# 本机有两处环境地雷（都在 Windows 上实测踩过），runner 已经自己处理掉了
# PYTHONUTF8 与 PYTHONPATH，但"用哪个解释器 / 装在哪里的 locust"仍然需要一处
# 明确的、可被照抄的执行入口 —— 验收时要求"从零起服务到落结果一条命令"。
# 脚本只负责把参数转发给 runner，判定逻辑一律留在 Python 里（一处，可测）。
#
# 退出码：0=跑完；2=环境缺依赖或端口被占；1=运行中出错。

[CmdletBinding()]
param(
    [string]$Users = "5,10,20,50",
    [string]$RunTime = "20s",
    [int]$Repeat = 2,
    [int]$Port = 8321,
    [string]$Label = "",
    [string]$Python = "",
    [string]$Role = "ADMIN",
    [string]$UserClasses = "MesApiUser",
    [switch]$ListOnly
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # 1) 解释器：-Python 参数 > $env:PERF_PYTHON > 仓库 venv > PATH 上的 python。
    #    与 `testcases/mes/mock_server_ctl.py` 的 DEFAULT_PYTHON / MOCK_PYTHON 同一套约定：
    #    默认用仓库 venv，环境变量可覆盖，不需要调用者背路径。
    if (-not $Python) { $Python = $env:PERF_PYTHON }
    if (-not $Python) {
        $venv = Join-Path $repoRoot ".venv\Scripts\python.exe"
        $Python = if (Test-Path $venv) { $venv } else { "python" }
    }
    Write-Host "[run_load.ps1] 解释器 $Python"

    # 2) locust 在哪：venv 里没有就走共享目录 pylibs（pip --target 安装）。
    $pylibs = Join-Path (Split-Path -Parent $repoRoot) "pylibs"
    if (Test-Path (Join-Path $pylibs "locust")) {
        $env:PYTHONPATH = @(
            $pylibs,
            (Join-Path $pylibs "win32"),
            (Join-Path $pylibs "win32\lib"),
            $repoRoot
        ) -join [IO.Path]::PathSeparator
        Write-Host "[run_load.ps1] locust 取自 $pylibs"
    }
    # 3) 中文 Windows 上 locust 会用 GBK 读 pyproject.toml 而崩（exit=2）。
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"

    # locust 的可用性探针：用临时文件收 stderr，而不是 `2>&1`。
    # 在 `$ErrorActionPreference = "Stop"` 下，把原生命令的 stderr 併进管道会
    # 生成 ErrorRecord 并把脚本打断 —— 那样"缺依赖"会表现成脚本自身崩溃，
    # 掩盖真正的提示信息。这里只关心退出码。
    $errFile = [IO.Path]::GetTempFileName()
    try {
        $probe = & $Python -c "import locust; print(locust.__version__)" 2>$errFile
        $probeExit = $LASTEXITCODE
        $probeErr = Get-Content $errFile -Raw -ErrorAction SilentlyContinue
    }
    finally {
        Remove-Item $errFile -ErrorAction SilentlyContinue
    }
    if ($probeExit -ne 0) {
        Write-Host "[run_load.ps1] 解释器 `"$Python`" 里没有可用的 locust。" -ForegroundColor Red
        Write-Host "  可选依赖装法： & `"$Python`" -m pip install -r requirements-perf.txt"
        Write-Host "  本机已装好 locust 的解释器可用 -Python 指定，或用 `$env:PERF_PYTHON 覆盖。"
        if ($probeErr) { Write-Host ("  探针报错： " + ($probeErr -split "`n" | Select-Object -Last 3 -join " / ")) }
        exit 2
    }
    Write-Host "[run_load.ps1] locust $probe"

    $runnerArgs = @(
        "perf/run_load.py",
        "--python", $Python,
        "--users", $Users,
        "--run-time", $RunTime,
        "--repeat", $Repeat,
        "--port", $Port,
        "--role", $Role,
        "--user-classes", $UserClasses
    )
    if ($Label) { $runnerArgs += @("--label", $Label) }
    if ($ListOnly) { $runnerArgs += "--list-only" }

    & $Python @runnerArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
