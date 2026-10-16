# 请按下面五栏填。留空的栏会在 review 时被打回——
# 尤其是「怎么验证的」那一栏：写"应该没问题"不算验证。

## 改了什么，为什么

<!-- 一两句话。如果是修 bug，请写清"原来的行为是什么、为什么那是错的"。 -->

## 关联的 issue

<!-- 例如 Closes #12。没有关联 issue 的也可以，但请在上面把动机写清楚。 -->

## 怎么验证的

<!--
这一栏是重点，请贴**你实际跑过的命令与输出**（可以截尾，但要有结论行与退出码）。
不要只写"本地跑过了"。可用的几条：

  python -m pytest -o addopts= -q -m "not ui" -p no:warnings   # 期望 460 passed, 34 deselected, 11 xfailed
  python -m pytest -o addopts= -q -m mes                       # 期望 64 passed
  python -m pytest -o addopts= -q -m login                     # 期望 29 passed
  python -m pytest -q -m platform                              # 期望 105 passed
  cd traffic-replay && python -m pytest -q                     # 期望 173 passed（自带 pytest.ini）
  python -m ruff check .            && python -m ruff format --check .
  python -m mypy
  docker compose -f docker-compose.yml config --quiet          # 只做静态校验
-->

## 检查清单

- [ ] 我**没有**为了让某项指标变绿而修改 `testcases/**` 的断言或删除测试
- [ ] 我**没有**修改 `mock_server/**` 或 `login_app/**` 的业务逻辑

<!--
上一行不是客套。这两个目录是**被测对象**，改它们等于改"标准答案"。
若确实需要改，请把上面那个勾去掉并在下面说明，同时评估受影响的用例。
-->

- [ ] 涉及 `testcases/**` 断言的改动，我在下面说明了**为什么改的是用例而不是实现**
- [ ] 新增/修改的文件已加入 `CODEOWNERS`（如属敏感路径）
- [ ] 如果新增了依赖，已同步 `requirements.txt` 并重新生成 `requirements.lock`
- [ ] CI 全绿（或已在下面说明哪些步骤是预期失败的）

## 涉及断言改动时，为什么是用例的问题

<!--
删掉本节或留空 = 没有改动 testcases/** 的断言。

背景：本仓库的 `-m login`（29 条）与 `-m mes`（64 条）里有一部分断言
**刻意钉住了被测服务的已知缺陷**（见 docs/login-contract.md 的差异表）。
所以在改动之前，请先回答：你看的是哪张表、结论是什么？
-->

## 未验证的部分

<!--
**诚实地列出你没验证的东西**，比假装都验证了有价值得多。
常见的几类，可以直接勾：

- [ ] Docker 镜像**未构建验证**（本机没有可用的 daemon，只做了 `docker compose config` 静态校验）
- [ ] pre-commit hooks **未在本地执行**（安装需要 ensurepip，受限环境会拒绝）
- [ ] `pip-audit` **未在本地执行**（同上），只在 CI 上跑
- [ ] UI 用例**未在本机跑通**（受限环境无法建进程管道，setup 阶段即失败）
- [ ] mypy 未做到全仓零错误，已知技术债登记在 `pyproject.toml` 的 overrides 里
-->
