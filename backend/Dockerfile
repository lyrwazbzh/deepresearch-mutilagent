# 多阶段构建（tickets.md T30）：builder 装依赖 → runtime 仅拷贝虚拟环境与代码
# 构建上下文为 backend/（compose build.context=./backend）

# ---------- builder：安装依赖到独立 venv ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 与仓库既有 backend/app/Dockerfile 保持同一镜像源约定
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime：仅含虚拟环境与代码 ----------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# 注意：migrations/ 与 app/config/ 等资源目录必须随代码进入镜像（票面风险条），
# 构建后需实际 curl /hello 验证，不能只看 build 成功
COPY app/ ./
COPY migrations/ ./migrations/

EXPOSE 8000

CMD ["uvicorn", "app_main:app", "--host", "0.0.0.0", "--port", "8000"]
