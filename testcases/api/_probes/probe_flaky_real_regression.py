"""探针：标了 flaky、但失败信息是**真回归**——绝不允许被重跑吸收。

这个探针是「禁止 blanket rerun」这条硬约束的活证据：它和
``probe_flaky_rerun.py`` 用同一个 flaky 策略（``flaky_policy.NETWORK_FLAKY``），
唯一的区别是失败文案不匹配任何抖动指纹，所以插件必须当场硬失败、一次都不重跑。

如果哪天有人把 ``--reruns`` 写进 addopts、或者把 only_rerun 拿掉，
这个探针就会「重跑一次然后绿」——对应的元测试随之变红。
"""

import pytest

from testcases.api.flaky_policy import NETWORK_FLAKY


@NETWORK_FLAKY
def test_probe_real_regression_must_not_be_rerun():
    # 文案照抄 common/assert_util.py::assert_field_equals 的实际输出。
    # 这正是「接口字段被改坏」的样子——它是真回归，重跑只会让它晚一轮暴露。
    pytest.fail("字段 [id] 不匹配: 期望 1, 实际 None")
