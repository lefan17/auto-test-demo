"""用例 ↔ operationId 覆盖矩阵。

**诚实性红线**：覆盖归属只有两个来源，二者都没有就算未覆盖。

1. **显式标注** —— ``@pytest.mark.covers("login_api_v1_auth_login_post")``（marker 名 ``covers``）
2. **源码路径字符串** —— 用例源码里出现的接口路径字面量，例如
   ``("GET /api/v1/materials", 401)`` 里的 ``/api/v1/materials``

不许做任何推断：不按文件名猜、不按 marker 名猜、不按目录归属猜、不把「同模块里有一条
用例覆盖了某路径」推广到同模块其它用例。猜出来的覆盖率报表比没有报表更危险，因为它会
让人以为某个接口被测过。

代价是显式可见的：``testcases/api/**`` 那 42 条用例走 ``apis/`` 封装对象发请求，源码里
一个路径字面量都没有，因此在矩阵里全部表现为「不贡献覆盖」——这是本机制如实反映的
现状，不是工具缺陷。要提升覆盖率，正确的做法是在用例上加 ``covers`` marker。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from qaplat.paths import CACHE_DIR, DEFAULT_SPEC_PATH, ROOT

#: 源码路径字符串的提取范围：以 ``/api`` 开头的字面量。
_PATH_LITERAL_RE = re.compile(r"(?<![\w.])(/api[A-Za-z0-9_\-./{}]*)")

#: 路径归一化时把 ``{...}`` 占位符统一成 ``{}``，好让 ``f"/api/v1/materials/{code}"``
#: 与 spec 里的 ``/api/v1/materials/{material_code}`` 能对上。
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")

#: 平台层自测目录（契约 §五）：这里出现的 ``/api...`` 字面量是喂给提取器的**输入样本**，
#: 不是对被测服务的调用，因此不参与路径归属。注意用 Path 组件级前缀比较，而不是字符串
#: ``startswith``——否则 ``testcases/platform_extra/`` 会被误伤。
PLATFORM_SELF_TEST_DIR = ROOT / "testcases" / "platform"

COVERAGE_JSON = CACHE_DIR / "registry.json"


@dataclass(frozen=True)
class OperationCoverage:
    operation_id: str
    method: str
    path: str
    covered_by: tuple[str, ...]


@dataclass(frozen=True)
class CaseInfo:
    node_id: str
    markers: tuple[str, ...]
    referenced_paths: tuple[str, ...]
    covers: tuple[str, ...]


def normalize_path(path: str) -> str:
    """接口路径归一化：占位符统一、去掉查询串与结尾斜杠。

    ``/api/v1/materials/{material_code}`` 与 ``/api/v1/materials/{code}`` 归一化后都是
    ``/api/v1/materials/{}``——参数名不同但指向同一个 operation，这属于合理的等价，
    不属于「猜」。
    """
    text = path.split("?", 1)[0].strip().rstrip("/")
    text = _PLACEHOLDER_RE.sub("{}", text)
    return text or "/"


def load_operations(spec_path: str | Path = DEFAULT_SPEC_PATH) -> list[tuple[str, str, str]]:
    """从 OpenAPI 文档读出 ``(operation_id, method, path)``，按 spec 里的顺序。

    ``operationId`` 缺失时用 ``f"{method} {path}"`` 兜底——契约里允许没有 operationId 的
    spec，硬造一个 id 会让覆盖矩阵与其他工具的 id 对不上。
    """
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    paths = spec.get("paths") or {}
    operations: list[tuple[str, str, str]] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
                continue
            if not isinstance(operation, dict):
                continue
            method_upper = method.upper()
            operation_id = str(operation.get("operationId") or f"{method_upper} {path}")
            operations.append((operation_id, method_upper, str(path)))
    return operations


def extract_paths_from_source(text: str) -> tuple[str, ...]:
    """从一段源码文本里提取 ``/api...`` 路径字面量（已归一化、去重、稳定排序）。"""
    return tuple(sorted({normalize_path(match) for match in _PATH_LITERAL_RE.findall(text)}))


def is_platform_self_test(file_path: str | Path) -> bool:
    """该文件是否位于平台层自测目录下（契约 §五「自测排除」）。

    用 ``Path.parents`` 做组件级比较，所以 ``testcases/platform_extra/x.py`` 不会被误判。
    路径先 ``resolve()``，相对路径按当前工作目录（pytest 下即仓库根）解析。
    """
    try:
        resolved = Path(file_path).resolve()
    except OSError:
        return False
    return resolved == PLATFORM_SELF_TEST_DIR or PLATFORM_SELF_TEST_DIR in resolved.parents


def read_referenced_paths(file_path: str | Path) -> tuple[str, ...]:
    """读用例源码文件并提取路径字面量。读不到就返回空元组，不抛异常。

    平台层自测目录下的文件一律返回空元组：那里的 ``/api...`` 字面量是喂给提取器的
    输入样本，不是对被测服务的调用（契约 §五）。
    """
    if is_platform_self_test(file_path):
        return ()
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return extract_paths_from_source(text)


def build_coverage(cases: Sequence[CaseInfo], operations: Sequence[tuple[str, str, str]]) -> list[OperationCoverage]:
    """把用例的归属信息投影到 operation 上。

    两条来源分别命中并**合并**（同一用例既写了 covers 又引用了路径时只出现一次）；
    ``covered_by`` 稳定排序，保证同一份输入永远产出同一份矩阵，diff 才有意义。
    """
    path_index: dict[str, set[str]] = {}
    covers_index: dict[str, set[str]] = {}
    for case in cases:
        for path in case.referenced_paths:
            path_index.setdefault(normalize_path(path), set()).add(case.node_id)
        for operation_id in case.covers:
            covers_index.setdefault(operation_id, set()).add(case.node_id)

    coverage: list[OperationCoverage] = []
    for operation_id, method, path in operations:
        owners = set(covers_index.get(operation_id, ()))
        owners |= path_index.get(normalize_path(path), set())
        coverage.append(
            OperationCoverage(
                operation_id=operation_id,
                method=method,
                path=path,
                covered_by=tuple(sorted(owners)),
            )
        )
    return coverage


def uncovered(coverage: Sequence[OperationCoverage]) -> list[OperationCoverage]:
    """未覆盖的 operation，按 ``operation_id`` 稳定排序。"""
    return sorted((c for c in coverage if not c.covered_by), key=lambda c: c.operation_id)


def owner_files(covered_by: Sequence[str]) -> list[tuple[str, int]]:
    """把归属用例按源文件归组：``[(文件, 用例数), ...]``，用例多的文件在前。

    为什么需要这个视图：参数化用例（``testcases/contract/test_schemathesis_properties.py``
    一条函数生成 188 条用例）会让单个 operation 的 ``covered_by`` 挂上上百个 nodeid。
    归因粒度是**文件级**——那个文件里的路径字面量属于文件里的每一条用例——所以
    「188 条用例覆盖某接口」这句话在计数上会误导人。按文件归组后读起来才是事实：
    「1 个文件（188 条用例）引用了这个路径」。计数口径写进 docs/platform-usage.md。
    """
    counts: dict[str, int] = {}
    for node_id in covered_by:
        file_part = node_id.split("::", 1)[0]
        counts[file_part] = counts.get(file_part, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def coverage_ratio(coverage: Sequence[OperationCoverage]) -> float:
    """覆盖率 = 有归属的 operation / 全部 operation；没有 operation 时返回 0.0。"""
    if not coverage:
        return 0.0
    return sum(1 for c in coverage if c.covered_by) / len(coverage)


#: pytest 的退出码 5 = 「一个用例都没收集到」；这是 collect_cases 唯一允许返回空列表的情形。
_EXIT_NO_TESTS_COLLECTED = 5


class CaseCollectionError(RuntimeError):
    """采集子进程的结论既拿不到、退出码又说不出个所以然。

    这是**观测通道坏了**，不是「仓库里没有用例」：``--collect-only`` 只有在一条都没收集到时
    才以 5 退出，其它退出码下拿到零结果，只可能是 dump 没写成、stdout 形态变了、或子进程
    自己失败了。此时返回空列表会把它伪装成「这份 spec 没有任何用例覆盖」——那正是
    ``registry.py`` 开头那条诚实性红线上最危险的一种错误，所以这里选择直接吵。
    """


def collect_cases(pytest_args: Sequence[str] = ()) -> list[CaseInfo]:
    """收集用例并附上 markers / 路径归属。

    子进程命令是契约指定的 ``pytest --collect-only -q -q --no-header``（``-q`` 写两遍
    才会输出「一行一个 nodeid」的纯文本，一遍是给人看的树形结构）。额外挂
    ``-p qaplat._dump``，让同一次收集顺带把 markers 与源文件路径带回来——markers 无法
    从 ``--collect-only`` 的文本里得到，多跑一次收集只为了拿 marker 属于浪费。

    收集阶段报错（例如缺 schemathesis 这类可选依赖）时 pytest 会中断并只返回部分结果；
    这里把已拿到的部分原样返回，缺哪些由调用方通过覆盖率数字与 ``registry gaps`` 看到。

    但**一条都拿不到**是另一回事：那说明采集通道本身坏了（dump 没写成、stdout 形态变了、
    子进程起不来），不是「仓库里没有用例」。此时除 ``exit 5``（pytest 对「零收集」的专用
    退出码）外一律抛 :class:`CaseCollectionError`，把返回码与两路输出的尾部带出来，
    而不是返回一个看着像「没覆盖」的空列表。
    """
    # 用 with 而不是裸 mkdtemp：收集一次就漏一个目录，跑几十轮后临时区里
    # 堆满几百个只有 collect.json 的 qaplat-collect-*，排查别的问题时全是噪声。
    with tempfile.TemporaryDirectory(prefix="qaplat-collect-") as tmp_dir:
        dump_path = Path(tmp_dir) / "collect.json"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-q",
            "--no-header",
            "-p",
            "qaplat._dump",
            "--qaplat-dump",
            str(dump_path),
            *[str(item) for item in pytest_args],
        ]
        # 与 gate.run_layer 同样的理由：管道里的子进程默认按 cp936 输出，这里按 utf-8 解码，
        # 中文用例名会变成替换字符（nodeid 本身是 ASCII 所以不一定立刻暴露）。
        child_env = dict(os.environ)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            argv,
            cwd=str(ROOT),
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        payload: dict[str, object] = {}
        dump_note = "不存在"
        if dump_path.exists():
            try:
                loaded = json.loads(dump_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
                    dump_note = "已读入"
                else:
                    dump_note = f"存在但顶层不是对象（{type(loaded).__name__}）"
            except (OSError, json.JSONDecodeError) as exc:
                payload = {}
                dump_note = f"存在但读不出来（{type(exc).__name__}: {exc}）"

    markers = _as_str_map(payload.get("markers"))
    files = _as_str_map(payload.get("files"))
    covers = _as_str_map(payload.get("covers"))
    node_ids = sorted(markers) or _node_ids_from_stdout(proc.stdout or "")

    if not node_ids and proc.returncode != _EXIT_NO_TESTS_COLLECTED:
        raise CaseCollectionError(_collection_failure_report(proc, pytest_args, dump_note, payload))

    cases: list[CaseInfo] = []
    for node_id in node_ids:
        case_markers = markers.get(node_id, ())
        # ``files`` 走的是同一套「值是元组」的归一化，单值源文件取第一个元素。
        file_entries = files.get(node_id, ())
        source = file_entries[0] if file_entries else None
        if source is None and "::" in node_id:
            source = str(ROOT / node_id.split("::", 1)[0])
        referenced = read_referenced_paths(source) if source else ()
        cases.append(
            CaseInfo(
                node_id=node_id,
                markers=tuple(case_markers),
                referenced_paths=tuple(referenced),
                covers=tuple(covers.get(node_id, ())),
            )
        )
    return cases


def _as_str_map(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        if isinstance(item, str):
            out[str(key)] = (item,)
        elif isinstance(item, Iterable):
            out[str(key)] = tuple(str(x) for x in item)
    return out


def _node_ids_from_stdout(text: str) -> list[str]:
    """从 ``--collect-only -q -q`` 的输出里抠出 nodeid。

    只认形如 ``path::name`` 的行：末尾的 ``363 tests collected in 0.66s`` 与 warnings
    summary 里的 ``jsonschema.RefResolutionError,`` 这类行都不含 ``::``，自然被过滤掉。
    """
    ids: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "::" in stripped and not stripped.startswith(("=", "-", "!", "E ")):
            ids.append(stripped)
    return ids


def _tail(text: str, lines: int = 12, width: int = 200) -> str:
    """取输出的最后几行，每行截到 ``width`` 个字符。

    整段 stdout/stderr 可能有上千行（收集失败时 pytest 会把 traceback 全吐出来），
    异常信息里只需要「末尾发生了什么」，所以从这里开始截。
    """
    kept = [line for line in (text or "").splitlines() if line.strip()][-lines:]
    if not kept:
        return "（空）"
    return "\n".join(line[:width] for line in kept)


def _collection_failure_report(
    proc: subprocess.CompletedProcess[str],
    pytest_args: Sequence[str],
    dump_note: str,
    payload: dict[str, object],
) -> str:
    """把「一条都没拿到」的现场拼成一段可直接读的异常信息。

    四样东西缺一不可：退出码（子进程到底怎么了）、目标参数（复现这一条要敲什么）、
    两条来源各自的形态（dump 有没有读到、stdout 抠出几行 nodeid）、以及两个输出的尾部
    （真正的报错原文在这里）。只写「收集失败」等于把排查工作原样退回给读日志的人。
    """
    return (
        "collect_cases 一条用例都没拿到，但子进程不是以 5（零收集）退出，"
        "因此这更像采集通道坏了而不是仓库里没有用例。\n"
        f"  returncode: {proc.returncode}\n"
        f"  目标参数: {[str(item) for item in pytest_args]!r}\n"
        f"  dump: {dump_note}；payload 顶层键: {sorted(payload)!r}\n"
        f"  stdout 抠出 nodeid: {len(_node_ids_from_stdout(proc.stdout or ''))} 条\n"
        f"  stdout 尾部:\n{_tail(proc.stdout or '')}\n"
        f"  stderr 尾部:\n{_tail(proc.stderr or '')}"
    )


def load_coverage_matrix(json_path: str | Path = COVERAGE_JSON) -> list[OperationCoverage]:
    """读回 ``registry build`` 落盘的矩阵（给 report 用）。"""
    path = Path(json_path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        OperationCoverage(
            operation_id=str(item["operation_id"]),
            method=str(item["method"]),
            path=str(item["path"]),
            covered_by=tuple(item.get("covered_by", ())),
        )
        for item in data.get("coverage", [])
    ]


def dump_coverage_json(
    coverage: Sequence[OperationCoverage],
    operations_count: int,
    cases_count: int,
    json_path: str | Path = COVERAGE_JSON,
) -> Path:
    """把矩阵写成 JSON。"""
    path = Path(json_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "operations_total": operations_count,
        "cases_total": cases_count,
        "covered": sum(1 for c in coverage if c.covered_by),
        "coverage_ratio": coverage_ratio(coverage),
        "coverage": [
            {
                "operation_id": c.operation_id,
                "method": c.method,
                "path": c.path,
                "covered_by": list(c.covered_by),
            }
            for c in coverage
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
