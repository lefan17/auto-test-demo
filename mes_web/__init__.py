"""本地 Demo Web 包：服务端渲染 HTML + 服务端转发到 mock_server。

包名必须是 `mes_web`（FROZEN_MESWEB.md §2）：仓库根 `conftest.py` 会把根目录插到
`sys.path[0]`，任何与标准库同名的目录（`platform/`、`types/`、`json/` …）都会遮蔽标准库，
而 pytest 自己启动就要 `import platform`，整套测试会崩在收集阶段。

这里刻意不做 `from mes_web.app import create_app` 之类的再导出：`app.py` 会连带导入
fastapi/requests，保持包 `__init__` 无副作用，导入 `mes_web` 本身永不失败。
"""
