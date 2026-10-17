"""Playwright 浏览器安装脚本的解析逻辑（`tools/install_chromium.py`）。

为什么这些用例必须存在
----------------------
这个脚本现在**不写死任何版本号**（`tools/install_chromium.py:47` 的 ``HEADER_RE``）：
它跑一次 ``python -m playwright install chromium --dry-run``，从输出里解析
``Install location:`` 与 ``Download url:``。好处是升级 Playwright 不用改脚本，
代价是**上游输出格式变成唯一契约，而它没有任何测试**——格式一变，脚本只会
「装到空目录」或者「静默什么都不做」，两种都不会报错。

绝不真跑 ``playwright install``：那会下载 120MB，本仓实测过会卡死。
这里把 ``subprocess.run`` 换成喂固定文本的假实现（`tools/install_chromium.py:56` 是唯一调用点）。
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tools import install_chromium

pytestmark = pytest.mark.platform


# 真实的 `--dry-run` 输出形状：先一段 bootstrap（没有 `(playwright ... vNNN)` 头），
# 再逐个浏览器给「头 + Install location + Download url」。
def _dry_run_text(root: str) -> str:
    """按给定缓存根目录拼一段 `--dry-run` 输出。``root`` 用真实 tmp_path 才能验证解压落点。"""
    return (
        "bootstrap 1155\n"
        f"  Install location:    {root}\\bootstrap-1155\n"
        "  Download url:        https://cdn.example/playwright/builds/ffmpeg/1155/ffmpeg-win64.zip\n"
        "\n"
        "Chrome for Testing 133.0.6943.16 (playwright chromium v1155)\n"
        f"  Install location:    {root}\\chromium-1155\n"
        "  Download url:        https://cdn.example/playwright/builds/chromium/1155/chromium-win64.zip\n"
        "  Download fallback 1: https://mirror.example/chromium-win64.zip\n"
        "\n"
        "Chrome Headless Shell 133.0.6943.16 (playwright chromium-headless-shell v1155)\n"
        f"  Install location:    {root}\\chromium_headless_shell-1155\n"
        "  Download url:        https://cdn.example/playwright/builds/chromium/1155/"
        "chromium-headless-shell-win64.zip\n"
    )


DRY_RUN_TEXT = _dry_run_text("C:\\Users\\u\\AppData\\Local\\ms-playwright")


def _patch_run(
    monkeypatch: pytest.MonkeyPatch, stdout: str, *, returncode: int = 0, stderr: str = ""
) -> list[list[str]]:
    """把 `subprocess.run` 换成固定输出的假实现，并记录它被怎么调用。"""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(install_chromium.subprocess, "run", fake_run)
    return calls


def test_dry_run_parses_each_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tools/install_chromium.py:47`+`:68-81`：头部与两行字段一一对应地解析出来。"""
    calls = _patch_run(monkeypatch, DRY_RUN_TEXT)

    entries = install_chromium.dry_run()

    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium", "--dry-run"]]
    assert [entry["name"] for entry in entries] == ["chromium", "chromium-headless-shell"]
    assert [entry["rev"] for entry in entries] == ["1155", "1155"]
    assert entries[0]["location"] == "C:\\Users\\u\\AppData\\Local\\ms-playwright\\chromium-1155"
    assert entries[0]["url"] == "https://cdn.example/playwright/builds/chromium/1155/chromium-win64.zip"
    assert entries[1]["location"] == "C:\\Users\\u\\AppData\\Local\\ms-playwright\\chromium_headless_shell-1155"
    assert entries[1]["url"].endswith("chromium-headless-shell-win64.zip")


def test_bootstrap_block_before_any_header_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """bootstrap 那段在第一个头之前（脚本 :74-76 的 ``if current is None: continue``）。

    如果把它挂到第一个条目上，就会把 ``Install location`` 解成 bootstrap 目录，
    于是 zip 被解到错误的地方、而 ``main()`` 依然返回 0 —— 静默装歪。
    """
    _patch_run(monkeypatch, DRY_RUN_TEXT)

    parsed = install_chromium.dry_run()

    assert len(parsed) == 2, f"bootstrap 段被当成了一个浏览器条目：{[e['name'] for e in parsed]}"
    assert "bootstrap" not in parsed[0]["location"], "bootstrap 段的 Install location 被错挂到第一个条目上"
    assert parsed[0]["location"].endswith("chromium-1155")


def test_nonzero_exit_reports_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :61-64：playwright 自己失败时要说明退出码与 stderr，并返回空列表（让 ``main`` 退出 1）。"""
    _patch_run(monkeypatch, "", returncode=1, stderr="ERROR: playwright not installed")

    assert install_chromium.dry_run() == []

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "退出码 1" in combined
    assert "playwright not installed" in combined


def test_unparseable_output_is_reported_not_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :52-81：returncode=0 但输出格式变了 → 必须**说出来**，不能静默返回空。

    这是一条真实缺陷的回归：原来的实现在这种情况下返回 ``[]`` 且**一个字都不打印**，
    ``main()``（脚本 :147-149）于是只给一个裸的退出码 1 —— 用的人分不清是
    「playwright 没装」「网络不通」还是「上游把输出格式改了」。
    """
    _patch_run(monkeypatch, "Playwright 1.60\n新格式：chromium at C:\\somewhere\n")

    entries = install_chromium.dry_run()

    out = capsys.readouterr()
    assert entries == []
    assert out.out.strip(), "解析不出条目却什么都没打印 —— 用户只能看到裸的退出码 1"
    assert "解析" in out.out or "没有" in out.out


def test_main_exits_nonzero_when_nothing_parsed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :147-149：解析不到条目时 ``main()`` 必须返回 1（否则下一步会在 None 上崩）。"""
    _patch_run(monkeypatch, "garbage that matches nothing\n")
    monkeypatch.setattr(sys, "argv", ["install_chromium.py"])

    assert install_chromium.main() == 1

    out = capsys.readouterr()
    assert out.out.strip() or out.err.strip(), "退出码 1 却没有任何解释"


def test_main_without_zip_prints_plan_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :151-154：只给 ``--dry-run`` 计划、不给 zip 时打印清单并以 0 退出。"""
    _patch_run(monkeypatch, DRY_RUN_TEXT)
    monkeypatch.setattr(sys, "argv", ["install_chromium.py"])

    assert install_chromium.main() == 0

    out = capsys.readouterr().out
    assert "chromium v1155" in out
    assert "chromium-headless-shell v1155" in out
    assert "下一步" in out


def test_pick_entry_by_zip_file_name() -> None:
    """脚本 :101-103：不给 ``--entry`` 时按 ``Download url`` 的文件名选条目。"""
    entries = [
        {"name": "chromium", "rev": "1155", "url": "https://cdn/x/chromium-win64.zip"},
        {"name": "chromium-headless-shell", "rev": "1155", "url": "https://cdn/x/chromium-headless-shell-win64.zip"},
    ]

    picked = install_chromium.pick_entry(entries, "chromium-headless-shell-win64.zip", None)

    assert picked is not None
    assert picked["name"] == "chromium-headless-shell"


def test_pick_entry_by_explicit_name() -> None:
    """脚本 :94-99：``--entry`` 优先，且按条目名（不是 zip 名）匹配。"""
    entries = [
        {"name": "chromium", "rev": "1155", "url": "https://cdn/x/chromium-win64.zip"},
        {"name": "chromium-headless-shell", "rev": "1155", "url": "https://cdn/x/same-name.zip"},
    ]

    picked = install_chromium.pick_entry(entries, "chromium-win64.zip", "chromium-headless-shell")

    assert picked is not None
    assert picked["name"] == "chromium-headless-shell"


def test_pick_entry_unknown_name_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    """脚本 :98-99：``--entry`` 写错时要列出可选项并返回 None（让 main 退出 1）。"""
    entries = [{"name": "chromium", "rev": "1155", "url": "https://cdn/x/chromium-win64.zip"}]

    assert install_chromium.pick_entry(entries, "any.zip", "chromium-headless-shell") is None

    out = capsys.readouterr().out
    assert "没有条目 chromium-headless-shell" in out
    assert "chromium" in out


def test_pick_entry_ambiguous_zip_requires_entry(capsys: pytest.CaptureFixture[str]) -> None:
    """脚本 :110-111：同一个 zip 名对上多条时拒绝猜 —— 猜错就是把 A 的包装进 B 的目录。"""
    entries = [
        {"name": "chromium", "rev": "1155", "url": "https://cdn/x/chromium-win64.zip"},
        {"name": "chromium-tip-of-tree", "rev": "1155", "url": "https://mirror/x/chromium-win64.zip"},
    ]

    assert install_chromium.pick_entry(entries, "chromium-win64.zip", None) is None

    out = capsys.readouterr().out
    assert "对应多条" in out
    assert "--entry" in out


def test_pick_entry_no_match_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    """脚本 :104-109：zip 名对不上任何 Download url 时返回 None。"""
    entries = [{"name": "chromium", "rev": "1155", "url": "https://cdn/x/chromium-win64.zip"}]

    assert install_chromium.pick_entry(entries, "chromium-mac.zip", None) is None

    out = capsys.readouterr().out
    assert "没有哪条 Download url 的文件名是 chromium-mac.zip" in out


def _make_zip(zip_path: Path, inner_exe: str) -> Path:
    """造一个和 Playwright 期望同构的 zip：顶层目录名 == zip 文件名（不含扩展名）。"""
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{zip_path.stem}/{inner_exe}", b"MZ fake exe")
    return zip_path


def test_extract_installs_into_the_location_given_by_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :114-138：解到 ``Install location`` 指向的目录，并认出 ``chrome.exe``。"""
    zip_path = _make_zip(tmp_path / "chromium-win64.zip", "chrome.exe")
    dest = tmp_path / "ms-playwright" / "chromium-1155"
    _patch_run(monkeypatch, _dry_run_text(str(tmp_path / "ms-playwright")))
    monkeypatch.setattr(sys, "argv", ["install_chromium.py", str(zip_path)])

    code = install_chromium.main()

    out = capsys.readouterr().out
    assert code == 0, out
    assert (dest / "chromium-win64" / "chrome.exe").is_file(), f"没解到 Install location：{out}"


def test_extract_rejects_unexpected_zip_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :133-135：zip 顶层没有期望的 exe 时必须返回 1，不能假装装好了。"""
    zip_path = _make_zip(tmp_path / "chromium-win64.zip", "readme.txt")
    dest = tmp_path / "ms-playwright"
    _patch_run(monkeypatch, _dry_run_text(str(dest)))
    monkeypatch.setattr(sys, "argv", ["install_chromium.py", str(zip_path)])

    code = install_chromium.main()

    out = capsys.readouterr().out
    assert code == 1, out
    assert "没找到" in out


def test_extract_reports_permission_error_with_powershell_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :123-127：受限环境写不进去时给出 Expand-Archive 的替代命令并以 1 退出。"""
    zip_path = _make_zip(tmp_path / "chromium-win64.zip", "chrome.exe")

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "拒绝访问")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", denied)

    code = install_chromium.extract(zip_path, tmp_path / "dest")

    out = capsys.readouterr().out
    assert code == 1
    assert "Expand-Archive" in out
    assert str(zip_path) in out


def test_main_rejects_zip_path_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :156-159：zip 路径写错时明确说找不到，而不是让 ZipFile 抛 FileNotFoundError。"""
    _patch_run(monkeypatch, DRY_RUN_TEXT)
    monkeypatch.setattr(sys, "argv", ["install_chromium.py", str(tmp_path / "nope.zip")])

    code = install_chromium.main()

    out = capsys.readouterr().out
    assert code == 1
    assert "找不到 zip" in out


def test_main_rejects_entry_without_install_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """脚本 :165-168：``--dry-run`` 没给 Install location 时不能猜一个默认目录。"""
    zip_path = _make_zip(tmp_path / "chromium-win64.zip", "chrome.exe")
    _patch_run(
        monkeypatch,
        "Chrome for Testing 133 (playwright chromium v1155)\n  Download url:        https://cdn/x/chromium-win64.zip\n",
    )
    monkeypatch.setattr(sys, "argv", ["install_chromium.py", str(zip_path)])

    code = install_chromium.main()

    out = capsys.readouterr().out
    assert code == 1
    assert "没有 Install location" in out
