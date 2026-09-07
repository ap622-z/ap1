# syntax=docker/dockerfile:1
# 多 target：app = backend（纯 API），nginx = web（前端 dist + 反代）。
# 前端只在 web target 里构建一次；backend 不再携带 dist。

# ---- stage: 前端构建（dist 只供 web target 使用）----
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ .
RUN npm run build

# ---- target: backend（app 服务）----
FROM python:3.12-slim AS backend
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# uv 为项目依赖管理工具：在镜像内仅用于安装已解析依赖
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY backend ./backend
# 摄入素材打进镜像：空知识库启动时自动灌入（seed_on_startup）
COPY doc/asset ./doc/asset

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---- target: web（nginx 服务）----
FROM nginx:1.27-alpine AS web
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=frontend /fe/dist /usr/share/nginx/html
EXPOSE 80
