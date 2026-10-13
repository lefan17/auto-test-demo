#!/usr/bin/env python
"""提交信息规范检查（Conventional Commits）。

为什么不用 commitlint：
commitlint 是 Node 生态的工具，要 npm install + 一个 node_modules 目录。
本仓库的**全部**依赖都声明在 requirements.txt 里，且 .venv 是唯一的环境来源——
为了校验一条几十字符的提交信息，引入第二个包管理器、第二个依赖树、
以及"CI 上还要多跑一步 npm ci"，成本与收益完全不成比例。
这一段 Python 覆盖了 commitlint 在本仓库真正会用到的那部分规则
（type 白名单、scope 格式、subject 非空、header 长度、正文空行分隔），
且它跑在已经存在的 Python 上，零新增依赖。

用法（与 commit-msg hook 一致，参数就是提交信息文件路径）：
    python scripts/check_commit_msg.py .git/COMMIT_EDITMSG
也可以直接管道传入：
    echo "feat(ci): 加缓存" | python scripts/check_commit_msg.py -

返回码：0 = 通过，1 = 不合规（并打印可操作的修改建议）。

边界：只检查**格式**，不检查内容对不对。机器能判断的只有格式，
"这条提交该不该拆成两条"是人的判断，不该假装成工具能做的事。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 允许的 type：与仓库现有提交历史对齐（feat / fix / docs / chore / ci / style / test / refactor / perf / build / revert）。
ALLOWED_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

# header 长度上限：与 ruff 的 line-length=120 保持同一量级但更严。
# 72 是 git 自身的经验值（`git log --oneline` 与 GitHub 的提交列表都在这个宽度内不截断）。
MAX_HEADER_LENGTH = 72
MAX_BODY_LINE_LENGTH = 100

HEADER_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<subject>.+)$")

# 自动生成的提交不检查：合并提交、revert 的默认信息、以及 git 自己的模板。
# 把它们判红只会让人学会用 --no-verify，反而把整个 hook 废掉。
SKIP_PREFIXES = (
    "Merge ",
    "Revert ",
    "fixup!",
    "squash!",
    "initial commit",
)


def _read_message(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    path = Path(arg)
    if not path.exists():
        # hook 传进来的文件不存在时不要装成"通过"——那会让 hook 静默失效。
        print(f"[commit-msg] 找不到提交信息文件: {path}", file=sys.stderr)
        raise SystemExit(1)
    # encoding 必须显式指定：Windows 默认 GBK，读中文提交信息会乱码。
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """去掉 git 自动加进 COMMIT_EDITMSG 的注释行（以 # 开头）。"""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def check(message: str) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    text = _strip_comments(message).strip()
    if not text:
        return ["提交信息为空。"]

    lines = text.splitlines()
    header = lines[0].strip()

    if any(header.startswith(prefix) for prefix in SKIP_PREFIXES):
        return []

    problems: list[str] = []

    match = HEADER_RE.match(header)
    if not match:
        problems.append("首行不符合 Conventional Commits 格式，应为 `<type>(<scope>): <subject>`（scope 可省略）。")
    else:
        commit_type = match.group("type")
        scope = match.group("scope")
        subject = match.group("subject").strip()

        if commit_type not in ALLOWED_TYPES:
            problems.append(f"type `{commit_type}` 不在允许列表内；可用：{', '.join(ALLOWED_TYPES)}。")
        if scope is not None:
            if not scope:
                problems.append("scope 写了括号但内容为空，要么删掉括号，要么填上模块名。")
            elif not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", scope):
                problems.append(f"scope `{scope}` 只允许小写字母、数字与 . _ / -，且不能以符号开头。")
        if not subject:
            problems.append("subject 为空。")
        elif subject.endswith("。"):
            # 中文提交里最常见的越界：句号。header 是标题不是句子。
            problems.append("subject 结尾不要加句号。")

    if len(header) > MAX_HEADER_LENGTH:
        problems.append(f"首行 {len(header)} 字符，超过 {MAX_HEADER_LENGTH} 上限（git log --oneline 会截断）。")

    if len(lines) > 1:
        if lines[1].strip():
            problems.append("首行与正文之间必须留一个空行，否则 git 会把它们算作同一段标题。")
        for index, line in enumerate(lines[1:], start=2):
            if len(line) > MAX_BODY_LINE_LENGTH:
                problems.append(f"第 {index} 行 {len(line)} 字符，超过 {MAX_BODY_LINE_LENGTH} 上限。")
                break

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python scripts/check_commit_msg.py <提交信息文件|->", file=sys.stderr)
        return 1

    problems = check(_read_message(argv[1]))
    if not problems:
        print("[commit-msg] 提交信息格式检查通过。")
        return 0

    print("[commit-msg] 提交信息不符合规范：", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\n格式：<type>(<scope>): <subject>\n"
        f"  type   : {', '.join(ALLOWED_TYPES)}\n"
        "  scope  : 可选，如 mes / ci / login\n"
        "  示例   : feat(mes): 补一条并发领料不超卖用例\n"
        "           fix(ci): 修正截图上传路径\n"
        "           docs: 订正 README 里的过期数字",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
