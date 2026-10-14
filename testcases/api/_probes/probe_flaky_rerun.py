"""探针：标了 flaky 的用例首次失败会被重跑，重跑通过后必须留下痕迹。

这个文件**不**匹配 pytest.ini 的 ``python_files = test_*.py``，所以正常的
目录递归收集不会碰到它；元测试
（``testcases/api/test_flaky_governance_meta.py``）用显式路径把它喂给一个
真实的 pytest 子进程，从而在「真 pytest.ini + 真根 conftest + 真插件」上验证机制。

第一次失败刻意照着 ``common/assert_util.py::assert_status_code`` 的实际文案拼
——探针要打的是生产里的那条正则，而不是随手编一句好匹配的话。
"""

from testcases.api.flaky_policy import NETWORK_FLAKY

_ATTEMPTS = {"n": 0}


@NETWORK_FLAKY
def test_probe_flaky_rerun_would_pass_second_time():
    _ATTEMPTS["n"] += 1
    if _ATTEMPTS["n"] == 1:
        raise AssertionError(
            "\nURL      : GET https://jsonplaceholder.typicode.com/users\n"
            "期望状态码: 200\n"
            "实际状态码: 502\n"
            "响应内容  : typicode.com | 502: Bad gateway"
        )
    # 第二次（重跑）直接通过：如果没人重跑它，这条用例就会是红的
    assert _ATTEMPTS["n"] == 2
