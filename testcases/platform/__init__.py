"""testcases.platform 是包（与 testcases/api、testcases/contract 一致）。

有 ``__init__.py`` 时 pytest 按 ``testcases.platform.test_x`` 导入，同名测试文件不会互相
遮蔽；根 conftest.py 已把仓库根放进 ``sys.path``，导入路径因此成立。
"""
