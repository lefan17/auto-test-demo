"""并发用例的判定口径：把「真的同时跑过」变成一个可计算的量。

为什么单独放一个模块而不是写在 conftest 的 race fixture 里：
fixture 是可以被**整体替换**的。判定口径一旦写在 fixture 内部（或依赖 fixture
自己去做断言），有人把 race 换成"起 N 个线程依次 join"、甚至一个普通 for 循环时，
自检会跟着一起消失，用例照样全绿 —— 这正是并发用例最典型的一种假绿：
注释里写着"否则构不成竞态"，但没有任何一条断言真的在检查竞态存不存在。
所以口径放在这里，断言写在用例正文里，替换任何 fixture 都抹不掉。

为什么核心指标是"时间区间是否交叠"而不是"总耗时是否更短"：
后端用 BEGIN IMMEDIATE 把写请求在事务开头串行化（见 mock_server/db.py 的 write_tx），
并发请求会排队，所以并发跑并不比串行快。实测同一对审核请求：
并发墙钟 25.4ms / 串行 4.8ms —— 并发反而更慢（抢锁 + 排队）。
拿耗时当判据会得出完全错误的结论，能区分"并发"与"排队"的只有时间线本身。
"""

from __future__ import annotations


def overlap_ms(spans: list) -> float:
    """所有请求里最长的一段"公共交叠时间"（毫秒）。

    spans 是 [(开始秒, 结束秒), ...]（time.perf_counter 的读数）。
    两个区间 [a0,a1] 与 [b0,b1] 的交叠长度 = min(a1,b1) - max(a0,b0)；
    串行执行时后者的开始必然晚于前者的结束，这个值恒 <= 0。
    """
    best = float("-inf")
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a0, a1 = spans[i]
            b0, b1 = spans[j]
            best = max(best, (min(a1, b1) - max(a0, b0)) * 1000)
    return best


def race_timeline(outcome, *, workers: int):
    """校验 race 的返回值形状，返回 (结果列表, 墙钟毫秒, 时间线)。

    为什么要显式校验形状：race 是并发用例的承重结构，
    它一旦被换成串行实现，用例必须红。"少了时间线"本身就是最直接的红法。
    """
    assert isinstance(outcome, tuple), (
        "race 必须返回 (结果列表, 墙钟毫秒, 每个请求的起止时间) 三件套；"
        f"实际拿到 {type(outcome).__name__}。"
        "少了时间线，'这些请求真的同时跑过'就无法被验证 —— 并发用例会退化成假绿。"
    )
    assert len(outcome) == 3, (
        f"race 必须返回三件套 (结果列表, 墙钟毫秒, 每个请求的起止时间)，实际长度 {len(outcome)}。"
        "少了时间线，'这些请求真的同时跑过'就无法被验证 —— 并发用例会退化成假绿。"
    )
    results, elapsed_ms, spans = outcome
    assert len(results) == workers, f"race 应回报 {workers} 个结果，实际 {len(results)}"
    assert len(spans) == workers, f"race 应回报 {workers} 段时间线，实际 {len(spans)}"
    assert all(span is not None for span in spans), f"有请求没有留下时间线: {spans}"
    return results, elapsed_ms, spans


def assert_requests_overlapped(spans: list, *, workers: int, elapsed_ms: float, action: str) -> None:
    """断言这 workers 个请求的**时间区间真的交叠过**，否则用例必须红。

    这是"并发"这件事唯一可观测的承重特征：把请求改成串行发出（哪怕是同一个 fixture
    换个实现），区间就会首尾相接、交叠量 <= 0，本条断言立刻失败。
    """
    assert len(spans) == workers, f"时间线条数应为 {workers}，实际 {len(spans)}"
    base = min(span[0] for span in spans)
    timeline = "；".join(
        f"#{index + 1} +{(span[0] - base) * 1000:.1f}ms → +{(span[1] - base) * 1000:.1f}ms"
        f"（历时 {(span[1] - span[0]) * 1000:.1f}ms）"
        for index, span in enumerate(spans)
    )
    overlapped = overlap_ms(spans)
    assert overlapped > 0, (
        f"{action}：{workers} 个请求的时间区间没有任何交叠（最长交叠 {overlapped:.1f}ms），"
        "说明它们其实是**排队串行**跑完的，这条用例根本没验证到并发行为。\n"
        f"  时间线: {timeline}\n"
        f"  墙钟总耗时: {elapsed_ms:.1f}ms"
    )
