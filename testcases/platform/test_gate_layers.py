"""门禁层自身：四层表达式拼装、阈值判定、退出码。

为什么这些用例必须存在：四层门禁是这套平台唯一会**阻断流水线**的部件。表达式写错
（例如把 ``not ui`` 写成 ``not_ui``）不会报错，只会静默地把该层变成「跑 0 条、全绿」，
是最危险的一类失效。所以表达式本身被逐字钉死在这里。
"""

from __future__ import annotations

import pytest

from qaplat import cli, gate
from qaplat.gate import LAYERS, Layer
from qaplat.metrics import RunRecord, outcome_of, pass_rate_of, utc_now

pytestmark = pytest.mark.platform


def _run(
    *,
    passed: int,
    failed: int = 0,
    errors: int = 0,
    skipped: int = 0,
    xfailed: int = 0,
    duration: float = 1.0,
    layer: str = "smoke",
    run_id: str = "run-test",
) -> RunRecord:
    total = passed + failed + errors + skipped + xfailed
    return RunRecord(
        run_id=run_id,
        layer=layer,
        commit_sha=None,
        started_at=utc_now(),
        duration_seconds=duration,
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        xfailed=xfailed,
        pass_rate=pass_rate_of(passed, failed, total),
        outcome=outcome_of(passed, failed, errors, total),
    )


# ------------------------------------------------------------------ 四层登记与表达式


def test_four_layers_are_registered() -> None:
    assert set(LAYERS) == {Layer.SMOKE, Layer.PR, Layer.NIGHTLY, Layer.RELEASE}


def test_marker_expressions_are_exactly_contracted() -> None:
    assert LAYERS[Layer.SMOKE].marker_expr == "smoke"
    assert LAYERS[Layer.PR].marker_expr == "smoke or api or platform"
    assert LAYERS[Layer.NIGHTLY].marker_expr == "not ui"
    assert LAYERS[Layer.RELEASE].marker_expr == ""


@pytest.mark.parametrize(
    ("layer", "expr"),
    [
        (Layer.SMOKE, "smoke"),
        (Layer.PR, "smoke or api or platform"),
        (Layer.NIGHTLY, "not ui"),
    ],
)
def test_build_pytest_args_puts_expression_first(layer: Layer, expr: str) -> None:
    args = gate.build_pytest_args(LAYERS[layer])
    assert args[:2] == ["-m", expr]
    assert gate.DUMP_PLUGIN in args


def test_release_layer_has_no_marker_expression() -> None:
    """release 是全量：写 ``-m ""`` 语义上等价，但读日志的人会以为有个表达式。"""
    args = gate.build_pytest_args(LAYERS[Layer.RELEASE])
    assert "-m" not in args
    assert args[:2] == ["-p", gate.DUMP_PLUGIN]


def test_extra_args_come_last_so_callers_can_override() -> None:
    args = gate.build_pytest_args(LAYERS[Layer.SMOKE], ["testcases/api", "--maxfail=1"])
    assert args[-2:] == ["testcases/api", "--maxfail=1"]


def test_every_layer_gets_its_own_dump_path() -> None:
    paths = {layer: gate._dump_path_for(layer.value) for layer in Layer}
    assert len(set(paths.values())) == 4


# ------------------------------------------------------------------ 达标 / 未达标两条路径


def test_gate_passes_when_pass_rate_meets_threshold() -> None:
    result = gate.evaluate(LAYERS[Layer.SMOKE], _run(passed=36))
    assert result.violations == ()
    assert result.within_budget is True
    assert gate.exit_code_for(result) == 0


def test_gate_fails_when_pass_rate_below_threshold() -> None:
    result = gate.evaluate(LAYERS[Layer.SMOKE], _run(passed=1, failed=1))
    assert result.pass_rate == pytest.approx(0.5)
    assert len(result.violations) == 1
    assert gate.exit_code_for(result) == 1


def test_gate_fails_when_nothing_collected() -> None:
    """0 条也是未达标：marker 拼错时它必须是红灯，而不是「没有失败所以通过」。"""
    result = gate.evaluate(LAYERS[Layer.SMOKE], _run(passed=0))
    assert result.total == 0
    assert any("未收集到任何用例" in v for v in result.violations)
    assert gate.exit_code_for(result) == 1


def test_all_errors_cannot_be_a_pass() -> None:
    """全 error（例如 6 条 ui 用例因无浏览器而 error）通过率记 0，不记 1。"""
    result = gate.evaluate(LAYERS[Layer.NIGHTLY], _run(passed=0, errors=6))
    assert result.pass_rate == 0.0
    assert gate.exit_code_for(result) == 1


def test_skipped_and_xfailed_do_not_enter_pass_rate() -> None:
    result = gate.evaluate(LAYERS[Layer.NIGHTLY], _run(passed=10, skipped=5, xfailed=3))
    assert result.pass_rate == pytest.approx(1.0)
    assert result.violations == ()


def test_errors_within_budget_do_not_block() -> None:
    """正例：error 数在预算内不阻断——nightly 预算 3，2 条 error 仍算达标。"""
    result = gate.evaluate(LAYERS[Layer.NIGHTLY], _run(passed=36, errors=2))
    assert result.errors == 2
    assert result.violations == ()
    assert gate.exit_code_for(result) == 0


def test_errors_over_budget_block_the_gate() -> None:
    """反例：smoke 层 total=36 passed=30 errors=6，通过率 100% 但 error 超预算 0。

    这条正是本次改动的靶子：``decisive = passed + failed = 30``、``pass_rate = 1.0``，
    若 error 判据挂在 pass_rate 分支之后，它会走 pass_rate 分支判达标。
    """
    result = gate.evaluate(LAYERS[Layer.SMOKE], _run(passed=30, errors=6))
    assert result.pass_rate == pytest.approx(1.0)
    assert len(result.violations) == 1
    assert "error 数" in result.violations[0]
    assert gate.exit_code_for(result) == 1


def test_errors_exactly_at_budget_is_not_a_violation() -> None:
    """边界：``errors == max_errors`` 等于预算，不算违规（判据是严格大于）。"""
    result = gate.evaluate(LAYERS[Layer.NIGHTLY], _run(passed=36, errors=3))
    assert result.errors == LAYERS[Layer.NIGHTLY].max_errors == 3
    assert result.violations == ()
    assert gate.exit_code_for(result) == 0


def test_over_budget_duration_fails_the_gate() -> None:
    result = gate.evaluate(LAYERS[Layer.SMOKE], _run(passed=10, duration=1000.0))
    assert result.within_budget is False
    assert any("超出预算" in v for v in result.violations)
    assert gate.exit_code_for(result) == 1


def test_thresholds_are_ordered_by_layer_aggressiveness() -> None:
    assert LAYERS[Layer.SMOKE].min_pass_rate == 1.0
    assert LAYERS[Layer.PR].min_pass_rate == 1.0
    assert LAYERS[Layer.NIGHTLY].min_pass_rate < 1.0
    assert LAYERS[Layer.RELEASE].min_pass_rate <= LAYERS[Layer.NIGHTLY].min_pass_rate


# ------------------------------------------------------------------ CLI 退出码


def test_cli_gate_returns_zero_on_pass(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        gate,
        "run_layer",
        lambda *a, **k: gate.PytestRun(
            exit_code=0,
            duration_seconds=1.0,
            counts={"passed": 3, "total": 3},
            cases=(),
            markers={},
            files={},
        ),
    )
    code = cli.main(["--db", str(tmp_path / "pass.db"), "gate", "--layer", "smoke", "--commit", "testsha"])
    assert code == 0


def test_cli_gate_returns_one_on_fail_and_still_records(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db = tmp_path / "fail.db"
    monkeypatch.setattr(
        gate,
        "run_layer",
        lambda *a, **k: gate.PytestRun(
            exit_code=1,
            duration_seconds=2.0,
            counts={"passed": 1, "failed": 1, "total": 2},
            cases=(("testcases/api/test_x.py::test_a", "failed", 0.1),),
            markers={},
            files={},
        ),
    )
    code = cli.main(["--db", str(db), "gate", "--layer", "smoke", "--commit", "testsha"])
    assert code == 1

    # 落库发生在比对之前：红灯轮次必须留痕，否则 flaky 检测永远缺失败样本。
    from qaplat.metrics import MetricsStore

    store = MetricsStore(db)
    runs = store.recent_runs()
    store.close()
    assert len(runs) == 1
    assert runs[0].failed == 1
    assert runs[0].commit_sha == "testsha"


def test_cli_unknown_layer_is_usage_error(tmp_path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--db", str(tmp_path / "x.db"), "gate", "--layer", "smooke"])
    assert excinfo.value.code == 2


def test_cli_without_command_prints_help_and_returns_usage_error() -> None:
    assert cli.main([]) == 2


def test_cli_subcommand_group_without_child_returns_zero() -> None:
    assert cli.main(["registry"]) == 0
