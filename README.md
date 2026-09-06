# AP1 — 偶像 Agent 情感陪伴系统

面向粉丝的“偶像 Agent”：**单例零用户状态 Agent** + **多用户数据隔离**。技术需求见
`doc/design/000-mvp/spec-mvp.md`，技术方案见 `doc/design/000-mvp/plan-mvp.md`，
开发票据见 `doc/design/000-mvp/ticket-mvp.md`。领域词汇（Scope / Repository / Worker /
Mailbox / Outbox）以 `CONTEXT.md` 为准。

## 技术栈

- 后端：Python 3.12 + FastAPI（单进程内 web + worker + agent 单例），uv 管理依赖
- 存储：MySQL（业务事实源）+ qdrant（偶像知识/歌词向量）+ redis（MVP 仅拉起、无业务消费者）
- 模型：DeepSeek API（`ds-v4-flash`），自定义 agent 循环（tool/skill/mcp 三能力）
- 前端：Vue 3 + Vite（微信式单会话网页，FastAPI 托管构建产物）
- 部署：全部中间件与最终打包走 Docker（`docker compose`）

## 本地开发

```bash
# 1) 起中间件（MySQL 映射宿主 3307，避免与本机已有 3306 冲突）
docker compose up -d mysql qdrant redis

# 2) 装后端依赖
uv sync

# 3) 配置环境（复制模板；LLM/搜索密钥按需填，不填时对联网/检索失败体面兜底）
cp .env.example .env   # 或用环境变量注入

# 4) 灌入偶像知识素材（幂等，可重跑）
uv run python -m app.cli.ingest

# 5) 起后端
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 6) （可选）起前端 dev（已 build 则由后端直接托管 /）
cd frontend && npm install && npm run dev
```

访问：后端 API 文档 `/api/docs`；网页对话 `/`。

## 全容器运行

```bash
# 依赖中间件
docker compose up -d mysql qdrant redis
# 先摄入知识（app 镜像构建好之后，进容器或宿主执行均可）
uv run python -m app.cli.ingest
# 构建并起 app（web+worker+agent 一进程）
docker compose up --build -d app
```

## 测试

E2E 测试以 **spec / plan 为准**（外部行为、用户可见入口），模型与联网搜索走本地协议桩，
无需外部密钥即可跑通完整主链路：

```bash
# 前置：docker compose up -d mysql qdrant redis
uv run pytest -q
```

覆盖：身份/唯一会话、整包回复、同会话并发恰一次、幂等重发、跨用户隔离、偶像检索
tool、含糊澄清 skill、联网搜索 mcp、上下文压缩持久化、失败重发不丢不重、run/step 日志。

## 备注

- 单偶像定位：全链路无偶像维度、无多偶像演进（见 spec Implementation Decisions）。
- 生产接入真实能力：配置 `LLM_API_KEY` / `SEARCH_API_KEY`；向量化如需语义检索，
  把 `EMBED_PROVIDER=openai_compatible` 并配置 `EMBED_BASE_URL/EMBED_MODEL/EMBED_API_KEY`。
- 应用级 env（dev/prod）通过独立数据库实例隔离，不落表列。
