"""qaplat —— 测试平台层。

把仓库里已有的 pytest 用例集合变成「可分层门禁、可度量、可追溯契约漂移」的平台：

- ``gate``     四层门禁（smoke / pr / nightly / release）的表达式、预算与阈值判定
- ``metrics``  运行结果落 SQLite：轮次、用例级结果、契约漂移历史
- ``registry`` 用例 ↔ operationId 覆盖矩阵（归属只取显式 ``covers`` marker 与源码路径字符串）
- ``classify`` 失败归因（环境 / 数据 / 被测缺陷 / 真实回归 / 未知）
- ``report``   静态 HTML 报表

包名是 ``qaplat`` 而不是 ``platform``：仓库根的 ``conftest.py`` 把 ``ROOT_DIR`` 插进
``sys.path[0]``，若存在 ``platform/`` 目录会遮蔽标准库 ``platform`` 模块——而 pytest 自身
在启动时就 ``import platform``，结果是整套测试直接崩在收集阶段。
"""

from __future__ import annotations

from qaplat.paths import CACHE_DIR, DEFAULT_DB, DEFAULT_SPEC_PATH, ROOT

__all__ = ["CACHE_DIR", "DEFAULT_DB", "DEFAULT_SPEC_PATH", "ROOT", "__version__"]

__version__ = "0.1.0"
