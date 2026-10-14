"""元测试：证明 flaky 治理机制**真的在生效**，而不是"装了个插件"。

为什么值得专门写一文件：``pytest-rerunfailures`` 装上了不等于治理成立——
它默认什么都不做（没标 flaky 就不重跑），而隔离区更是纯粹我们自己写的钩子。
所以这里不测插件、也不 import 插件的内部函数，而是**开真的 pytest 子进程**，
让「真 pytest.ini + 真根 conftest.py + 真插件」这条完整链路自己跑一遍，
再从子进程的输出与治理账本上读证据。

三条机制各有一个反面对照，任何一条被拆掉都会让对应用例变红：

  ① flaky 重跑真的发生   —— 关掉 flaky marker/重跑 → 探针首跑失败直接红
  ② only_rerun 真的收窄  —— 换成 blanket rerun（全局 --reruns）→ 真回归被吞成绿
  ③ 隔离区真的生效       —— 摘掉 conftest 的 deselect 钩子 → 探针把门禁弄红

探针放在 ``_probes/`` 下且**不**匹配 ``python_files = test_*.py``，
所以它们不会被任何正常的目录递归收集到（``pytest testcases/api`` 仍是 33 条），
只有本文件用显式路径才喂得进去。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.api

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBES_DIR = Path(__file__).resolve().parent / "_probes"

FLAKY_PROBE = "testcases/api/_probes/probe_flaky_rerun.py"
REGRESSION_PROBE = "testcases/api/_probes/probe_flaky_real_regression.py"
QUARANTINE_PROBE = "testcases/api/_probes/probe_quarantine.py"


def _run_pytest(*args: str, ledger: Path) -> subprocess.CompletedProcess:
    """在仓库根开一个真的 pytest 子进程，并把治理账本重定向到 ledger。

    cwd 必须是仓库根：否则 pytest.ini（markers / addopts / python_files）不生效，
    这条元测试就退化成"在测一个没有任何治理机制的裸 pytest"，全绿也说明不了什么。
    账本路径用环境变量重定向，避免并发跑的元测试互相覆盖同一份文件。
    """
    env = dict(os.environ)
    # 子进程的 stdout 走管道，默认会用本机 locale 编码（Windows 中文是 GBK），
    # 这里钉成 utf-8，断言才不依赖运行机器的区域设置。
    env["PYTHONIOENCODING"] = "utf-8"
    env["FLAKY_GOVERNANCE_LEDGER"] = str(ledger)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _ledger(path: Path) -> dict:
    assert path.exists(), f"治理账本没有被写出来：{path}\n—— conftest 的 sessionfinish 钩子可能已经失效"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ① flaky 重跑真的发生
# ---------------------------------------------------------------------------
def test_flaky_marker_reruns_a_transient_failure(tmp_path):
    """首跑失败的 flaky 用例被重跑一次并通过；账本里留下「重跑才通过」的记录。"""
    proc = _run_pytest(FLAKY_PROBE, ledger=tmp_path / "ledger.json")

    assert proc.returncode == 0, f"重跑没有把这条用例救回来：\n{proc.stdout}"
    # -raR 里的 R 就是为这一节加的；只有 reportchars 含 r/R 时插件才打印它。
    assert "RERUN" in proc.stdout, f"没有出现 RERUN：\n{proc.stdout}"
    assert "1 rerun" in proc.stdout, f"重跑次数不是 1：\n{proc.stdout}"

    data = _ledger(tmp_path / "ledger.json")
    assert [entry["nodeid"] for entry in data["rerun_passed"]] == [
        "testcases/api/_probes/probe_flaky_rerun.py::test_probe_flaky_rerun_would_pass_second_time"
    ]
    assert data["rerun_still_failed"] == []


def test_plain_failure_is_not_rerun(tmp_path):
    """没标 flaky 的失败用例一次都不会被重跑——这是「不许 blanket rerun」的底线。"""
    proc = _run_pytest(QUARANTINE_PROBE, "-m", "quarantine", ledger=tmp_path / "ledger.json")

    assert proc.returncode == 1
    assert "RERUN" not in proc.stdout, f"一条没标 flaky 的用例被重跑了：\n{proc.stdout}"
    assert _ledger(tmp_path / "ledger.json")["reruns_total"] == 0


# ---------------------------------------------------------------------------
# ② only_rerun 真的把重跑收窄到抖动指纹
# ---------------------------------------------------------------------------
def test_real_regression_is_not_swallowed_by_reruns(tmp_path):
    """同样是标了 flaky 的用例，失败信息是契约断言（真回归）→ 当场硬失败，不重跑。

    这条是「门禁必须保持真红可信度」的直接守卫：把 flaky_policy 的 only_rerun
    拿掉、或在 addopts 里加全局 --reruns，它就会变成"重跑一次然后绿"。
    """
    proc = _run_pytest(REGRESSION_PROBE, ledger=tmp_path / "ledger.json")

    assert proc.returncode == 1, f"真回归被重跑吞掉了：\n{proc.stdout}"
    assert "RERUN" not in proc.stdout, f"真回归竟然被重跑了：\n{proc.stdout}"
    assert "字段 [id] 不匹配" in proc.stdout, f"失败原因丢失：\n{proc.stdout}"
    assert _ledger(tmp_path / "ledger.json")["reruns_total"] == 0


# ---------------------------------------------------------------------------
# ③ 隔离区真的生效
# ---------------------------------------------------------------------------
def test_quarantine_case_is_deselected_and_listed_in_main_gate(tmp_path):
    """主门禁里隔离用例被 deselect（门禁不受它的红影响），但清单被逐条打印出来。"""
    proc = _run_pytest(QUARANTINE_PROBE, FLAKY_PROBE, ledger=tmp_path / "ledger.json")

    # 它红着，但门禁是绿的 —— 说明红被隔离钩子挡住了，而不是被吞掉了。
    assert proc.returncode == 0, f"隔离用例没有退出主门禁：\n{proc.stdout}"
    assert "1 deselected" in proc.stdout, f"deselect 计数不对：\n{proc.stdout}"
    # 关键：必须打印清单。静默丢弃是另一种死亡方式。
    assert f"DESELECTED {QUARANTINE_PROBE}::test_probe_quarantined_known_failure" in proc.stdout, (
        f"隔离清单没有被打印出来：\n{proc.stdout}"
    )

    data = _ledger(tmp_path / "ledger.json")
    assert data["quarantined"] == [f"{QUARANTINE_PROBE}::test_probe_quarantined_known_failure"]
    assert data["quarantined_count"] == 1
    # 账本的 collected_count 是 deselect 之后的数量（worker 侧 deselect 后报给 master），
    # 所以它证明的是「隔离用例没有进入执行阶段」，而不是「收集不到它」。
    assert data["collected_count"] == 1


def test_quarantine_cases_can_be_run_on_demand(tmp_path):
    """`-m quarantine` 单独跑时，隔离用例照常执行、照常红——隔离区不是洗白区。"""
    proc = _run_pytest(QUARANTINE_PROBE, "-m", "quarantine", ledger=tmp_path / "ledger.json")

    assert proc.returncode == 1, f"隔离用例单跑时居然没红：\n{proc.stdout}"
    assert "test_probe_quarantined_known_failure" in proc.stdout
    assert "DESELECTED" not in proc.stdout, f"单跑隔离区时不该再 deselect：\n{proc.stdout}"

    data = _ledger(tmp_path / "ledger.json")
    assert data["quarantined"] == []
    assert data["quarantined_count"] == 0
