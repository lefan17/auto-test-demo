"""traffic-replay 的测试夹具入口。由调度者冻结，子代理不得修改。

只做两件事：把仓库根加进 sys.path，好让 `import recorder / replay / analyze` 生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
