#!/usr/bin/env python
"""依赖清单一致性检查：`requirements.txt` ↔ `requirements.lock`。

## 为什么需要这个脚本

本仓库的依赖有两份文本：`requirements.txt` 是**人写的直接依赖**，`requirements.lock`
是 `pip-compile` 从它编译出来的**全量钉死清单**（直接依赖 + 全部传递依赖）。
`pyproject.toml` 的 `[project].dependencies` 故意留空，理由写在那份文件里：
「依赖仍然声明在 requirements.txt，不搬到这里来。搬迁的代价是两份真值来源」。
也就是说这里没有第三份清单可以拿来对照——能互相对照的只有这两份。

问题在于编译是**手工**跑的，仓库里没有任何机制校验它们还同步。忘了重跑
`pip-compile` 就会出现最糟的组合：

* CI 装的是 `requirements.lock` → 流水线全绿；
* 而 `pip install -r requirements.txt`（README 给新同事的第一条命令）装的是另一套版本
  → 数字对不上，新同事复现不出 CI 的结论。

一份**看起来一直是对的**依赖清单，比一份明显过时的清单危险得多。这与仓库里
反复记的「静默放行」是同一类问题：错的东西不报错，只是安静地不同——`tools/check_retry.py`
被 README 当证据引用却从未在 CI 上跑过，也是同一类。

## 口径

1. 逐行读 `requirements.txt`：跳过空行与 `#` 开头的注释行，其余每行必须解析成
   一个 `name==version` 形式的 pin。解析不了的行当场报错，不静默忽略
   （静默忽略会让「写错格式」变成「检查通过」）。
2. 逐行读 `requirements.lock`：只取 pin 行，跳过注释行与 pip-compile 生成的
   `# via ...` 续行（那些缩进的 `#` 行是溯源信息，不是 pin）。
3. **断言方向是单向的**：`requirements.txt` 里的每个包都必须在 lock 里出现，且版本
   一致。**lock 里有而 txt 里没有的，是合法的传递依赖**（当前 45 个），不算漂移。
   这一条必须写死——否则以后一定有人把它当成 bug「修」掉，而那会把 pip-compile 的
   产物削回只有直接依赖，等于悄悄取消全量钉死。
4. 版本比较按**规范字符串逐字**比较，不做版本号语义解析（`2.34` 与 `2.34.2` 算不同）。
   理由与本仓库既有决策一致：这些依赖**全部是精确 pin**，逐字比较覆盖全部真实场景；
   引入 `packaging` 解析就等于顺带承认 `>=` / `~=` 这类写法合法，而本仓库从不使用它们，
   判定规则白白变复杂。检查脚本一旦有分支，就会有没人跑的分支。
5. 包名比较**大小写不敏感且归一化**：按 PEP 503 把 `[-_.]+` 统一成 `-` 再转小写，
   所以 `PyYAML` ↔ `pyyaml`、`pytest-cov` ↔ `pytest_cov` 是同一个包，不算漂移
   （pip 自己就是这么判等价的，逐字比包名会产出假红灯）。归一化只用于**比对**，
   报错时打印的仍是两个文件里的原始写法，方便直接定位改哪一行。

用法::

    python scripts/check_deps_sync.py

退出码：0 = 一致；1 = 有漂移（逐行打印哪个文件第几行、期望什么、实际什么）。
没有第三种退出码：这个脚本只回答「两份清单是不是同一件事」。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECT = ROOT / "requirements.txt"
DEFAULT_LOCK = ROOT / "requirements.lock"

# `name==version`：name 允许 PEP 503 的三种分隔符，可带 extras（extras 不参与名字比对）。
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<spec>[^=\s]+)$")
_NORMALIZE_RE = re.compile(r"[-_.]+")


def _force_utf8_stdio() -> None:
    """让输出在所有平台上都是 UTF-8。

    不是洁癖：本脚本要打印 `↔`，而 Windows 控制台默认是 GBK，实测会直接抛
        UnicodeEncodeError: 'gbk' codec can't encode character '\\u2194'
    也就是这个检查在 Windows 上**还没开始比对就先崩了**。

    pytest 的捕获对象例外：它是 TextIOWrapper 子类，`reconfigure` 存在但重配会打断
    capsys 读取，而这些对象本身已经是 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        if type(stream).__module__.split(".")[0] == "_pytest":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # 已被重定向成不可重配置的对象
                pass


_force_utf8_stdio()


@dataclass(frozen=True)
class Pin:
    """一行 `name==version`。`line_no` 是 1 起算的行号，只用于报错定位。"""

    name: str
    spec: str
    line_no: int
    raw: str

    @property
    def key(self) -> str:
        """PEP 503 归一化后的包名，比对用这个，不要用 `name`。"""
        return _NORMALIZE_RE.sub("-", self.name).lower()


class UnparsableLine(Exception):
    """一行既不是注释也不是 `name==version`。带上文件名与行号一起报。"""


def parse_pin(raw: str, line_no: int) -> Pin | None:
    """解析一行。注释行与空行返回 None，其余解析不了就抛 `UnparsableLine`。"""
    text = raw.split(" #", 1)[0].strip()  # 行尾注释（`pytest==9.0.3  # 理由`）
    if not text or text.startswith("#"):
        return None
    if text.endswith("\\") or text.startswith("-"):
        raise UnparsableLine(f"第 {line_no} 行是续行或 pip 选项，不是 name==version 形式的 pin：{text}")
    match = _PIN_RE.match(text)
    if match is None:
        raise UnparsableLine(f"第 {line_no} 行不是 name==version 形式的 pin：{text}")
    return Pin(name=match["name"], spec=match["spec"], line_no=line_no, raw=text)


def read_pins(path: Path) -> tuple[dict[str, Pin], list[str]]:
    """读出一个文件里的 pin。第二个返回值是解析失败的行（文本化后便于逐条打印）。"""
    pins: dict[str, Pin] = {}
    problems: list[str] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            pin = parse_pin(raw, line_no)
        except UnparsableLine as exc:
            problems.append(f"  {path.name}:{line_no} {exc}")
            continue
        if pin is None:
            continue
        pins.setdefault(pin.key, pin)  # 同一文件里重复声明时保留第一处，报错行号更靠前
    return pins, problems


def compare(direct: dict[str, Pin], locked: dict[str, Pin]) -> list[str]:
    """单向比对：直接依赖必须都在 lock 里且版本逐字一致。lock 多出的不报。"""
    issues: list[str] = []
    for key, want in direct.items():
        got = locked.get(key)
        if got is None:
            issues.append(
                f"  {want.raw}：requirements.txt:{want.line_no} 声明了，requirements.lock 里没有这个包"
                "（lock 需要重跑 pip-compile）"
            )
        elif got.spec != want.spec:
            issues.append(
                f"  {want.name} 版本漂移："
                f"requirements.txt:{want.line_no} 写 {want.spec}，"
                f"requirements.lock:{got.line_no} 写 {got.spec}"
            )
    return issues


def main(direct_path: Path = DEFAULT_DIRECT, lock_path: Path = DEFAULT_LOCK) -> int:
    """返回退出码。两个路径参数只为测试注入，正常调用一律用默认值。"""
    missing = [path for path in (direct_path, lock_path) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"[失败] 缺少 {path}", file=sys.stderr)
        return 1

    direct, direct_problems = read_pins(direct_path)
    locked, lock_problems = read_pins(lock_path)
    problems = direct_problems + lock_problems
    issues = compare(direct, locked)

    if problems or issues:
        if problems:
            print("[失败] 有解析不了的行：", file=sys.stderr)
            for problem in problems:
                print(problem, file=sys.stderr)
        if issues:
            print(f"[失败] {direct_path.name} ↔ {lock_path.name} 漂移 {len(issues)} 处：", file=sys.stderr)
            for issue in issues:
                print(issue, file=sys.stderr)
        print(
            "\n两份必须一起改：改 requirements.txt 之后要重跑 pip-compile 重生成 lock。"
            "理由见 scripts/check_deps_sync.py 的文件头——"
            "漂移会产出「CI 绿，而 pip install -r requirements.txt 装的是另一套版本」这种最危险的组合。",
            file=sys.stderr,
        )
        return 1

    excess = len(locked) - len(direct)
    print(
        f"[通过] {direct_path.name} ↔ {lock_path.name}："
        f"{len(direct)} 条直接依赖逐字一致；lock 另有 {excess} 个传递依赖（合法，不算漂移）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
