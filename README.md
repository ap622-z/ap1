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

## 运行（全容器，一条命令）

**不需要在宿主装 uv / node / python**。镜像内已打包：后端 + Vue 前端构建产物 + 摄入素材；
容器启动时自动完成迁移，且知识库为空时自动摄入素材（幂等），`docker compose up` 即开即用。

```bash
# 一条命令：起全部（mysql + qdrant + redis + app，app 自迁移自摄入自托管前端）
docker compose up -d --build

# 打开
#   网页对话： http://localhost:8000/
#   API 文档： http://localhost:8000/api/docs
```

> 提示：默认 `EMBED_PROVIDER=local`（离线确定性向量，无需任何密钥即可跑通全流程）。
> 若要语义检索，配硅基流动 BGE 后重启 app 容器即可（见下「接入真实能力」），
> 首次重启会在空库时用真实模型重灌向量。

停止：`docker compose down`；清库重灌：`docker compose down -v && docker compose up -d --build`。

## 本地开发 / 测试（可选，需宿主装 uv / node）

```bash
docker compose up -d mysql qdrant redis     # 只起中间件
cp .env.example .env
uv run python -m app.cli.ingest             # 幂等摄入（容器也会自动做，这里供手动触发/更新）
uv run uvicorn app.main:app --port 8000     # 宿主起后端
uv run pytest -q                            # E2E
cd frontend && npm install && npm run dev   # 前端热更新（生产由后端托管 / 即可）
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
- 生产接入真实能力（经环境变量注入，不进文档/仓库）：
  - 对话模型：`LLM_API_KEY`（DeepSeek）
  - 联网搜索：`SEARCH_API_KEY`（Tavily）
  - 语义向量（硅基流动 SiliconFlow BGE）：
    ```
    EMBED_PROVIDER=openai_compatible
    EMBED_BASE_URL=https://api.siliconflow.cn/v1
    EMBED_MODEL=BAAI/bge-m3        # 或 BAAI/bge-large-zh-v1.5
    EMBED_API_KEY=sk-...
    ```
    适配说明：向量维度由 embedder 首个请求自动探测（BGE 系列为 1024 维），
    与 qdrant 集合自动对齐，无需手工指定；请求按 OpenAI 兼容合同（Bearer / encoding_format=float）批量提交。
  - 未配密钥时：对话/联网对不可用能力体面兜底；向量退化为本地离线 n-gram（`EMBED_PROVIDER=local`），
    检索仅按字面重叠召回，语义检索需切到上述真实模型。
- 应用级 env（dev/prod）通过独立数据库实例隔离，不落表列。
