# Plan — MVP 技术方案

由 `doc/design/000-mvp/spec-mvp.md` 导出的具体技术方案。领域语义以根 `CONTEXT.md` 为准；表结构以 `doc/constitution/database-schema.md` 为准。本文件描述**方案与模块**，不含代码。所有标「演进接缝」处是未来目标架构（MQ/多进程/长期记忆）的替换点，MVP 只留接口不实现。本系统为**唯一偶像**设计，无偶像维度。

---

## 1. 目标与边界

**MVP 目标**：单进程内同时承载 web 入口、worker、零用户状态 agent 单例，验证一条完整主链路——多用户并发下的数据隔离、单会话串行、幂等、会话窗口压缩、tool/skill/mcp 三能力、偶像知识 RAG 检索。

**唯一偶像定位**：系统为一位偶像设计，全链路无偶像维度、无多偶像演进。

**明确不做**（spec Out of Scope）：RabbitMQ/outbox 异步投递、多 worker 进程、进程间 RPC、Nginx 分流、gateway、长期记忆、撤回、SSE/WebSocket 流式、metrics 中心与完整 errors 分类治理、App/微信接入、运营端知识管理 UI、偶像与歌词的精细模型（资产先按现有表入库一份）。

**技术选型**：语言 Python；Web 框架 FastAPI；模型 DeepSeek API `ds-v4-flash`；存储 MySQL（事实源）+ qdrant（偶像知识/歌词向量）+ redis（缓存/限流）；偶像知识摄入为种子/脚本级。

---

## 2. 总体分层

```
┌ 接入层  HTTP + 鉴权 + scope 构建（唯一对外边界）
└ 编排层  worker：消息生命周期 / 会话锁 / 把最新用户消息交给 agent
   └ 执行层 agent（全局单例、无特定用户状态）：读库组装上下文 / 压缩 / 自定义循环
      └ 能力层 tool（偶像检索）/ skill（提问引导）/ mcp（联网搜索）
└ 数据层  repository：唯一数据访问门面 → MySQL / qdrant / redis
```

- **接入层**只做协议与身份：解析请求 → 校验令牌 → 解析出用户与会话 → 构造 Scope → 交给编排层。不碰业务。
- **编排层（worker）**处理消息生命周期与后端事务：接最新用户消息 → 取会话锁 → 调 `agent.run`。**它只把"scope + 最新一条用户消息"传给 agent**，不组装上下文、不干预 agent 内部。未来接 MQ 时 worker 变成消费者回调（演进接缝）。
- **执行层（agent）**负责思考所需的一切：**在内部经 repository 读历史与摘要、自己组装上下文窗口（含压缩）**，再跑自定义循环产出回复。"无用户状态"指**不硬编码任何特定用户**——每个用户长什么样由 repo 里的数据决定，agent 进程内不留跨 run 的用户态。
- **数据层（repository）**是所有持久状态的唯一入口，按 scope 强校验；向 worker 与 agent 分别提供方法面（见 §8）。

---

## 3. 进程与并发模型

- **单进程、单 worker**：FastAPI 一个进程，web 与 worker、agent 同驻；不引入多进程。
- **并发轴**：跨会话并行、同会话串行。
  - 跨会话：HTTP handler 为 async；阻塞调用（DeepSeek、qdrant、MySQL 同步驱动、工具）丢线程池/异步客户端，事件循环不阻塞 → 一个用户慢不影响他人。
  - 同会话：**mailbox 锁**（进程内、按 session_id 键控）保证同一会话任一时刻至多一条消息在处理、顺序为锁获取顺序；接口抽成可换分布式实现的形态（演进接缝，Redis 锁）。
- **run/step 只进日志**：一次回复 = 一次 run；run 内每轮模型+工具 = 一步 step。日志带完整 scope，跨组件可串联回放（见 §11）。
- 部署无 Nginx：进程直连；`env`（开发/生产）用独立数据库实例隔离，不落表列。

---

## 4. 鉴权与 Scope

- **注册**：生成唯一昵称 + 登录令牌；库里只存令牌 SHA-256，明文令牌仅在注册时返回一次。令牌为持有型凭据，请求以 Bearer 携带。
- **鉴权**：每请求解析令牌 → 哈希反查 users → 加载该用户唯一会话（首用惰性创建）→ 构造身份层 Scope `{env, user_id, session_id}`。
- **越界防线**：Scope 贯穿 worker 与 repository 的一切方法；repository 方法签名强制携带 scope，内部所有查询/写入以 scope 为条件，不存在无 scope 的裸访问。数据隔离由此在数据层兜底，而非依赖上层自觉。
- **演进接缝**：scope 的身份维度固定为用户/会话，无偶像维度（本系统单偶像，scope 不含 idol）。

---

## 5. 一次消息的完整时序（交付形态：同步整包 HTTP）

前端为单一会话窗口，无轮询通道设计；回复随同一次 HTTP 响应整包返回。`GET /messages` 仅用于刷新恢复历史与断线重取，不作为主交付通道。

```
POST /messages { text, client_message_id }
 ① 鉴权 → 构造 Scope
 ② repository：MySQL 事务写入 user 消息行（status=received）
    └ 幂等：若 (session_id, client_message_id) 已存在 → 命中已有结果，不重复执行
 ③ worker 获取本会话 mailbox 锁
 ④ worker 调 agent.run(scope, 最新用户消息) —— 只传这两样，不装配上下文
 ⑤ agent 内部：经 repository 读历史与摘要 → 组装上下文窗口（summary + 边界后全部轮次，
    总 token 超预算则把最近 10 轮之前的内容并入 summary，§7）→ 自定义循环（§6）→ 产出回复（与工具记录）
 ⑥ worker 经 repository：MySQL 事务写入 tool 记录 + agent 回复行（origin_message_id 关联）
    并置 user 消息 status=replied —— 与回复同批事务
 ⑦ 释放 mailbox 锁 → 整包回复作为 HTTP 响应返回

GET /messages?after=xxx → 会话历史（刷新恢复）
```

失败语义：②之后、⑥之前进程崩溃 → user 消息停留 received/processing，前端可凭同一 `client_message_id` 重发复用，不重复跑 agent。该状态列即未来 outbox 的 MVP 替身。

---

## 6. Agent 执行循环（自定义）

不依赖现成 agent 框架，自行实现主循环：

- **输入**：`scope + 最新用户消息`（worker 传入）。agent **不拿**装配好的上下文，而是在内部经 repository 读取该会话历史与摘要，自行组装窗口（§7）后进入循环。
- **每步**：调模型 → 解析输出，二选一：
  - 文本回复 → 结束 run；
  - 工具调用 → 经**能力层**执行 → 结果写回当前 run 上下文 → 进入下一步。
- **护栏**：单 run 最大步数上限、每步 token 预算上限，超限即强制收尾（体面兜底，不让对话中断）；单步工具失败不 abort run，交还给模型重新决策。
- **能力层三态**（对模型而言的形态不同）：
  - **tool**：可调用单元，直接执行动作。MVP 首个为**偶像信息检索**——由 repository 对 idol_infos / songs.intro / idol_lyrics 做向量召回 + 规范行校验（§9）。走模型 function calling。
  - **mcp**：外部能力来源协议。MVP 首个为**联网搜索**（Tavily）；在能力层登记为可调用工具，供模型调用，与本地 tool 同接口形态，便于日后挂更多 mcp server。
  - **skill**：**行为指导**而非可调用函数——一段注入人格/规则的指导文本。MVP 首个为**提问**：当用户表述含糊、且该信息可能影响后续记忆与判断时，指导 agent 先巧妙反问澄清再继续。触发判定 MVP 用轻量规则（信息缺失/含糊信号），命中即把指导注入当次 prompt。
- **step_id/run_id** 在此层生成并写入日志。

---

## 7. 上下文窗口与压缩

上下文窗口与压缩发生在 **agent 内部**（读库组装窗口、超预算时先压缩），不经 worker。

- **窗口定义**：`summary（压缩历史）+ summary_seq 边界之后的所有轮次`（每轮含其工具记录）。边界之后是新产生的对话，随交流自然累积（预算宽裕时可达数百轮），不进 summary。
- **token 计账**：每行消息写入时估算 `token_count`（写入方估算）；summary 存 `summary_tokens`。
- **压缩触发**：agent 组装窗口时，窗口总 token 超出预算（预算按数百轮规模设定，远大于 10 轮）→ 触发一次压缩。
- **压缩动作**：把 `summary_seq 边界之后、最近 10 轮之前`的全部轮次经 LLM 重写并入 summary → 更新 `sessions.summary_text / summary_tokens / summary_seq` → 压缩后窗口 = 新 summary + 最近 10 轮。
  - **最近 10 轮永不压缩**：它们是最新信息，始终以原文留在窗口；被压缩并入 summary 的轮次从窗口消失。
  - 物理上被压缩的旧消息行仍保留在库（历史完整性），只是不再进入上下文（由 summary_seq 界定）。
- **scope 约束**：摘要读取/写入经 repository、绑定会话，绝无跨会话泄漏。
- **演进接缝**：会话内 summary 是未来长期记忆的种子；摘要版本/审计需求出现时再拆表。

---

## 8. 数据访问与一致性（repository）

- repository 是唯一数据门面，对内分两组方法面，与 spec"分别提供方法"一致：
  - **worker 面**：写用户消息、消息生命周期状态、结束事务内统一落库工具记录与 agent 回复（与 status=replied 同批）。
  - **agent 面**：读该会话历史与摘要、上下文组装与压缩摘要写入、知识检索。agent 只经 repo 取上下文与检索，产物交回 worker 落库，不在 agent 侧独立写消息/回复。
- **用户链路（只进 MySQL）**：user/agent/tool 消息、会话、用户 —— 单库普通事务，同一会话的写（消息→回复/工具记录→置状态）在 MySQL 事务内原子提交。用户数据**永不写 qdrant**。
- **知识链路（MySQL + qdrant 双写）**：仅偶像知识/歌词/歌曲介绍摄入时发生。低代价协议：
  1. MySQL 规范行落库（同 id 幂等，`content_hash`）；
  2. 同 id 写 qdrant 向量；失败 → 置 `vector_synced=0` 并报错；
  3. 清扫任务按 `vector_synced=0` 幂等补写；
  4. 删除 = `status=0` + 删 qdrant 点。
- **RAG 读路径（查询校验）**：qdrant ANN 只返回候选 id → 回 MySQL 按 id 校验（行存在、hash 匹配、status=上架）→ 只回传校验通过内容。孤儿/过期向量对用户不可见。

---

## 9. 偶像知识摄入（MVP 种子级，非请求路径）

独立于对话主链路的**摄入流程**（脚本/种子任务，无 UI，对应 spec"运营端知识管理 UI 不做"）：

1. 解析 `doc/asset/idolinfo-清洗后.xlsx` → 逐行写 `idol_infos`（tag/content/event_time）+ 向量化。
2. 解析 `歌曲介绍.md` 与 `lhw歌词新.md` → 以 `song_title` 为唯一归并键归一：
   - 曲目级写 `songs`（元数据 + intro）；
   - 歌词正文按段切分写 `idol_lyrics`（song_id + seg_no）；
   - 素材两文件歌目/标题有出入，无歌词的歌保留歌曲行。
3. 每行/每段向量化进 qdrant（同 id），供"检索偶像信息 tool"使用。

偶像与歌词的**更精细模型设计**后置，MVP 以现有 6 表先落一份。

---

## 10. API 概念面（MVP）

- 身份：注册（返回昵称+一次性令牌）、登录（令牌校验）。无密码找回/多设备管理。
- 发消息：单接口，含幂等键；回复随响应返回。
- 读历史：分页/游标拉取会话历史（刷新恢复）。
- 错误面：统一结构化错误返回（错误类型 + scope 关联），不做完整 errors 分类治理体系；业务异常类型仅覆盖 MVP 必要分支（未授权、会话越界、幂等冲突、能力不可用、处理失败），其余收敛为通用错误并体面兜底。

---

## 11. 可观测性（MVP）

- **原则**：测试接缝是端到端（用户入口 → 回复就绪），组件不设内部测试接口；**trace/日志是组件唯一的"内部可见面"**。
- 每个组件执行中产出结构化日志，携带完整 scope（env/user_id/session_id/run_id/step_id）与事件名，使一条端到端请求可跨组件串联回放。
- MVP 不做指标采集中心；运行问题依赖日志定位 + 端到端测试兜底。

---

## 12. 部署与运行形态

- 单进程服务：web + worker + agent 单例同驻；**依赖与最终打包全部 Docker 化**。
- MVP 容器：MySQL、qdrant、redis（均 Docker 运行）。无 RabbitMQ、无 Nginx（目标架构再引入，见 constitution 技术选型）。
- 参数集中可配：窗口 token 预算、压缩豁免下限（MVP=最近 10 轮）、单 run 最大步数、模型名/端点/密钥。
- redis MVP 仅拉起、无业务消费者（演进接缝：令牌→会话解析短缓存、每用户限流计数留待后续）；不承担业务事实。

---

## 13. 一致性风险与演进接缝清单

| MVP 实现 | 目标架构演进 |
|---|---|
| 单进程（web+worker+agent） | 多进程 + 独立 agent + MQ + RPC |
| 消息 status 列兜底 | outbox 事件 + MQ 投递 |
| mailbox 进程内锁 | Redis 分布式锁 / 会话队列 |
| 会话内 summary | 长期记忆系统 |
| 同步整包 HTTP 回复 | 保留（产品形态即整包）；投递通道可换 |
| 知识摄入同 id 双写 + 清扫 | 同上协议保留 |

接受的风险：单进程是 SPOF、单点扩容受限、模型调用为慢路径；均为 MVP 有意取舍，演进接缝已列出。
