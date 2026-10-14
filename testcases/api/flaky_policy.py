"""公网组用例的 flaky 策略：只允许「第三方抖动」被重跑吸收，真回归必须当场红。

为什么需要这个文件：``testcases/api/`` 的 4 个文件全部打
``https://jsonplaceholder.typicode.com``（``data/config.yaml`` 的 ``base.api``）。
这个公共接口会偶发 502（Cloudflare ``typicode.com | 502: Bad gateway``）与 429，
CI 上实测 4 轮里 3 轮撞上。但「抖动会红」不等于「可以全量重跑」——
全量重跑会把真回归一起吞掉，门禁就只剩一个装饰性的绿灯。

所以这里用 pytest-rerunfailures 的 ``only_rerun`` 把重跑收窄到**抖动指纹**：
失败信息不匹配下面任何一条正则时，插件立刻硬失败（``_should_hard_fail_on_error``，
见 pytest_rerunfailures.py:661），一次都不会重跑。

正则匹配的对象是 ``f"{type(exc).__name__}: {exc}"``（pytest_rerunfailures.py:651），
也就是断言失败信息本身——所以下面几条必须照着 ``common/assert_util.py``
实际拼出来的文案写，写错了等于没写。
"""

import pytest

# 只有命中这些指纹的失败才允许重跑一次。
# 逐条的取舍都写在注释里——漏掉一条会随机红，多写一条会吞掉真回归。
NETWORK_JITTER_PATTERNS = [
    # assert_util.assert_status_code 的文案是「实际状态码: 502」。
    # 429 是被限流、5xx 是网关/服务端抖动，两者都与被测代码无关。
    r"实际状态码: (?:429|5\d\d)",
    # 传输层重试耗尽（apis/base.py 已自带 2 次传输重试，这里是它之后的第二层）。
    r"Max retries exceeded",
    r"ConnectionError",
    r"Timeout",
    # 第三方返回了非 JSON 内容（Cloudflare 的错误页、限流页）。这确实是契约被破坏，
    # 但破坏方是第三方；持续发生时重跑仍然会红，只是把「瞬时」和「持续」区分开。
    r"响应不是合法 JSON",
    # 响应时间用例测的是「本机到 jsonplaceholder 的速度」（用例 docstring 已自认），
    # 阈值由 MAX_RESPONSE_MS 控制。网络抖动的慢必须重跑，
    # 否则这条用例会在 CI 上随机红——但它持续变慢时重跑仍会红。
    r"接口响应过慢",
]

# 刻意**不含**的指纹（出现即硬失败，一次都不重跑）：
#   字段 [...] 不匹配 / Schema 校验失败 / 类型错误 / 存在重复
# —— 这些是契约断言，红了就是真回归，重跑只会让它晚一轮暴露。

# 裸 @pytest.mark.flaky 的插件默认就是 reruns=1（pytest_rerunfailures.py:263），
# 这里显式写出 reruns=1 是为了让「只重跑一次」这件事在用例里可见、可 grep。
NETWORK_FLAKY = pytest.mark.flaky(reruns=1, only_rerun=NETWORK_JITTER_PATTERNS)
