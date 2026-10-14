"""探针：隔离区用例（明知红、暂不删）。

这个文件**不**匹配 ``python_files = test_*.py``，正常目录递归收集碰不到它；
元测试用显式路径把它喂给真实的 pytest 子进程。

刻意让它**失败**，因为这样才能同时证明两件事：
  1. 主门禁里它被 deselect（所以整体仍然绿）——如果隔离钩子失灵，它会把门禁弄红；
  2. ``-m quarantine`` 单独跑时它的红**照样看得见**——隔离区不是「洗白区」，
     只是把它从「每次提交都挡路」降级为「按需查看 + 周报统计」。
"""

import pytest

pytestmark = pytest.mark.quarantine


def test_probe_quarantined_known_failure():
    pytest.fail("已知红：这条用例在等 product 侧确认，暂不阻断门禁（见 wave2 handoff）")
