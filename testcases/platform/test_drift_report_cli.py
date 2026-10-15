"""契约漂移门禁、失败归因、报表与 CLI 端到端。

漂移用**假检查脚本**驱动，而不是真的调 ``scripts/export_openapi.py``：真脚本验的是
「当前代码与基线文件是否一致」，它的结论会随别人改代码而变；门禁要测的是「脚本报告漂移
时我们是否正确落库并返回 1」，这个结论必须与我无关地稳定。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qaplat import classify, cli, report
from qaplat.classify import Attribution, Failure
from qaplat.metrics import MetricsStore, build_run_record, case_outcomes_from_pairs, new_run_id, utc_now
from qaplat.paths import ROOT

pytestmark = pytest.mark.platform


def _write_script(tmp_path: Path, *, drifted: bool) -> Path:
    """写一个假的漂移检查脚本：退出码 0 表示一致，1 表示漂移。"""
    script = tmp_path / ("drift.py" if drifted else "clean.py")
    lines = [
        "import sys",
        f"print('[一致] {tmp_path.as_posix()}/docs/openapi.json（80867 字节）')" if not drifted else "",
        "" if not drifted else "print('[漂移] docs/openapi.json 与代码不一致（13 处）')",
        "" if not drifted else "print('[漂移] docs/openapi-login_app.json 与代码不一致（2 处）')",
        f"sys.exit({1 if drifted else 0})",
    ]
    script.write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")
    return script


# ------------------------------------------------------------------ 漂移：无漂移


def test_drift_record_without_drift_returns_zero_and_records(tmp_path) -> None:
    db = tmp_path / "d.db"
    code = cli.main(
        [
            "--db",
            str(db),
            "drift",
            "record",
            "--commit",
            "sha-clean",
            "--script",
            str(_write_script(tmp_path, drifted=False)),
        ]
    )
    assert code == 0
    store = MetricsStore(db)
    history = store.contract_drift_history()
    store.close()
    assert len(history) == 1
    assert history[0].drifted is False
    assert history[0].changed == ()


# ------------------------------------------------------------------ 漂移：有漂移


def test_drift_record_with_drift_returns_one_and_records_changed_files(tmp_path) -> None:
    db = tmp_path / "d.db"
    code = cli.main(
        [
            "--db",
            str(db),
            "drift",
            "record",
            "--commit",
            "sha-drift",
            "--script",
            str(_write_script(tmp_path, drifted=True)),
        ]
    )
    assert code == 1
    store = MetricsStore(db)
    history = store.contract_drift_history()
    store.close()
    assert [item.commit_sha for item in history] == ["sha-drift"]
    assert history[0].drifted is True
    assert set(history[0].changed) == {"docs/openapi.json", "docs/openapi-login_app.json"}


def test_drift_record_on_missing_script_is_usage_error(tmp_path) -> None:
    code = cli.main(
        ["--db", str(tmp_path / "d.db"), "drift", "record", "--commit", "s", "--script", str(tmp_path / "no.py")]
    )
    assert code == 2


def test_real_drift_check_script_exists_and_is_wired() -> None:
    """真脚本必须存在，否则 ``drift record`` 在生产里只会返回用法错误。"""
    assert (ROOT / "scripts" / "export_openapi.py").exists()
    assert (ROOT / "docs" / "openapi.json").exists()


# ------------------------------------------------------------------ 归因分类


@pytest.mark.parametrize(
    ("outcome", "message", "expected"),
    [
        ("passed", "", None),
        ("skipped", "", None),
        ("xfailed", "", Attribution.DEFECT),
        ("error", "playwright: Executable doesn't exist at ...", Attribution.ENVIRONMENT),
        ("error", "ConnectionRefusedError: [WinError 10061]", Attribution.ENVIRONMENT),
        ("failed", "ModuleNotFoundError: No module named 'schemathesis'", Attribution.ENVIRONMENT),
        ("failed", "sqlite3.IntegrityError: UNIQUE constraint failed", Attribution.DATA),
        ("failed", "KeyError: 'material_code'", Attribution.DATA),
        ("failed", "AssertionError: 期望 200，实际 500 # DEF-102", Attribution.DEFECT),
        ("failed", "AssertionError: 期望 200，实际 500", Attribution.REGRESSION),
        ("error", "fixture error with no recognizable signal", Attribution.UNKNOWN),
    ],
)
def test_attribute_rules(outcome: str, message: str, expected: Attribution | None) -> None:
    assert classify.attribute(outcome, message) is expected


def test_markers_are_part_of_the_evidence() -> None:
    """markers 也是证据：xfail 标记的用例失败不是回归。"""
    assert classify.attribute("failed", "AssertionError", markers=("xfail",)) is Attribution.DEFECT


def test_summarize_fills_every_category_for_stable_rendering() -> None:
    counts = classify.summarize(
        [Failure("a.py::test_1", "failed"), Failure("b.py::test_2", "error", "ConnectionRefusedError")]
    )
    assert set(counts) == set(Attribution)
    assert counts[Attribution.REGRESSION] == 1
    assert counts[Attribution.ENVIRONMENT] == 1
    assert counts[Attribution.DATA] == 0


def test_verdict_text_says_regression_when_there_is_one() -> None:
    assert "真实回归" in classify.verdict_text(classify.summarize([Failure("t::x", "failed")]))
    assert "非回归" in classify.verdict_text(classify.summarize([Failure("t::y", "error", "ConnectionRefusedError")]))
    assert classify.verdict_text(classify.summarize([])) == "无失败"


def test_every_attribution_has_a_chinese_label() -> None:
    assert set(classify.LABELS) == set(Attribution)
    assert all(classify.LABELS.values())


# ------------------------------------------------------------------ 报表


def _seed(db: Path) -> None:
    store = MetricsStore(db)
    store.migrate()
    run_id = new_run_id("smoke")
    store.record_run(
        build_run_record(
            run_id=run_id,
            layer="smoke",
            commit_sha="a49b097",
            started_at=utc_now(),
            duration_seconds=24.3,
            counts={"passed": 30, "error": 6},
        ),
        case_outcomes_from_pairs(
            run_id,
            [
                ("testcases/ui/test_login.py::test_login_page", "error", 0.5),
                ("testcases/api/test_a.py::test_ok", "passed", 0.1),
            ],
        ),
    )
    store.record_drift("a49b097", False, [])
    store.close()


def test_report_writes_all_pages_and_reports_counts(tmp_path) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    store = MetricsStore(db)
    result = report.build_report(store, tmp_path / "out")
    store.close()

    assert {p.name for p in result.pages} == {
        "index.html",
        "runs.html",
        "coverage.html",
        "attribution.html",
        "drift.html",
    }
    assert all(p.exists() and p.stat().st_size > 0 for p in result.pages)
    assert result.runs_count == 1
    assert result.cases_count == 36
    # error 只有结论、没有报文，归因不足以判成回归 → 不得算作真实回归。
    assert result.regressions == 0
    html = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert "qaplat 平台报表" in html
    assert "testcases/ui/test_login.py::test_login_page" not in html  # 首页不列用例
    assert "已漂移" not in html


def test_report_without_coverage_shows_empty_state_not_a_fake_matrix(tmp_path) -> None:
    """矩阵文件不存在时覆盖页必须显示「没有数据」，不许伪造一份 0/0 的矩阵。"""
    db = tmp_path / "r.db"
    _seed(db)
    store = MetricsStore(db)
    result = report.build_report(store, tmp_path / "out2", coverage=[])
    store.close()
    assert result.operations_total == 0
    coverage_html = (tmp_path / "out2" / "coverage.html").read_text(encoding="utf-8")
    assert "没有覆盖数据" in coverage_html or "无数据" in coverage_html


def test_report_escapes_html_in_case_ids(tmp_path) -> None:
    """失败摘要里可能出现 ``<`` 或 ``&``，不转义就会撕坏页面结构。"""
    db = tmp_path / "e.db"
    store = MetricsStore(db)
    store.migrate()
    run_id = new_run_id("smoke")
    store.record_run(
        build_run_record(
            run_id=run_id,
            layer="smoke",
            commit_sha=None,
            started_at=utc_now(),
            duration_seconds=1.0,
            counts={"failed": 1},
        ),
        case_outcomes_from_pairs(run_id, [("<script>alert(1)</script>", "failed", 0.1)]),
    )
    report.build_report(store, tmp_path / "out3")
    store.close()
    attribution = (tmp_path / "out3" / "attribution.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in attribution
    assert "&lt;script&gt;" in attribution


def test_report_on_empty_db_is_still_generated(tmp_path) -> None:
    """空库也要出报表：CI 第一次跑的时候 report 不该比 gate 先失败。"""
    store = MetricsStore(tmp_path / "empty.db")
    store.migrate()
    result = report.build_report(store, tmp_path / "out4")
    store.close()
    assert result.runs_count == 0
    assert result.index.exists()


# ------------------------------------------------------------------ CLI 端到端


def test_cli_metrics_show_on_empty_db_returns_zero_and_hints(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--db", str(tmp_path / "m.db"), "metrics", "show"])
    assert code == 0
    assert "无记录" in capsys.readouterr().out


def test_cli_metrics_flaky_on_empty_db_returns_zero(tmp_path) -> None:
    assert cli.main(["--db", str(tmp_path / "m.db"), "metrics", "flaky"]) == 0


def test_cli_metrics_show_prints_recorded_run(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "m.db"
    _seed(db)
    assert cli.main(["--db", str(db), "metrics", "show", "--last", "5"]) == 0
    out = capsys.readouterr().out
    assert "smoke" in out
    assert "30" in out


def test_cli_registry_gaps_with_a_matrix_file_returns_zero(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from qaplat.registry import OperationCoverage, dump_coverage_json

    matrix = tmp_path / "c.json"
    dump_coverage_json(
        [
            OperationCoverage("covered_op", "GET", "/api/v1/covered", ("t.py::test_1",)),
            OperationCoverage("gap_op", "POST", "/api/v1/gap", ()),
        ],
        2,
        1,
        matrix,
    )
    assert cli.main(["--db", str(tmp_path / "g.db"), "registry", "gaps", "--json", str(matrix)]) == 0
    out = capsys.readouterr().out
    assert "gap_op" in out
    assert "covered_op" not in out
    assert json.loads(matrix.read_text(encoding="utf-8"))["coverage_ratio"] == pytest.approx(0.5)


def test_cli_report_generates_files(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "r.db"
    _seed(db)
    out_dir = tmp_path / "html"
    assert (
        cli.main(["--db", str(db), "report", "--out", str(out_dir), "--coverage-json", str(tmp_path / "none.json")])
        == 0
    )
    assert (out_dir / "index.html").exists()
    assert "报表已生成" in capsys.readouterr().out


def test_cli_help_for_every_command_is_zero() -> None:
    """每条子命令的 ``--help`` 都必须可用：契约里列的 CLI 面全部要能自解释。"""
    for argv in (
        ["--help"],
        ["run", "--help"],
        ["gate", "--help"],
        ["registry", "--help"],
        ["registry", "build", "--help"],
        ["registry", "gaps", "--help"],
        ["metrics", "--help"],
        ["metrics", "show", "--help"],
        ["metrics", "flaky", "--help"],
        ["report", "--help"],
        ["drift", "--help"],
        ["drift", "record", "--help"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 0, argv


def test_cli_is_runnable_as_a_module() -> None:
    """契约把入口写成 ``python -m qaplat.cli``，所以模块必须能直接执行。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "qaplat.cli", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0
    assert "gate" in (proc.stdout or "")
