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
# 本仓库没有 .dockerignore（有意不改边界外的文件），`COPY . .` 会把
# .venv/（几百 MB）、.pytest_cache/、pytest-of-*/ 这些本机产物灌进构建上下文，
# 既拖慢构建，又可能把本机的临时数据库带进镜像。
# 只列真正要跑的东西，比事后补一份 .dockerignore 更不容易漏。

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
