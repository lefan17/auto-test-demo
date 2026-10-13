# 测试运行器镜像：被测服务（mock_server / login_app）与 pytest 共用这一个镜像。
#
# 为什么不写"每个服务一个 Dockerfile"：
# mock_server 与 login_app 的依赖**就是** requirements.txt 里那一份
# （FastAPI + uvicorn + pydantic），pytest 及其插件也在同一份文件里。
# 拆成三个 Dockerfile 意味着同一份依赖装三次、三处版本可能各自漂移，
# 而这三处必须**严格一致**才有意义（用例跑的必须是被测服务真正加载的那份代码）。
# 一个镜像 + compose 里换 command，是这类"同代码库多进程"的标准做法。
#
# 为什么用精确 COPY 而不是 `COPY . .`：
# 仓库根现在有了 .dockerignore（见那个文件），它挡住的是**构建上下文体积**；
# 但这两者解决的不是同一件事，所以这里仍然不写 `COPY . .`。
# 精确 COPY 管的是**镜像内容**：往仓库根新增一个顶层目录（比如 docs/、
# scripts/、一个临时实验目录）时，`COPY . .` 会静默把它一起塞进镜像，
# 而精确 COPY 不会——镜像里有什么，这份清单就是唯一的事实来源。
# 换句话说：.dockerignore 是黑名单，漏一条就等于漏一条；这份 COPY 列表是白名单，
# 漏一条会被立刻发现（镜像里少东西，用例直接红）。
# 上下文体积由 .dockerignore 负责，镜像内容由这份清单负责，各管一头。

FROM python:3.12-slim

# 国内构建时用 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 覆盖；默认仍是官方源，不把镜像源偏好写死给所有人。
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖清单再 install：改一行业务代码不会让依赖层缓存失效。
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && \
    python -m pip install -i "${PIP_INDEX_URL}" -r requirements.txt

COPY apis/ ./apis/
COPY common/ ./common/
COPY data/ ./data/
COPY login_app/ ./login_app/
COPY mock_server/ ./mock_server/
COPY testcases/ ./testcases/
COPY tools/ ./tools/
COPY ui/ ./ui/
COPY conftest.py pytest.ini ruff.toml ./

# UI 用例需要浏览器，chromium 不在基础镜像里（约 140MB，且要装一堆系统依赖）。
# 需要跑 UI 时在 compose 的测试服务里执行：
#   playwright install --with-deps chromium
# 默认命令只跑不依赖浏览器的部分，保证开箱即用。
CMD ["python", "-m", "pytest", "-m", "not ui"]
