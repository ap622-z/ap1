# syntax=docker/dockerfile:1
# 多阶段：先构建 Vue 前端，再组装 Python 运行时（web + worker + agent 单例同驻一进程）。

# ---- stage 1: 前端 ----
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ .
RUN npm run build

# ---- stage 2: 后端运行时 ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# uv 为项目依赖管理工具：在镜像内仅用于安装已解析依赖
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY app ./app
COPY --from=frontend /fe/dist ./frontend/dist

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
