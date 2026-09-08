# Plan — MVP+ 技术方案（补齐交付缺口）

由 `doc/design/001-mvp-plus/spec.md` 导出的具体技术方案。领域语义以根 `CONTEXT.md` 为准；表结构以 `doc/constitution/database-schema.md` 为准。本文描述**方案与模块**，不含代码。需求判定以 spec 的 `## Acceptance Checklist`（A01–A22）为准，文末附 §↔A 对照。

本版不改产品能力、不改表结构、不引入数据迁移。改动集中在四块：**结构重构（backend）**、**运行底座（卷 / 幂等 / 启动校验 / 自愈 / 降级日志）**、**nginx 前门**、**真实运行验证**。

---

## 1. 结构重构：`app/` → `backend/` + 组件子包

> 目录分层标准以新增的 `doc/constitution/directory-architecture.md` 为唯一事实源（只记录到目录层）；本文件 §1 是其落到文件/引用层面的实施展开。

### 1.1 目标树

依据模块依赖事实（config 为全叶子；api/deps 与 agent/worker 只经 Runtime 交汇；errors/scope/records 为跨层共享；repository 为被 agent/worker/capabilities/cli 共用的枢纽），目标布局：

```
backend/
  __init__.py
  main.py              # FastAPI 工厂 / lifespan（原 app/main.py）
  context.py           # 组合根 Runtime / build_runtime（原 app/context.py，放根避免 agent↔worker 环）
  errors.py            # AppError 家族（原 app/errors.py，api/worker/capabilities/repository 五处共用→放根）
  logging_setup.py     # 结构化日志（横切，放根）
  config/              # 全局统一配置（读 env/.env）
    __init__.py        # 再导出 Settings / get_settings
    settings.py        # pydantic-settings（原 app/config.py）
  api/                 # 接入层：HTTP + 鉴权 + scope 构建（唯一对外边界）
    __init__.py
    routes.py          # register/login/send/history/health（原 app/api.py 单模块合并）
    deps.py            # get_runtime / require_scope（原 app/deps.py）
    security.py        # generate_token/hash_token/generate_nickname（原 app/security.py）
  worker/              # 编排层
    __init__.py
    worker.py
    mailbox.py
  agent/               # 执行层：零状态 agent + 能力层
    __init__.py
    agent.py           # 主循环（读库组装上下文 / 压缩 / 生成）
    capabilities.py    # CapabilityRunner：tool/skill/mcp 的注册与执行编排（能力层门面）
    providers.py       # LLM / 联网搜索 provider 协议与实现（原 app/providers.py）
    embeddings.py      # 嵌入提供方（原 app/embeddings.py）
    tool/              # 可调用工具：偶像信息 / 歌词检索（原 capabilities 的 tool 部分）
      __init__.py
    skill/             # 行为型 skill：含糊表述 → 巧妙反问指导（原 capabilities 的 skill 部分）
      __init__.py
    mcp/               # 外部能力接入：联网搜索（原 capabilities 的 mcp 部分）
      __init__.py
  repository/          # 数据层：唯一持久状态入口（强 scope）
    __init__.py
    repository.py      # 门面（worker 面 + agent 面）
    db.py              # 引擎 / migrate（原 app/db.py）
    schema.sql         # 与 db.py 同目录，保相对路径
    vector_store.py    # qdrant（原 app/vector_store.py）
    records.py         # 行对象（原 app/records.py）
    scope.py           # Scope（原 app/scope.py）
    hashing.py         # sha256_hex（原 app/hashing.py，仅 repository 用）
    tokens.py          # estimate_tokens（原 app/tokens.py，仅 repository 用）
  cli/
    __init__.py
    ingest.py          # 入口改为 `uv run python -m backend.cli.ingest`
```

相对批准过的初版树的**两处修正**及理由：
- `errors.py` 放根而非 api：worker / repository / capabilities 都 import AppError 家族，放 api 会造成下层反向依赖数据层/编排层。
- `context.py` 放根而非 agent：真实职责是**组合根**（imports worker + agent + repository…），放 agent 会与 worker 构成 import 环。

### 1.2 旧 → 新映射（完整 24 项）

config→config/settings、main→main、api→api/routes、deps→api/deps、security→api/security、worker→worker/worker、mailbox→worker/mailbox、agent→agent/agent、capabilities→agent/capabilities（仅保留 CapabilityRunner 门面；tool/skill/mcp 三部分分别拆入 agent/tool、agent/skill、agent/mcp 三个子包）、providers→agent/providers、embeddings→agent/embeddings、repository→repository/repository、db→repository/db、schema.sql→repository/schema.sql、vector_store→repository/vector_store、records/scope/hashing/tokens→repository/*、context→context、errors/logging_setup→根、cli/ingest→cli/ingest、`__init__` 版本号保留。

### 1.3 引用同步清单（须全仓一次改净）

- 包内 `from app.*` → `from backend.*`（Explore 已逐文件列出约 80 处 import 边，全部机械替换）。
- `Dockerfile`：`COPY app ./app` → `COPY backend ./backend`；`CMD … app.main:app` → `backend.main:app`。
- `docker-compose.yml`：无 `app.` 模块引用，仅服务名/容器名（ap1-app 等），**不受包名影响**。
- `tests/conftest.py` / `tests/test_e2e.py`：`app.config` / `app.db` / `app.embeddings` / `app.repository` / `app.vector_store` 与 uvicorn 目标 `app.main:app` 全部替换。
- `backend/config/settings.py` 的 `.env` 定位：原 `app/config.py` 用 `parents[1]/.env`；新位置深一级，须改 `parents[2]/.env`，并抽成显式常量（防再挪目录静默失效）。
- `cli/ingest.py` 的素材目录 `parents[2]/doc/asset`：`backend/cli/ingest.py` 仍深两级，无需改；入口 docstring 的 `python -m app.cli.ingest` 同步。
- `schema.sql` 随 `db.py` 同目录移动，保持 `parent/schema.sql` 计算不变。
- `.dockerignore` / `.gitignore` 不含 `app/` 路径，但须确认改名后 `backend` 不被任何 ignore 规则吞掉。
- 文档（CLAUDE.md / CONTEXT.md / doc/**）经查无 `app.` 模块引用，无需动。
- **顺带修一处隐患**：`pyproject.toml` 的 `readme = "README.md"` 指向已删除文件，会在重建/元数据校验时报错——本版直接移除该行（纯元数据，无行为影响）。

### 1.4 重构约束

纯搬移 + import 改写；**不重命名任何公共符号、不改任何类/函数/路由路径/数据结构**。重构以"既有 E2E 全套通过"为唯一回归判据（对应 A11/A12）。

---

## 2. 运行底座

### 2.1 compose：命名卷 + 端口策略 + 健康检查

`docker-compose.yml` 改造（服务名/容器名沿用，测试依赖 ap1-mysql）：

- 顶层新增命名卷：`ap1_mysql_data` / `ap1_qdrant_data` / `ap1_redis_data`。
- mysql：挂 `ap1_mysql_data:/var/lib/mysql`；宿主端口改绑 `127.0.0.1:3307:3306`；健康检查与字符集命令不变。
- qdrant：挂 `ap1_qdrant_data:/qdrant/storage`；宿主端口改绑 `127.0.0.1:6333:6333`。
- redis：挂 `ap1_redis_data:/data`；宿主端口改绑 `127.0.0.1:6379:6379`。
- app：不再发布宿主端口（仅容器网络内 8000）；新增镜像内健康检查（`/api/health`）；`depends_on` 三库 `service_healthy` 不变；`SEED_ON_STARTUP=true` 与密钥注入方式不变。
- 新增 nginx 服务：见 §3。

**端口策略与 spec A16 的关系**：mysql/qdrant/redis 仅绑 `127.0.0.1`——宿主机本机工具与现有 E2E（conftest 直连 127.0.0.1:3307/6333/6379 + `docker exec ap1-mysql`）可照常跑，同时不对局域网/公网开放；**对外唯一发布端口是 nginx**。app 无宿主端口，杜绝绕过前门直连。

**空卷冷启动 / 幂等**（对 A01–A05）：
- 冷启动事件 = `docker compose down -v` 后首次 `up`：mysql 空数据目录由镜像初始化建库建账号 → app 首启执行 migrate（空库建 6 表）→ 建 qdrant 集合 → 知识空 → 自动灌入。
- 日常 `down`（不带 `-v`）/`up`/restart：卷保留 → migrate 见表跳过、知识非空不重灌、qdrant 点在，app 即起即用，无重复副作用。
- 幂等由「`CREATE TABLE IF NOT EXISTS` + 列宽幂等对齐」保证，本版**不引入**迁移账本（spec 决策）。

### 2.2 启动配置校验（对 A06/A09）

在 lifespan 最前（migrate 之前）、且 `ENV ∈ {dev, prod}`（即非 test）时执行一次**显式配置校验**：
- 收集缺失项：远程嵌入必需 `EMBED_API_KEY/EMBED_MODEL/EMBED_BASE_URL`；LLM 必需 `LLM_API_KEY`；搜索必需 `SEARCH_API_KEY`。
- 任一缺失 → **阻止启动**，输出一段可读错误：逐条列出缺什么、该填到哪个变量、以及"想降级就把 `EMBED_PROVIDER=local` / `SEED_ON_STARTUP=false`"等提示；进程以非零退出。
- `ENV=test`（开发测试桩形态）跳过校验，保证现有 E2E 不依赖真实密钥。

### 2.3 空库首启摄入失败 = 中止（对 A07）

去掉现状「seed 失败 → continue_booting」的吞错路径：空库 + `seed_on_startup` 时摄入为**必需步骤**，异常须记 ERROR 日志（含明确指引：哪一步、什么原因、怎么修）后**向上抛出**，使启动中止、容器以失败态退出，杜绝"空知识假健康"。显式 `SEED_ON_STARTUP=false` 时跳过（A08 降级路径）。

### 2.4 qdrant 空集合自愈（对 A05 兜底）

启动知识就绪流程改为统一判定（消除"只删了 qdrant 卷 → DB 有行但集合空 → 静默坏 RAG"）：
1. DB 知识为空（`idol_infos` 计数 0）且 `seed_on_startup` → 走灌入（同现状）；
2. DB 知识非空但 qdrant 集合点数为 0 → **自动触发对账补灌**（复用摄入的幂等 reconcile，重写向量）；补灌失败 → 同 §2.3 中止；
3. 两者都非空 → 仅对齐集合维度，不重灌。

（点 2 属异常运维态，正常带卷重启永不触发；补灌走批处理，冷启动慢仅发生一次。）

### 2.5 运行期降级告警（对 A10）

LLM / 搜索在请求路径上的失败沿用 000"体面兜底、不中断会话"；但 worker 捕获 provider 失败时必须以 **ERROR 级 + scope（env/user/session/run/step）** 结构化日志落一条"能力不可用"告警，并把失败原因回显给用户回复（体面文案）。绝不无声降级。嵌入不在请求路径（仅灌入/自愈时用），其失败已由启动语义覆盖。

---

## 3. nginx 前门

### 3.1 镜像拓扑（单 Dockerfile 多 target）

- 保留 `node:20-alpine` 前端构建 stage（产出 dist）。
- **backend target**（app 服务，`build.target: backend`）：python 运行时 + `uv sync` + `COPY backend` + `COPY doc/asset`；不再含 dist、不再依赖前端 stage；CMD 指向 `backend.main:app`。
- **web target**（nginx 服务，`build.target: web`）：`FROM nginx:alpine`，`COPY --from=frontend /fe/dist` 到站点根，配 nginx.conf；EXPOSE 80。
- compose 两个服务指向同一 Dockerfile 的不同 target → `docker compose up --build` 单命令不变，前端只构建一次（只属于 web target）。

### 3.2 路由行为（nginx.conf 要点，非代码）

- `location = /` 与静态资源 → 站点根 dist；`try_files $uri … /index.html` 做 SPA 回退（刷新/直达子路由不 404，对 A14）。
- `location /api/` → `proxy_pass` 到 `http://app:8000`，保留路径与原请求头（Host/X-Forwarded-*）；`/api/docs`、OpenAPI 随此前缀自然可达（对 A15）。
- `location /assets/` 静态缓存头（可后补）。
- 本版监听 http；server 块留 https 注释样板，不启用。

### 3.3 后端静态托管移除（对 A17）

`backend/main.py` 删除 dist 挂载、`GET /` index 与 `/{path:path}` SPA fallback 三处（含目录穿越守卫一并移除——该职责移交 nginx）。后端退化为纯 API：`/api/*` 与 `/api/docs`。`_FRONTEND_DIST` 路径计算随之删除（顺带消掉一处对包深度的脆弱依赖）。

### 3.4 编排与端口

- nginx 服务：`ports: "${WEB_PORT:-8080}:80"`；`depends_on: app: condition: service_healthy`；自身镜像内健康检查。
- 说明：宿主取 8080 为默认以免与既有 80 冲突，`WEB_PORT` 可注入切换（A16"对外仅 nginx 一个发布端口"）。
- app 服务补充镜像内健康检查（§2.1），nginx 等它 healthy 再放流量。

---

## 4. 真实运行可用性（对 A18–A22）

### 4.1 provider 适配的真实协议风险点（须真实冒烟确认并修）

- **chat（DeepSeek）**：桩从未验证真实输出的 `tool_calls` 结构、多 tool call、与"文本回复直接收尾"分支的解析——函数调用字段形状若与桩不一致，会在此断。为首要验证点。
- **embed（硅基流动 BGE）**：维度探测（`[probe]` 首请求取 dim）、≤128 批量、超长截断（`EMBED_MAX_INPUT_CHARS` 按字符非 token）三条真实路径；确认与 qdrant 集合维度一致。
- **search（Tavily）**：真实响应解析（answer/results 结构）与工具回填格式一致。

### 4.2 真实冒烟（开发期自测，不入库）

以一次性本地脚本/手动方式（`.env` 真 key，`tmp/` 下，不提交、不进常驻套件）依次验证：三家 provider 单点协议 → 一次真实 agent 主链（发消息含 tool call）→ 一次真实检索问答 → 一次真实联网 → 一次压缩后连贯。每项通过才把对应 A 项判绿；发现适配缺陷即修回代码并复跑。结果映射到 A18–A22。

---

## 5. 验收对照（§ ↔ A 清单）

| Plan § | 交付项 | spec A |
|---|---|---|
| §2.1 | 命名卷 / 冷启动 / 幂等重启 / 显式清空 | A01–A04 |
| §2.3–2.4 | 知识持久一致（无空库假健康） | A05 |
| §2.2 | 缺 key 阻止启动 + 可读错误 | A06 |
| §2.3 | 首启摄入失败中止 | A07 |
| §2.2/2.3 | 显式降级（local 嵌入 / 关自动灌入）可启动 | A08 |
| §2.2 | 密钥隔离（.env 不入库、example 占位） | A09 |
| §2.5 | 运行期降级 ERROR 告警 + 会话不断 | A10 |
| §1 | 重构回归 / 无旧引用 / 统一配置 | A11–A13 |
| §3 | nginx 动静分离 / /api 反代 / 单一入口 / 后端不托管静态 | A14–A17 |
| §4 | 真实 provider 冒烟 → 真实对话/检索/反问/联网/压缩 | A18–A22 |

## Further Notes / 遗留

- 演进接缝沿用 000：本版维持单进程（web+worker+agent 同驻），nginx 仅接入层静态/反代，不改进程模型。
- 前端产物为构建时快照（nginx 镜像内）；vite 热更新/直连后端调试等开发体验不进本版（若需，后续以 compose override 追加，不在 MVP+）。
- 未决小项（实现时定，不影响本方案）：`WEB_PORT` 是否写入 `.env.example`；nginx 健康检查命令具体形态；app 健康检查的 python 探测写法。
