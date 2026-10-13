name: 🐛 缺陷报告
description: 用例失败、框架行为异常、被测服务与契约不符
title: "[Bug] "
labels: ["bug", "triage"]
assignees: []
body:
  - type: markdown
    attributes:
      value: |
        ## 先做两件事，能省掉一半的来回

        1. **确认这不是"已知缺陷"**。本仓库的 `login_app/` 是被测服务，
           **故意保留了 6 个缺陷**（FIND-01~06），`-m login` 与 `-m mes` 的用例
           钉住的是"当前行为就是这样"。差异表在 `docs/login-contract.md`。
           如果你看到的"错误"在那张表里，那它就不是 bug，是契约。
        2. **确认失败可复现**。请贴出完整命令与输出，不要只写"跑不过"。

  - type: dropdown
    id: scope
    attributes:
      label: 出问题的部分
      description: 选一个最贴近的。选错不要紧，triage 时会改。
      options:
        - 接口用例（testcases/api/）
        - UI 用例（testcases/ui/）
        - 业务全链路用例（testcases/mes/）
        - 登录契约用例（testcases/login/）
        - 被测服务本身（mock_server/ 或 login_app/）
        - 框架/工具层（apis/、common/、conftest.py）
        - CI 流水线（.github/workflows/）
        - 工程化外壳（Docker / pre-commit / mypy / 依赖）
        - 不确定
    validations:
      required: true

  - type: textarea
    id: command
    attributes:
      label: 复现命令
      description: 请贴**原样的**命令，包含 -m 标记与任何环境变量。
      render: powershell
      placeholder: |
        # 例（本机跑基线时用的就是这个）：
        python -m pytest -o addopts= -q -m "not ui" -p no:warnings
    validations:
      required: true

  - type: textarea
    id: actual
    attributes:
      label: 实际输出
      description: |
        贴**原始输出**（可以截尾，但要保留失败摘要和那条 AssertionError 的完整正文）。
        如果只贴截图，请确保截图里有命令与退出码。
        ⚠️ 请顺手确认这不是"环境问题"：UI 用例在受限环境（临时目录不可写、
        无法建进程管道）会以 `PermissionError: [WinError 5]` 在 setup 阶段全失败，
        那不是用例的缺陷。
      render: text
    validations:
      required: true

  - type: textarea
    id: expected
    attributes:
      label: 你期望的结果，以及为什么
      description: |
        这一栏是重点。"期望通过"不算理由。
        如果涉及断言改动，请说明**为什么该改的是用例而不是实现**——
        本仓库有一条硬规则：改 `testcases/**` 的断言必须能说清这一点
        （见 CONTRIBUTING.md）。
    validations:
      required: true

  - type: dropdown
    id: stability
    attributes:
      label: 复现稳定性
      description: 这一栏决定了后面怎么排查——稳定复现可以二分定位，偶发只能靠日志。
      options:
        - 每次都能复现
        - 偶发（10 次里中几次）
        - 只在 CI 上出现，本地跑不出来
        - 只出现过一次，之后没再见过
    validations:
      required: true

  - type: textarea
    id: environment
    attributes:
      label: 环境
      description: 请把命令替换成实际值后粘贴。
      render: text
      value: |
        - 操作系统：
        - Python 版本（python -V）：
        - 是否使用 .venv：
        - 环境档案（TEST_ENV，未设置为 dev）：
        - 网络情况（接口用例打公网 jsonplaceholder，需要能出网）：
        - 相关环境变量（API_TIMEOUT / API_RETRIES / MAX_RESPONSE_MS / HTTP_PROXY）：
    validations:
      required: true

  - type: checkboxes
    id: checklist
    attributes:
      label: 提交前确认
      options:
        - label: 我已经在最新的 main 上复现过（不是几个月前的分支）
          required: true
        - label: 我贴的是原始输出，没有只写"跑不过"或只给截图
          required: true
        - label: 我已经确认这不是 docs/login-contract.md 里登记的已知缺陷
          required: true
        - label: 如果这是偶发失败，我**没有**直接重跑把它刷绿了事
          required: false
