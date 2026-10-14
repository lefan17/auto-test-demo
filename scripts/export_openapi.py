"""从 FastAPI 应用导出 OpenAPI 契约并入库，同时提供漂移检测。

为什么要有这个脚本：
- 契约必须与代码同源。"人肉维护一份 CONTRACT 字典"（testcases/login/contract.py 的做法）
  迟早与实现脱节，而脱节的那一刻没人知道。这里改为从 `app.openapi()` 直接导出，
  让契约成为**构建产物**而不是文档。
- 产物入库 + 漂移检测 = 契约 diff 门禁的落点：任何人改了路由/字段/状态码，
  只要忘记重新导出，`--check` 就会非零退出并打印差异摘要。

用法：
    python scripts/export_openapi.py              # 重新生成 docs/ 下的两个契约文件
    python scripts/export_openapi.py --check      # 只比较，不写文件；有漂移则非零退出

为什么同时导出两个服务：
    `mock_server` 是 MES Mock 后端（前缀 /api/v1），`login_app` 是登录演示服务（前缀 /api）。
    仓库里那份手写的契约（testcases/login/contract.py）描述的是 login_app；
    只导出 mock_server 会让"手写契约 vs 自动导出"根本无从对照。
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "docs"

# 应用名 -> (入库文件名, 说明)。文件名固定，不接受命令行指定，
# 否则 CI 里的漂移检测会因为路径不同而失去意义。
APP_FILES: dict[str, tuple[str, str]] = {
    "mock_server": ("openapi.json", "MES Mock 服务（前缀 /api/v1）"),
    "login_app": ("openapi-login_app.json", "登录演示服务（前缀 /api）"),
}


def build_spec(app_name: str) -> dict[str, object]:
    """构建指定 FastAPI 应用的 OpenAPI 文档对象。

    `create_app()` 会顺带初始化 SQLite（建表、写种子数据），所以这里显式传一个临时库路径，
    避免在仓库里留下 dsh_mes.db。spec 内容与库路径无关，因此不影响产物可复现性。
    """
    if app_name == "mock_server":
        from mock_server.app import create_app

        with tempfile.TemporaryDirectory(prefix="openapi-export-") as tmp:
            app = create_app(db_path=str(Path(tmp) / "export.db"))
            return app.openapi()
    if app_name == "login_app":
        from login_app.app import create_app as create_login_app

        return create_login_app().openapi()
    raise SystemExit(f"未知应用：{app_name}（可选：{', '.join(APP_FILES)}）")


def dumps(spec: dict[str, object]) -> str:
    """把一个 spec 序列化成稳定的文本。

    sort_keys=True 让产物与 dict 插入顺序无关；ensure_ascii=False 保留中文描述的可读性；
    固定结尾换行，避免 Windows/Linux 之间的尾字节差异。
    """
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def diff_summary(expected: str, actual: str, limit: int = 40) -> str:
    """给出两份 spec 文本的差异摘要（截断到 limit 行）。"""
    diff = list(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="入库文件",
            tofile="重新导出",
            lineterm="",
            n=1,
        )
    )
    if not diff:
        return "（文本内容一致，差异可能来自行尾或编码）"
    shown = diff[:limit]
    text = "\n".join(shown)
    if len(diff) > limit:
        text += f"\n... 共 {len(diff)} 行差异，此处只显示前 {limit} 行"
    return text


def write_one(app_name: str, out_dir: Path, check: bool) -> bool:
    """写出或校验一个应用的契约文件，返回是否一致（写模式恒为 True）。"""
    filename, description = APP_FILES[app_name]
    target = out_dir / filename
    expected = dumps(build_spec(app_name))

    if check:
        if not target.is_file():
            print(f"[漂移] {target} 不存在，请先运行 python scripts/export_openapi.py", file=sys.stderr)
            return False
        if target.read_text(encoding="utf-8") != expected:
            print(f"[漂移] {target} 与代码不一致（{description}）", file=sys.stderr)
            print(diff_summary(target.read_text(encoding="utf-8"), expected), file=sys.stderr)
            return False
        print(f"[一致] {target}（{len(expected.encode('utf-8'))} 字节）")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8", newline="\n")
    print(f"[写出] {target}（{len(expected.encode('utf-8'))} 字节，{description}）")
    return True


def _force_utf8_streams() -> None:
    """把标准输出/错误固定成 UTF-8。

    脚本会打印中文（[漂移]/[一致]/[写出]），而 Windows 上管道默认按 cp936 编码，
    CI 里用 UTF-8 解码日志就会得到乱码，排查漂移时反而看不清。显式固定，
    与仓库其它 Windows 相关处理（mock_server_ctl 设 PYTHONIOENCODING）保持一致。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 FastAPI 的 OpenAPI 契约，或检测契约漂移")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比较入库文件与代码重新导出的结果，不写文件；不一致时返回 1",
    )
    parser.add_argument(
        "--app",
        choices=[*APP_FILES, "all"],
        default="all",
        help="要处理的应用（默认 all）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="契约文件所在目录（默认 docs/）")
    args = parser.parse_args(argv)

    _force_utf8_streams()
    out_dir = Path(args.out_dir)
    apps = list(APP_FILES) if args.app == "all" else [args.app]
    ok = all(write_one(name, out_dir, args.check) for name in apps)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
