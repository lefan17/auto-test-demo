name: ✨ 功能请求
description: 新增用例、新增测试能力、改进工程化外壳
title: "[Feature] "
labels: ["enhancement", "triage"]
assignees: []
body:
  - type: markdown
    attributes:
      value: |
        ## 这个仓库对"新功能"的偏好，写在前面

        这是一个**测试框架仓库**，所以"功能请求"多半属于下面某一类。
        请先想清楚你要的是哪一类，因为它决定了验收标准：

        - **新增用例** —— 验收标准是"它能抓住一个真实的缺陷"。
          一条永远绿的用例不增加信心，只增加维护成本。
        - **新增测试能力**（比如把子进程纳入覆盖率、接入视觉回归）——
          验收标准是"它在本机与 CI 都能跑，且失败时给出可定位的证据"。
        - **改进工程化外壳**（CI / 容器 / 类型 / 依赖）——
          验收标准是"它拦得住它声称要拦的东西"，而不是"配置写好了"。

  - type: dropdown
    id: kind
    attributes:
      label: 请求类型
      options:
        - 新增测试用例
        - 新增测试能力 / 基础设施
        - 改进工程化外壳（CI / 容器 / 类型检查 / 依赖管理）
        - 改进文档
        - 其它
    validations:
      required: true

  - type: textarea
    id: problem
    attributes:
      label: 你要解决的问题是什么
      description: |
        请描述**当前的困难**，而不是直接给方案。
        "我想加一个 XXX 工具"不是问题描述；
        "现在 `mock_server/*` 的覆盖率永远是 0%，导致覆盖率门禁只能定在 40%，
        而这块恰恰是测的最狠的代码"才是。
    validations:
      required: true

  - type: textarea
    id: current
    attributes:
      label: 现在是怎么绕过去的
      description: |
        如果你已经在用某种土办法绕开它，请写下来——那通常是最有价值的输入，
        它精确地告诉我们"缺的那一块到底在哪"。
    validations:
      required: false

  - type: textarea
    id: proposal
    attributes:
      label: 你建议怎么做（可选）
      description: 如果有具体方案，写在这里；有取舍分析更好。
    validations:
      required: false

  - type: textarea
    id: acceptance
    attributes:
      label: 怎么算做完了
      description: |
        请给出**可执行的**验收标准。反例："CI 全绿"。
        正例："`python -m pytest -m ui` 在 CI 上 14 passed；
        `docker build` 在 daemon 可用的机器上退出码 0，
        且 `docker run --rm <image> python -c 'import fastapi'` 不报错。"
        ⚠️ 如果验收需要本机没有的条件（Docker daemon、能出网的环境），
        请在那一条上注明"仅 CI / 需特定环境可验证"。
    validations:
      required: true

  - type: checkboxes
    id: checklist
    attributes:
      label: 提交前确认
      options:
        - label: 我搜过现有 issue，没有重复
          required: true
        - label: 我给出的验收标准是可执行的命令，不是"应该能工作"
          required: true
        - label: 我理解这个仓库不接受"为了指标好看"的改动（例如为覆盖率数字删测试）
          required: true
