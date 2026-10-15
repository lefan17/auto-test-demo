"""路径常量集中处。

单独成文件，是因为 ``__init__`` 被 ``-p qaplat._dump`` 这类 pytest 插件入口占用时
（插件在 pytest 启动早期被 import），需要一条不触发任何副作用、不 import sqlite3 /
subprocess 的轻量路径来源。``paths`` 只依赖标准库 ``pathlib``。
"""

from __future__ import annotations

from pathlib import Path

#: 仓库根目录（``qaplat/`` 的上一级）。
ROOT = Path(__file__).resolve().parent.parent

#: 平台层产物目录：数据库、原始 JSON、HTML 报表都落这里（已进 .gitignore）。
CACHE_DIR = ROOT / "qaplat" / ".cache"

#: CLI 默认数据库路径，对应契约里的 ``qaplat/.cache/qaplat.db``。
DEFAULT_DB = CACHE_DIR / "qaplat.db"

#: 默认契约基准文件（OpenAPI 3.1.0，13 paths / 29 operations）。
DEFAULT_SPEC_PATH = ROOT / "docs" / "openapi.json"

#: 契约漂移检查脚本。
EXPORT_SCRIPT = ROOT / "scripts" / "export_openapi.py"
