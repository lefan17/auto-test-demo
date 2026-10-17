"""`scripts/check_deps_sync.py` 的判定规则：正例、反例、边界与归一化。

这个脚本的结论会随 `requirements.txt` / `requirements.lock` 的内容而变——只测「真实
两份文件跑一遍得 0」是不够的：那份断言在「检查逻辑被删空、永远返回 0」时同样为绿，
而这正是依赖清单检查最需要防的那种失效（假绿）。所以下面每一条都自己造两份文件，
把结论钉在**造出来的输入**上；真实文件那条只作为「当前仓库确实同步」的事实快照。

脚本不是包里的模块（`scripts/` 没有 `__init__.py`，且在 `pyproject.toml` 的 mypy
`files` 里是按目录收的），所以用 `importlib` 按路径加载，而不是 `import scripts.x`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from qaplat.paths import ROOT

pytestmark = pytest.mark.platform

SCRIPT = ROOT / "scripts" / "check_deps_sync.py"


def _load_module():
    """按路径加载被测脚本。用私有模块名，避免与仓库里同名的 `scripts/*.py` 撞车。"""
    spec = importlib.util.spec_from_file_location("_check_deps_sync_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_module()


def _write_pair(tmp_path: Path, direct: str, lock: str) -> tuple[Path, Path]:
    direct_path = tmp_path / "requirements.txt"
    lock_path = tmp_path / "requirements.lock"
    direct_path.write_text(direct, encoding="utf-8")
    lock_path.write_text(lock, encoding="utf-8")
    return direct_path, lock_path


# ------------------------------------------------------------------ 正例：真实文件


def test_real_manifests_are_in_sync(checker, capsys) -> None:
    """正例（当前仓库的事实快照）：真实两份文件跑一遍 → 0，并报出直接 / 传递依赖条数。

    `capsys` 顺带验证 `_force_utf8_stdio()` 不会在 pytest 捕获下炸掉——它在
    Windows 上真踩过 `UnicodeEncodeError`，而那段代码只在真正打印 `↔` 时才暴露。
    """
    assert checker.main() == 0
    out = capsys.readouterr()
    assert "[通过]" in out.out
    assert "↔" in out.out
    assert "传递依赖（合法，不算漂移）" in out.out


# ------------------------------------------------------------------ 反例：版本漂移


def test_version_drift_exits_one_and_names_the_package(checker, tmp_path, capsys) -> None:
    """反例：txt 写 `requests==2.34.2`、lock 写 `requests==2.31.0` → 1，且点名 requests。"""
    direct, lock = _write_pair(
        tmp_path,
        "requests==2.34.2\n",
        "requests==2.31.0\n    # via -r requirements.txt\n",
    )
    assert checker.main(direct, lock) == 1
    err = capsys.readouterr().err
    assert "requests" in err
    assert "2.34.2" in err
    assert "2.31.0" in err
    assert "requirements.txt:1" in err
    assert "requirements.lock:1" in err


def test_package_missing_from_lock_exits_one(checker, tmp_path, capsys) -> None:
    """反例：txt 声明了包而 lock 里完全没有 → 1（这是「忘了重跑 pip-compile」的形状）。"""
    direct, lock = _write_pair(
        tmp_path,
        "requests==2.34.2\nfastapi==0.143.0\n",
        "requests==2.34.2\n    # via -r requirements.txt\n",
    )
    assert checker.main(direct, lock) == 1
    err = capsys.readouterr().err
    assert "fastapi" in err
    assert "requirements.lock 里没有这个包" in err


def test_unparsable_line_is_a_failure_not_a_skip(checker, tmp_path, capsys) -> None:
    """反例：txt 里混进一行范围式 spec → 1。

    静默跳过解析不了的行会把「写错格式」变成「检查通过」，所以这里必须是红的。
    """
    direct, lock = _write_pair(
        tmp_path,
        "requests==2.34.2\nfastapi>=0.143\n",
        "requests==2.34.2\n    # via -r requirements.txt\n",
    )
    assert checker.main(direct, lock) == 1
    err = capsys.readouterr().err
    assert "不是 name==version 形式的 pin" in err
    assert "fastapi>=0.143" in err


# ------------------------------------------------------------------ 边界：传递依赖不算漂移


def test_transitive_only_in_lock_is_not_drift(checker, tmp_path, capsys) -> None:
    """边界（防以后误修）：lock 多出的传递依赖**不算**漂移 → 0。

    lock 是 pip-compile 的全量产物，理应有 txt 里没有的包（当前真实文件里 45 个）。
    如果有人把这条规则改成双向相等，本用例立刻变红——那正是要拦的改动：它会逼着
    lock 砍回只有直接依赖，等于悄悄取消全量钉死。
    """
    direct, lock = _write_pair(
        tmp_path,
        "requests==2.34.2\n",
        "requests==2.34.2\n    # via -r requirements.txt\n"
        "certifi==2026.7.22\n    # via requests\n"
        "urllib3==2.7.0\n    # via requests\n",
    )
    assert checker.main(direct, lock) == 0
    out = capsys.readouterr().out
    assert "1 条直接依赖" in out
    assert "2 个传递依赖" in out


# ------------------------------------------------------------------ 归一化：包名大小写与分隔符


@pytest.mark.parametrize(
    ("txt_name", "lock_name"),
    [
        ("PyYAML==6.0.3", "pyyaml==6.0.3"),
        ("pytest-cov==6.3.0", "pytest_cov==6.3.0"),
        ("pytest.cov==6.3.0", "PyTest-Cov==6.3.0"),
    ],
)
def test_package_name_normalization_is_not_drift(checker, tmp_path, capsys, txt_name, lock_name) -> None:
    """归一化：`PyYAML`/`pyyaml`、`pytest-cov`/`pytest_cov` 是同一个包，不算漂移 → 0。

    pip 自己按 PEP 503 判等价（`[-_.]+` 统一成 `-`、转小写），逐字比包名会产出假红灯。
    """
    direct, lock = _write_pair(tmp_path, f"{txt_name}\n", f"{lock_name}\n    # via -r requirements.txt\n")
    assert checker.main(direct, lock) == 0
    assert "1 条直接依赖" in capsys.readouterr().out


def test_extras_in_lock_do_not_break_matching(checker, tmp_path) -> None:
    """归一化的另一半：lock 里带 extras 的写法（`x[extra]==1.0`）仍认得出包名 → 0。"""
    direct, lock = _write_pair(
        tmp_path,
        "schemathesis==4.31.1\n",
        "schemathesis[test]==4.31.1\n    # via -r requirements.txt\n",
    )
    assert checker.main(direct, lock) == 0


# ------------------------------------------------------------------ 缺文件


def test_missing_lock_file_is_a_failure(checker, tmp_path, capsys) -> None:
    """反例：lock 文件不存在 → 1（不是「跳过」；缺文件说明编译从没跑过）。"""
    direct, lock = _write_pair(tmp_path, "requests==2.34.2\n", "requests==2.34.2\n")
    lock.unlink()
    assert checker.main(direct, lock) == 1
    assert "缺少" in capsys.readouterr().err
