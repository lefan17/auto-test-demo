"""元测试：验证「测试自己起的服务子进程」的覆盖率真的被采集到了。

为什么需要这条用例：覆盖率盲区是**静默**的 —— 报告里 `mock_server/*` 那一栏恒为 0%，
看起来像"这些代码没人跑过"，而不是"采集失败"。人眼看一次报告挡不住以后悄悄回退
（例如有人把 `stop()` 改回一句 `terminate()`，或者把 launcher 摘掉）。所以这里自己起一个
短命服务、自己指一个临时 data file，断言两件事：

1. 服务子进程退出后确实多出一个 `.coverage.*` 分片；
2. 分片里 `mock_server/routers.py` 有**真实执行的行**。

只断言"分片文件存在"是不够的：Windows 上 `terminate()` 恰好会产生一个空壳分片
（`pytest_cov.embed.init()` 被 site 处理两次，第二次会 `cleanup()` 并用零数据落一次盘）。
"文件在、数据空"正是这个 bug 最迷惑人的形态，所以断言必须落到行数据上。
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest
from mock_server_ctl import MockServer, subprocess_coverage_env

pytestmark = [pytest.mark.mes, pytest.mark.regression]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTERS_PY = PROJECT_ROOT / "mock_server" / "routers.py"


def _shards(directory: Path) -> set[Path]:
    """列出目录里的覆盖率分片。

    用 `.coverage.*` 而不是 `.coverage*`：后者会把 `.coveragerc` 也匹配进来，
    再拿它当 sqlite 读就会报 "file is not a database"（这个坑踩过）。
    """
    return {path for path in directory.glob(".coverage.*") if path.is_file()}


def _executed_lines(shard: Path, target: Path) -> list[int]:
    """读一个分片里某个源文件被真正执行到的行号。"""
    from coverage import CoverageData

    data = CoverageData(basename=str(shard))
    data.read()
    wanted = target.resolve()
    for measured in data.measured_files():
        if Path(measured).resolve() == wanted:
            return list(data.lines(measured) or [])
    return []


def test_service_subprocess_shard_contains_real_lines(tmp_path: Path) -> None:
    """服务跑完一轮后，分片里必须有它执行过的行 —— 否则覆盖率盲区又回来了。"""
    data_file = tmp_path / ".coverage"
    shards_before = _shards(tmp_path)
    assert shards_before == set(), f"临时目录一开始就有分片，断言会失真：{shards_before}"

    # 显式注入 COV_CORE_*：本用例要能**脱离 --cov 独立跑**（普通 `pytest -m mes` 下
    # os.environ 里没有这些变量），断言才不会依赖"外面恰好开着覆盖率"。
    server = MockServer(
        db_path=tmp_path / "coverage.db",
        log_path=tmp_path / "coverage-server.log",
        extra_env=subprocess_coverage_env(data_file),
    )
    try:
        server.start()
        for _ in range(3):
            with urllib.request.urlopen(f"{server.root_url}/api/v1/healthz", timeout=5.0) as resp:
                resp.read()
    finally:
        server.stop()

    new_shards = _shards(tmp_path) - shards_before
    assert new_shards, (
        "服务子进程没有写出任何覆盖率分片：子进程根本没拿到覆盖率埋点。"
        f"临时目录内容={sorted(p.name for p in tmp_path.iterdir())}，服务日志尾部={server._read_log_tail()}"
    )

    per_shard = {shard.name: len(_executed_lines(shard, ROUTERS_PY)) for shard in sorted(new_shards)}
    assert any(count > 0 for count in per_shard.values()), (
        "分片文件存在但 mock_server/routers.py 一行执行记录都没有 —— "
        "这就是覆盖率盲区的原始症状。最常见原因是服务进程被硬杀（Windows 上 terminate() = "
        "TerminateProcess，不跑 atexit），覆盖率数据没机会落盘，只留下 embed 二次 init 写的空壳。"
        f"各分片命中的 routers.py 行数={per_shard}，服务日志尾部={server._read_log_tail()}"
    )
