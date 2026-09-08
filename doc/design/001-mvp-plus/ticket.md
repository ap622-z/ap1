# MVP+ Tickets

由 `doc/design/001-mvp-plus/spec.md`（验收清单 A01–A22）与 `plan.md` 拆分。文件按依赖序编号，编号小者先做；实现按序推进，保证任意时刻主干可跑、每张票验收独立成立。领域语义以根 `CONTEXT.md` 为准。

## T-01 — 后端包更名：`app` → `backend`

**What to build**：全仓以 `backend` 作为唯一后端包名运行——FastAPI 进程、端到端测试、CLI 摄入、容器启动全部指向 `backend`；本次只更名，不改任何行为。

**Blocked by**：无，可立即开始。

**Status**：ready-for-agent

- [ ] 后端以 `backend.main` 启动（容器与本地 uvicorn），`/api/health` 返回 200。
- [ ] 既有端到端测试全套通过（对外行为不变）。
- [ ] 全仓无旧包名 `app.*` 残留引用（代码 / 测试 / 容器命令 / CLI 帮助）。
- [ ] 项目打包元数据可解析（`pyproject` 无指向已删文件的悬空引用）。

## T-02 — 后端组件化落地

**What to build**：`backend` 内按 `doc/constitution/directory-architecture.md` 组织子包（config / api / worker / agent / repository / cli + 根级共享），能力层由单一模块拆成 `agent/tool`、`agent/skill`、`agent/mcp` 三子包并由能力层门面统一编排；统一配置目录正确读取 env / `.env`。纯结构变动，行为不变。

**Blocked by**：T-01。

**Status**：ready-for-agent

- [ ] 目录结构与 directory-architecture 一致（到目录层）。
- [ ] 统一配置入口在任意工作目录都能定位到项目 `.env`。
- [ ] 全量端到端测试通过（对外行为不变）。
- [ ] 无旧布局残留（单文件 config 等）；CLI 摄入以新包名可运行。

## T-03 — 启动正确性：校验 / 中止 / 自愈

**What to build**：真实运行形态（非 test、非显式降级）启动时，缺关键密钥即阻止启动并给出可读错误；空库首启灌入失败即中止而非带病继续；DB 有知识但向量集合空时启动自动补灌；显式降级仍可启动做本地调试。开发测试形态不受影响。

**Blocked by**：T-02。

**Status**：ready-for-agent

- [ ] 移除嵌入密钥 → 启动被阻止，错误指出缺失项；补回后正常启动。
- [ ] 空库 + 灌入失败（断网 / 错 key）→ 启动中止，日志含指引；无"空知识假健康"。
- [ ] 知识行在但向量集合空 → 启动自动补灌，补灌后检索可用。
- [ ] `SEED_ON_STARTUP=false` / `EMBED_PROVIDER=local` 显式降级可启动。
- [ ] `ENV=test` 下既有测试不受启动校验影响。

## T-04 — 运行期降级告警

**What to build**：模型 / 联网在请求路径失败时仍体面兜底、不中断会话，但日志以 ERROR 级 + scope 明确告警，并让用户能感知失败原因。

**Blocked by**：T-02。

**Status**：ready-for-agent

- [ ] 令某外部能力请求失败 → 会话不中断，用户收到一条体面回复。
- [ ] 同一次失败在日志中为 ERROR 级，携带 scope 字段与失败组件标识。

## T-05 — 数据持久化与端口收敛

**What to build**：`docker compose` 让三种数据库以命名卷运行并跨重启持久；数据库宿主端口仅本机回环可达；后端端口不进宿主；各服务带健康检查。

**Blocked by**：T-02。

**Status**：ready-for-agent

- [ ] `down` 再 `up` 后三库数据保留（用户 / 会话 / 知识）。
- [ ] 删除容器与卷后单命令 `up` 自动完成建表 / 建集合 / 灌知识，且可检索。
- [ ] 幂等重启：无重复建表、无重复灌入。
- [ ] mysql / qdrant / redis 宿主端口仅 `127.0.0.1` 可达；后端不发布宿主端口。
- [ ] 重启后旧用户可登录、历史仍在、知识问答仍命中。

## T-06 — nginx 前门

**What to build**：前端产物打进 nginx 镜像，nginx 作为唯一对外入口提供静态文件 + SPA 回退 + `/api` 反代；后端不再托管静态文件；compose 单命令仍可起整套。

**Blocked by**：T-02、T-05。

**Status**：ready-for-agent

- [ ] 经 nginx 打开页面正常；刷新 / 直达子路由不 404。
- [ ] 页面内 `/api` 请求全部经 nginx 成功（同源、无路径丢失）。
- [ ] 对外仅 nginx 一个发布端口。
- [ ] 直连后端不再返回前端页面（属预期）。

## T-07 — 真实 provider 冒烟与适配修复

**What to build**：以真实密钥验证三家外部提供方适配层（chat 函数调用、嵌入探测 / 批量 / 截断、搜索响应解析），跑通一次真实 agent 主链；发现真实协议差异即修复并复测。冒烟属开发期自测，不入库、不提交密钥。

**Blocked by**：T-02。

**Status**：ready-for-agent

- [ ] chat 真实输出可解析并正常收尾（含工具调用）。
- [ ] embed 真实维度 / 批量 / 截断正常，与向量集合维度一致。
- [ ] search 真实响应解析正常并正确回填。
- [ ] 真实密钥下完整消息主链通过（覆盖检索 / 反问 / 联网 / 压缩抽查）。

## T-08 — 全量回归与交付自测

**What to build**：提交给验收人前，按 spec 验收清单 A01–A22 逐项自测并留存证据；汇总真实运行中发现的缺陷并闭环修复。

**Blocked by**：T-01 — T-07。

**Status**：ready-for-agent

- [ ] 既有端到端测试全绿。
- [ ] A01–A22 每项标注自测结论（通过 / 依据），全部通过才交付。
