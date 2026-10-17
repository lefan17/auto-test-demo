r"""手工安装 Playwright 浏览器（绕过 `playwright install` 卡死的问题）。

背景：`playwright install` 自带的下载器在某些 Windows 环境（代理、企业安全软件、
受限临时目录）会静默卡死，而用 curl 从 npmmirror 下载 zip 只要几秒。
这个脚本把已下载的 zip 解压到 Playwright 期望的目录结构。

**不写死任何版本号**：它自己跑 `python -m playwright install chromium --dry-run`，
从输出里读「Install location」与「Download url」——只有那一步的输出是这一版
Playwright 的真值。写死的 rev（旧文档里的 `chromium-1140` 就是一例）换版本后会对不上，
运行期只会得到 `Executable doesn't exist at ...\chromium-<rev>\chrome-win64\chrome.exe`。

用法：
    # 1. 看真值（只读，任何环境都能跑）：列出每个浏览器的名字、rev、安装位置、下载地址
    python tools/install_chromium.py

    # 2. 照第 1 步输出的 Download url 下载 zip。国内可先设镜像让 URL 直接指向 npmmirror：
    #    $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://cdn.npmmirror.com/binaries/playwright"
    curl.exe -L -o .tmp/chrome-headless-shell-win64.zip "<第 1 步输出的 Download url>"

    # 3. 解压进 Playwright 期望的位置（zip 文件名与某条 Download url 的文件名一致即可）
    python tools/install_chromium.py .tmp/chrome-headless-shell-win64.zip

注意：受限环境里 Python 进程写工作区外会被拒（实测 `PermissionError: [Errno 13] Permission
denied`，也有环境报 `[WinError 5]：拒绝访问`）。第 3 步要写 `ms-playwright`，在这种环境里会失败
——脚本会把等价的 PowerShell 命令打出来，照那个做即可（README 常见问题第 6 条）。
第 1 步不写盘，任何环境都能跑。

另：本机 `chromium_headless_shell-1243` 目录里**本来就没有** `INSTALLATION_COMPLETE` /
`DEPENDENCIES_VALIDATED`，它照样能启动（playwright 1.63.0 只查可执行文件在不在），
所以本脚本不写它们。补上无害，但别把它当必要条件。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Playwright 的浏览器缓存目录
BROWSER_ROOT = Path.home() / "AppData" / "Local" / "ms-playwright"

# --dry-run 的条目头，例如：Chrome for Testing 153.0.8010.12 (playwright chromium v1243)
HEADER_RE = re.compile(r"\(playwright ([A-Za-z0-9_-]+) v([0-9]+)\)")

EXPECTED_EXES = ("chrome.exe", "chrome-headless-shell.exe")


def dry_run() -> list[dict[str, str]]:
    """跑 `playwright install chromium --dry-run`，解析出每个浏览器的真值。"""
    cmd = [sys.executable, "-m", "playwright", "install", "chromium", "--dry-run"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as exc:
        print(f"[错误] 跑不起来 {cmd}：{exc}")
        print("可以自己跑一次上面这条命令，把输出的 Install location / Download url 抄下来。")
        return []
    if proc.returncode != 0:
        print(f"[错误] {' '.join(cmd)} 退出码 {proc.returncode}")
        print(proc.stderr.strip())
        return []

    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in proc.stdout.splitlines():
        header = HEADER_RE.search(line)
        if header is not None:
            current = {"name": header.group(1), "rev": header.group(2)}
            entries.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Install location:"):
            current["location"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Download url:"):
            current["url"] = stripped.split(":", 1)[1].strip()
    if not entries:
        # 退出码 0 却一条都没解析出来，只可能是上游改了输出格式（HEADER_RE 失配）。
        # 原来这里直接返回空列表，main() 于是只给一个裸的 exit 1 —— 用的人分不清是
        # 没装 Playwright、断网，还是格式变了。把原始输出贴出来才可能自查。
        print("[错误] playwright --dry-run 退出码 0，但输出里没解析到任何 (playwright <名字> v<版本>) 头。")
        print("多半是 Playwright 改了 --dry-run 的输出格式，需要同步更新 HEADER_RE 与字段前缀。")
        print("--dry-run 的实际输出：")
        print(proc.stdout.strip() or "（空）")
    return entries


def print_plan(entries: list[dict[str, str]]) -> None:
    print(f"浏览器缓存目录：{BROWSER_ROOT}")
    for entry in entries:
        print(f"\n{entry['name']} v{entry['rev']}")
        print(f"  Install location:  {entry.get('location', '(--dry-run 没给)')}")
        print(f"  Download url:      {entry.get('url', '(--dry-run 没给)')}")


def pick_entry(entries: list[dict[str, str]], zip_name: str, wanted: str | None) -> dict[str, str] | None:
    """按 zip 文件名或 --entry 选一条 --dry-run 条目。"""
    if wanted is not None:
        for entry in entries:
            if entry["name"] == wanted:
                return entry
        print(f"[错误] --dry-run 里没有条目 {wanted}，可选：{'、'.join(e['name'] for e in entries)}")
        return None

    matches = [e for e in entries if e.get("url", "").rsplit("/", 1)[-1] == zip_name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        print(f"[错误] 没有哪条 Download url 的文件名是 {zip_name}。可选项：")
        for entry in entries:
            print(f"  {entry['name']:<24} {entry.get('url', '(没有 url)')}")
        print("确认 zip 名字，或显式指定：--entry <名字>")
        return None
    print(f"[错误] {zip_name} 对应多条：{'、'.join(e['name'] for e in matches)}，请用 --entry 指定。")
    return None


def extract(zip_path: Path, dest: Path) -> int:
    if dest.exists():
        print(f"删除已存在的目录：{dest}")
        shutil.rmtree(dest, ignore_errors=True)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        print(f"解压 {zip_path.name} -> {dest} ...")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    except PermissionError as exc:
        print(f"[错误] 写不进 {dest}：{exc}")
        print("受限环境下 Python 进程写工作区外会被拒。改用 PowerShell 做这一步：")
        print(f'  Expand-Archive -Path "{zip_path}" -DestinationPath "{dest}" -Force')
        return 1

    # zip 顶层目录名就是 Playwright 期望的子目录名（chrome-win64 / chrome-headless-shell-win64）
    inner = dest / zip_path.stem
    exe = next((p for p in inner.glob("*.exe") if p.name in EXPECTED_EXES), None)
    print("解压后顶层内容：", sorted(p.name for p in dest.iterdir()))
    if exe is None:
        print(f"[错误] {inner} 下没找到 {' / '.join(EXPECTED_EXES)}；zip 结构可能和 Playwright 期望的不一致。")
        return 1
    print(f"[完成] 可执行文件：{exe}")
    print(f"[完成] 大小：{exe.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把已下载的 Playwright 浏览器 zip 装进 --dry-run 给出的目录")
    parser.add_argument("zip", nargs="?", type=Path, help="已下载的浏览器 zip（文件名与某条 Download url 一致）")
    parser.add_argument("--entry", help="zip 属于哪条 --dry-run 条目（如 chromium-headless-shell），默认按文件名匹配")
    args = parser.parse_args()

    entries = dry_run()
    if not entries:
        return 1

    if args.zip is None:
        print_plan(entries)
        print("\n下一步：照上面的 Download url 下载 zip，再 `python tools/install_chromium.py <zip 路径>`。")
        return 0

    zip_path: Path = args.zip
    if not zip_path.exists():
        print(f"[错误] 找不到 zip：{zip_path}")
        return 1

    entry = pick_entry(entries, zip_path.name, args.entry)
    if entry is None:
        return 1

    location = entry.get("location")
    if not location:
        print(f"[错误] {entry['name']} 这条没有 Install location，请自己跑一次 --dry-run 确认。")
        return 1
    return extract(zip_path, Path(location))


if __name__ == "__main__":
    sys.exit(main())
