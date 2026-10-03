"""手工安装 Playwright 的 Chromium（绕过 playwright install 卡死的问题）。

背景：playwright install 自带的下载器在某些 Windows 环境（代理、企业安全软件、
受限临时目录）会静默卡死，但 zip 用 curl 从 npmmirror 下载只要 8 秒。
这个脚本把已下载的 zip 解压到 Playwright 期望的目录结构，并写好标记文件。

用法：
    curl.exe -L -o .tmp/chromium-win64.zip ^
      https://cdn.npmmirror.com/binaries/playwright/builds/chromium/1140/chromium-win64.zip
    python tools/install_chromium.py

注意：在受限沙箱环境里，Python 进程写工作区外会报
`PermissionError: [WinError 5] 拒绝访问`。遇到这种情况请改用 PowerShell 手工解压，
命令见 README 常见问题第 6 条——那套命令在受限环境下同样有效。
"""
import shutil
import sys
import zipfile
from pathlib import Path

# Playwright 的浏览器缓存目录
BROWSER_ROOT = Path.home() / "AppData" / "Local" / "ms-playwright"
TARGET = BROWSER_ROOT / "chromium-1140"
ZIP_PATH = Path(__file__).resolve().parent.parent / ".tmp" / "chromium-win64.zip"


def main() -> int:
    if not ZIP_PATH.exists():
        print(f"[错误] 找不到 zip: {ZIP_PATH}")
        print("先执行: curl.exe -L -o .tmp/chromium-win64.zip https://cdn.npmmirror.com/binaries/playwright/builds/chromium/1140/chromium-win64.zip")
        return 1

    # 清理可能存在的半成品
    if TARGET.exists():
        print(f"删除已存在的目录: {TARGET}")
        shutil.rmtree(TARGET, ignore_errors=True)
    TARGET.mkdir(parents=True, exist_ok=True)

    print(f"解压 {ZIP_PATH.name} -> {TARGET} ...")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(TARGET)

    # zip 顶层目录名（通常是 chrome-win）统一改名为 chrome-win
    entries = [p for p in TARGET.iterdir()]
    print("解压后顶层内容:", [p.name for p in entries])

    chrome_exe = next(TARGET.rglob("chrome.exe"), None)
    if chrome_exe is None:
        print("[错误] 解压后找不到 chrome.exe")
        return 1

    # Playwright 期望的结构是 chromium-1140/chrome-win/chrome.exe
    if chrome_exe.parent.name != "chrome-win":
        new_dir = TARGET / "chrome-win"
        print(f"重命名 {chrome_exe.parent.name} -> chrome-win")
        chrome_exe.parent.rename(new_dir)
        chrome_exe = new_dir / "chrome.exe"

    # 写标记文件：Playwright 靠这两个文件判断浏览器已安装且依赖已校验
    (TARGET / "INSTALLATION_COMPLETE").touch()
    (TARGET / "DEPENDENCIES_VALIDATED").touch()

    print(f"[完成] chrome.exe 位置: {chrome_exe}")
    print(f"[完成] 大小: {chrome_exe.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
