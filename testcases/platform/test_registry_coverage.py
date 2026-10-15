"""覆盖归属不靠猜：两条来源分别验，缺来源时必须如实算未覆盖。

这是整套平台里唯一涉及「诚实性」的部件。归属写错的代价不是测试失败，而是报表上出现
一个看起来已被覆盖、实际没人测的接口——这种错误不会有人发现，所以必须由测试钉死：
``@pytest.mark.covers`` 与源码路径字符串各自独立生效，且**都缺失时归为未覆盖**。
"""

from __future__ import annotations

import json

import pytest

from qaplat import registry
from qaplat.registry import CaseInfo, OperationCoverage, build_coverage, load_operations

pytestmark = pytest.mark.platform


SPEC = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/healthz": {"get": {"operationId": "healthz_api_v1_healthz_get"}},
        "/api/v1/materials": {"post": {"operationId": "create_material_api_v1_materials_post"}},
        "/api/v1/materials/{material_code}": {
            "get": {"operationId": "get_material_api_v1_materials_material_code_get"}
        },
        "/api/v1/no-id": {"delete": {}},
    },
}

OPS = [
    ("healthz_api_v1_healthz_get", "GET", "/api/v1/healthz"),
    ("create_material_api_v1_materials_post", "POST", "/api/v1/materials"),
    ("get_material_api_v1_materials_material_code_get", "GET", "/api/v1/materials/{material_code}"),
    ("DELETE /api/v1/no-id", "DELETE", "/api/v1/no-id"),
]


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------ load_operations


def test_load_operations_keeps_spec_order_and_uppercases_methods(spec_file) -> None:
    assert load_operations(spec_file) == OPS


def test_load_operations_falls_back_when_operation_id_missing(spec_file) -> None:
    """契约允许没有 operationId 的 spec；硬造 id 会让矩阵与其他工具的 id 对不上。"""
    assert ("DELETE /api/v1/no-id", "DELETE", "/api/v1/no-id") in load_operations(spec_file)


def test_load_operations_ignores_non_http_keys(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"paths": {"/x": {"parameters": [{"name": "q"}], "get": {"operationId": "o"}}}}),
        encoding="utf-8",
    )
    assert load_operations(path) == [("o", "GET", "/x")]


def test_load_operations_on_real_repo_spec_has_ids_and_methods() -> None:
    """真契约基准：每个 operation 都必须有 operationId，否则覆盖矩阵无法稳定引用。"""
    operations = load_operations(registry.DEFAULT_SPEC_PATH)
    assert operations, "docs/openapi.json 里没有读出任何 operation"
    assert all(op_id and method.isupper() and path.startswith("/") for op_id, method, path in operations)
    assert len({op_id for op_id, _, _ in operations}) == len(operations)


# ------------------------------------------------------------------ 来源一：源码路径字符串


def test_source_path_literal_claims_the_operation(tmp_path) -> None:
    src = tmp_path / "test_x.py"
    src.write_text('def test_a(client):\n    client.get("/api/v1/materials")\n', encoding="utf-8")
    cases = [CaseInfo("testcases/x/test_x.py::test_a", (), registry.read_referenced_paths(src), ())]
    coverage = {c.operation_id: c for c in build_coverage(cases, OPS)}
    assert coverage["create_material_api_v1_materials_post"].covered_by == ("testcases/x/test_x.py::test_a",)
    assert coverage["healthz_api_v1_healthz_get"].covered_by == ()


def test_source_path_literal_matches_placeholder_regardless_of_param_name(tmp_path) -> None:
    """``{code}`` 与 spec 的 ``{material_code}`` 指同一个 operation，这是等价而不是猜。"""
    src = tmp_path / "test_y.py"
    src.write_text('url = f"/api/v1/materials/{code}"\n', encoding="utf-8")
    cases = [CaseInfo("t.py::test_b", (), registry.read_referenced_paths(src), ())]
    coverage = {c.operation_id: c for c in build_coverage(cases, OPS)}
    assert coverage["get_material_api_v1_materials_material_code_get"].covered_by == ("t.py::test_b",)


def test_dunder_comment_or_docstring_literals_still_count(tmp_path) -> None:
    """路径出现在注释/文档字符串里也算归属：归因粒度是文件级，不做语法级甄别。"""
    src = tmp_path / "test_z.py"
    src.write_text("# 覆盖 POST /api/v1/materials 的正向路径\n", encoding="utf-8")
    cases = [CaseInfo("t.py::test_c", (), registry.read_referenced_paths(src), ())]
    coverage = {c.operation_id: c for c in build_coverage(cases, OPS)}
    assert coverage["create_material_api_v1_materials_post"].covered_by == ("t.py::test_c",)


def test_non_api_literal_is_not_a_claim(tmp_path) -> None:
    src = tmp_path / "test_w.py"
    src.write_text('x = "/login"\ny = "http://localhost:8000/docs"\n', encoding="utf-8")
    assert registry.read_referenced_paths(src) == ()


def test_read_referenced_paths_on_missing_file_is_empty_not_fatal(tmp_path) -> None:
    assert registry.read_referenced_paths(tmp_path / "nope.py") == ()


def test_api_layer_cases_have_no_source_claim_by_design() -> None:
    """已实测事实：testcases/api/** 全走 apis/ 封装对象，源码里没有 ``/api`` 字面量。

    所以源码这条路对 api 层天然覆盖不到——这是诚实性红线的预期结果，不是缺陷。
    如果有人给 apis/ 加了路径常量，这条用例会失败，提醒更新文档口径。
    """
    from qaplat.paths import ROOT

    for path in sorted((ROOT / "testcases" / "api").glob("test_*.py")):
        assert registry.read_referenced_paths(path) == (), f"{path.name} 里出现了 /api 字面量"


# ------------------------------------------------------------------ 来源二：covers marker


def test_explicit_covers_marker_claims_the_operation() -> None:
    cases = [
        CaseInfo(
            node_id="testcases/api/test_user_list.py::test_get_user_list_should_return_200_and_contract",
            markers=("api", "smoke"),
            referenced_paths=(),
            covers=("list_users_api_v1_users_get",),
        )
    ]
    coverage = {
        c.operation_id: c for c in build_coverage(cases, [("list_users_api_v1_users_get", "GET", "/api/v1/users")])
    }
    assert coverage["list_users_api_v1_users_get"].covered_by == (
        "testcases/api/test_user_list.py::test_get_user_list_should_return_200_and_contract",
    )


def test_covers_works_for_operations_the_source_cannot_name() -> None:
    """covers 是给「走封装对象、源码里没有路径字面量」的用例准备的唯一归属途径。"""
    cases = [CaseInfo("t.py::test_e", ("api",), (), ("create_material_api_v1_materials_post",))]
    coverage = {c.operation_id: c for c in build_coverage(cases, OPS)}
    assert coverage["create_material_api_v1_materials_post"].covered_by == ("t.py::test_e",)


# ------------------------------------------------------------------ 两条来源的合并与缺失


def test_both_sources_merge_without_duplicating_a_case(tmp_path) -> None:
    src = tmp_path / "test_m.py"
    src.write_text('client.post("/api/v1/materials")\n', encoding="utf-8")
    cases = [
        CaseInfo("t.py::test_f", (), registry.read_referenced_paths(src), ("create_material_api_v1_materials_post",)),
    ]
    coverage = {c.operation_id: c for c in build_coverage(cases, OPS)}
    assert coverage["create_material_api_v1_materials_post"].covered_by == ("t.py::test_f",)


def test_case_with_neither_source_is_uncovered() -> None:
    """两条来源都没有 → 未覆盖。这是红线：不许用文件名或 marker 猜归属。"""
    cases = [CaseInfo("testcases/ui/test_login.py::test_login_page", ("ui", "smoke"), (), ())]
    coverage = build_coverage(cases, OPS)
    assert all(c.covered_by == () for c in coverage)
    assert len(registry.uncovered(coverage)) == len(OPS)
    assert registry.coverage_ratio(coverage) == 0.0


def test_covered_by_is_stably_sorted_for_diffable_output(tmp_path) -> None:
    src = tmp_path / "test_s.py"
    src.write_text('client.get("/api/v1/healthz")\n', encoding="utf-8")
    paths = registry.read_referenced_paths(src)
    cases = [
        CaseInfo("b.py::test_2", (), paths, ()),
        CaseInfo("a.py::test_1", (), paths, ()),
        CaseInfo("c.py::test_3", (), (), ("healthz_api_v1_healthz_get",)),
    ]
    first = build_coverage(cases, OPS)
    second = build_coverage(list(reversed(cases)), OPS)
    assert first == second
    assert first[0].covered_by == ("a.py::test_1", "b.py::test_2", "c.py::test_3")


# ------------------------------------------------------------------ 覆盖率口径


def test_coverage_ratio_counts_operations_not_cases() -> None:
    coverage = [
        OperationCoverage("a", "GET", "/a", ("x.py::test_1",)),
        OperationCoverage("b", "GET", "/b", ("x.py::test_1", "y.py::test_2")),
        OperationCoverage("c", "GET", "/c", ()),
    ]
    assert registry.coverage_ratio(coverage) == pytest.approx(2 / 3)


def test_coverage_ratio_on_empty_matrix_is_zero() -> None:
    assert registry.coverage_ratio([]) == 0.0


def test_uncovered_is_sorted_by_operation_id() -> None:
    coverage = [
        OperationCoverage("zeta", "GET", "/z", ()),
        OperationCoverage("alpha", "GET", "/a", ()),
        OperationCoverage("mid", "GET", "/m", ("t.py::test",)),
    ]
    assert [c.operation_id for c in registry.uncovered(coverage)] == ["alpha", "zeta"]


def test_owner_files_groups_by_file_and_ranks_by_case_count() -> None:
    """参数化用例会把上百个 nodeid 挂到同一 operation 上，按文件归组才是可读事实。"""
    covered_by = tuple([f"big.py::test_p{i}" for i in range(188)] + ["small.py::test_a"])
    assert registry.owner_files(covered_by) == [("big.py", 188), ("small.py", 1)]


# ------------------------------------------------------------------ 矩阵落盘与读回


def test_matrix_json_roundtrip(tmp_path) -> None:
    coverage = [OperationCoverage("a", "GET", "/a", ("t.py::test_1",))]
    path = registry.dump_coverage_json(coverage, len(OPS), 2, tmp_path / "c.json")
    assert registry.load_coverage_matrix(path) == coverage
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["operations_total"] == len(OPS)
    assert payload["cases_total"] == 2
    assert payload["covered"] == 1


def test_load_coverage_matrix_on_missing_file_is_empty(tmp_path) -> None:
    """矩阵不存在时返回空而不是抛异常：report 要显示「没有覆盖数据」而不是崩掉。"""
    assert registry.load_coverage_matrix(tmp_path / "absent.json") == []


# ------------------------------------------------------------------ collect_cases 子进程


def test_collect_cases_returns_nodeids_with_markers_and_files() -> None:
    """真实子进程收集：一条用例都不能少，且 markers / 源文件都要带回来。

    只收集平台层自己的用例（``testcases/platform/test_gate_layers.py``），避免这个测试
    因为其他目录的依赖缺失而失败——平台层自测的稳定性不该依赖被测仓库里所有可选依赖。
    """
    cases = registry.collect_cases(["testcases/platform/test_gate_layers.py", "--collect-only"])
    assert cases, "collect_cases 没拿到任何用例"
    assert all("::" in case.node_id for case in cases)
    assert all(case.markers for case in cases), "markers 全空说明采集插件没生效"
    assert all(case.referenced_paths == () for case in cases), "平台层自测不该引用接口路径"

    by_name = {case.node_id.split("::", 1)[1]: case for case in cases}
    assert "platform" in by_name["test_four_layers_are_registered"].markers
    assert "parametrize" in by_name["test_build_pytest_args_puts_expression_first[smoke-smoke]"].markers


def test_collect_cases_features_a_second_file(tmp_path) -> None:
    """第二个文件必须被真正读源码、真的能命中 covers —— 端到端验一次两条来源。"""
    target = tmp_path / "probe.py"
    target.write_text(
        "import pytest\n"
        "@pytest.mark.covers('healthz_api_v1_healthz_get')\n"
        "def test_probe_a():\n"
        '    assert "/api/v1/materials"\n',
        encoding="utf-8",
    )
    rewrite = registry.collect_cases([str(target), "--collect-only", "-p", "no:cacheprovider"])
    # 仓库外的文件，pytest 报的 nodeid 相对 rootdir（不是绝对路径），所以只断言尾部。
    assert len(rewrite) == 1
    assert rewrite[0].node_id.endswith("probe.py::test_probe_a")
    case = rewrite[0]
    assert case.covers == ("healthz_api_v1_healthz_get",)
    assert case.referenced_paths == ("/api/v1/materials",)
