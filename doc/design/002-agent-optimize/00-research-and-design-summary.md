# 002 Agent 优化 — 调研与设计总览

> 对五个 agent 子系统（会话压缩 / 提示词结构与组装 / loop 循环 / log 观测 / tool 管理）做一次**基于真实开源源码**的调研，并把结论落到本 idol-agent 项目。分册：`01`–`05`。
> 每册结构固定：① 四家样本的真实实现（带引用）→ ② 共性取向 → ③ 本项目现状与差距 → ④ 设计建议。
> 领域语义以根 `CONTEXT.md` 为准；表结构以 `doc/constitution/database-schema.md` 为准。

## 0. 调研样本与方法（可复现）

四家开源样本均为 **clone 到本地后逐行读源码**所得，非二手资料；引用统一为 `仓库名@commit 相对路径:行号`。

| 仓库 | 身份 | commit | 语言 | 与本项目的相关性 |
|---|---|---|---|---|
| pi | earendil-works/pi（MIT agent toolkit：agent-core/ai/telemetry/session） | `3e4bc26` | TS | 现代 agent harness 参考主样本 |
| dsh | deepseek-ai/deepseek-harness（"Everything is a Plugin"） | `c389f96` | TS | 插件化 + 事件溯源 harness；**与 LLM 强相关的 DeepSeek 系** |
| hermes | NousResearch/hermes-agent（"the agent that grows with you"） | `ee84ccd` | Python | **Python 系**、记忆/压缩/cache 工程极深 |
| workbuddy | zhuang-HE/workbuddy-harness | `03108ac` | JS | WorkBuddy 式教学/结构骨架（**定性为反例/结构性参考**） |

**workbuddy-harness 的真实形态**（重要，避免误读）：它不是端到端能跑的生产 harness，而是教学/结构骨架——唯一真实 LLM 调用是 Ollama `/api/generate` 单发（`workbuddy-harness/plugins/multi-agent-orchestrator/index.js:68-116`）；HookRunner 的 `compress`/`sync` action 只打日志不做事（`engine/hook-runner.js:175-178`）；WorkerPool 从不执行任务函数（`plugins/task-orchestrator/index.js:65-71`）。因此本文只把它的**结构性创意**（分维度插件、hook 声明表）当参考，不把它当"成熟实现"来对标。

## 1. 各子系统在"成熟 harness"里的共性取向

（细目见各分册；此处给一页结论。）

| 子系统 | 共性取向 |
|---|---|
| **会话压缩** | ①触发按 token 预算（不是按轮数）：近满才压，保留"recent tail"原文、压缩旧文（pi: `packages/agent/src/harness/compaction/compaction.ts:246-249`、dsh: `packages/compaction/compaction-basic/src/config.ts:144-147`、hermes: head/tail 护住）。②摘要由 LLM 用**独立 system prompt + 严格结构化格式**生成（pi `compaction.ts:420-494`）；增量 = 旧摘要+新消息**合并更新**而非每次全量重写。③压缩自身是可观测的持久事件（reason: threshold/overflow/manual、usage、tokensBefore）——pi `compaction.ts:98-109,138-141`。④剪断绝不切开 tool-call/result 对（dsh `region.ts:124-129`）。⑤有"极长单步把上下文打爆"的 overflow 恢复路径，不是只靠兜底文案（pi `drive/response.ts:188-247`）。 |
| **提示词组装** | ①系统提示**不是一块常量文本**，而是"命名 section × 有序槽位"的注册表，插件可插槽（dsh `system-prompt/src/index.ts:121-154`）；或按缓存友好性分 **stable/context/volatile 层**，不稳定内容押到尾部（hermes `system_prompt.py:604-611`）。②动态上下文/记忆以**合成 user 消息**进历史、旧快照被新快照取代，而非塞 system（dsh `agent-loop/src/runtime-context.ts:64-75`）。③预留 `transform_context` 型 hook，在真正发请求前统一改写 messages+systemPrompt（pi `agent-harness.ts:444-445`, `drive/generation.ts:190-201`）。 |
| **loop 循环** | ①**不是计数器驱动**的裸 while：要么事件溯源状态机 + 显式终止/重试/等待态（pi `runtime/drive.ts:48-105`），要么 phase helper 管道 + verdict（return/break/continue）（hermes `conversation_loop.py:1252-1504`）。②外部故障（LLM/工具）→ 重试有**退避与"该不该重试"的策略**，不是立即兜底（dsh `llm-retry`, `llm/llm/src/retry-policy.ts:14-24`）。③防死循环 = 插件观测注入提醒，不在循环体写硬上限（dsh `guard/repeat-tool-reminder/src/index.ts:213-232`）。④**持久化先于副作用**：工具调用行在副作用前落库（hermes `turn_tool_round.py:52-55,116-119`）。⑤文本 stop ≠ 终态：可用 stop-gate 否决并续一轮（hermes `turn_stop_gates.py:104-139`）。 |
| **log 观测** | ①**事件日志即真相**（append-only），消息历史/计量/遥测都由它派生，而非日志只是"观感"（dsh `session/src/types.ts:260-358`）。②结构化事件走**类型化词汇表**（每事件带 scope + 固定字段），不是自由串 key=value（pi `telemetry.ts` schema、dsh `SessionEventMap`）。③monitoring/遥测与业务解耦：fire-and-forget、非阻塞、绝不向调用方抛错、丢旧保新（hermes `monitoring/emitter.py:1-7`）。④热路径观测**不携带内容**（无 prompt/工具参数/历史）；携带内容的 trace 默认脱敏私有（hermes `monitoring/events.py:1-7`, `trace_upload.py:145-165`）。 |
| **tool 管理** | ①工具是**声明式对象**：schema(参数) + execute + 输出 schema + 元数据(replay/并发安全/超时)（pi `types.ts:386-412`、dsh `tools/src/index.ts:213-280`）。②模型可见的 schema 是**白名单投影**，声明与执行同一来源，杜绝"模型看到不能用"（dsh `tools/src/index.ts:1245-1257`）。③执行走**分层事件管线**：pre-execute 审批 → execute(around:超时/重试) → post-execute(替换/拦截) → result（dsh `tools/src/index.ts:129-200`）。④防死循环/重复用观测控制器：同 (tool,args,result) 连续命中→提醒；字节相同大结果→引用 stub 省 token（hermes `tool_guardrails.py:403-440`）。⑤**读/写、幂等/易变显式分类**，并行只在无写者冲突段内（hermes `tool_dispatch_helpers.py:133-143`）。 |

## 2. 判断基准与目标形态（本版修订的核心）

> **修订**：本文初版假设"本项目功能简单、长期也是简单引擎"，把一批结构机制判为"场景不符/不做"。据此轮确认，**愿景是本 idol-agent 引擎要有约 ⅓ 编码 agent harness 的复杂度**，分布在四轴：**引擎结构与可扩展性、规模与可靠性、观测与成本、记忆与压缩纵深**（另有同类方向项）。故下文把判断基准从"现在简单→不做"改为 **"现在立骨架 + 按阶段长到目标形态"**；只有**纯编码域机制**仍在任何阶段都不做。

### 2.1 四档处置（替代旧的"值得借鉴 / 刻意不为"二分）

| 档 | 处置 | 例 |
|---|---|---|
| **S0 本轮立骨架** | 现在就把结构接缝立起来，行为基本不变，但为后续阶段铺路 | 压缩可观测、loop 分阶段/护栏、提示词分层、tool 单一 schema 源、事件词汇表 + usage 记账、幂等/隔离不变量 |
| **S1 下一阶段主体** | 目标形态的主体结构，值得**提前实现**而非永远不做；在 S0 骨架上加行为 | 压缩元数据 + retainedTail 落库（schema 变更）、溢出恢复压缩续跑、工具分层注册/审批语义、usage 驱动的预算校准、可恢复重试事件化 |
| **S2 远期 1/3 全貌** | 触发条件满足才做（工具面变大、多进程、有平台），做成可插拔，不阻塞 S0/S1 | 并行工具段、progressive disclosure、事件日志导出到平台（OTel）、多 worker / outbox / 分布式 mailbox、prompt cache 工程化 |
| **L4 领域外（永不）** | 与"偶像情感陪伴 + 单例零状态 + 只读工具"领域冲突或纯编码域 | LSP/文件沙箱、ptc/run_code 直调、verify-on-stop/kanban、全量事件溯源状态机、dsh 插件宿主（emit/serial/waterfall） |

> 说明：progressive disclosure 在 hermes 中即 "Tool Search"（`model_tools.py:444-478`），二者是同一种"工具 schema 挤占窗口时延迟揭示"的机制 → 归 **S2**（工具面变大触发），不列入 L4。

### 2.2 目标形态定义（⅓ 编码 agent harness 具体指什么）

**要长出（对标对象）**：显式分阶段的 agent-loop（termination/retry/护栏是显式状态而非裸 while）；提示词 = 有序命名节的组装面（persona 稳定层 + 可变层押尾）；工具/能力 = 单一 schema 声明源 + 白名单投影 + 注册分层（为将来更多 skill/tool 预留）；log = 类型化事件词汇表 + usage 记账 + 压缩/失败/预算可观测；压缩 = 结构化增量摘要 + 元数据 + 溢出恢复。

**仍不追求（与编码 agent 的差距 = 那 ⅔，属 S2/L4 而不属"本轮"）**：事件溯源作状态真相源并全量重放、多 agent/subagent/多轮协作编排、代码执行域的工具与守卫（LSP/沙箱/run_code/verify-on-stop）、插件宿主与三大派发语义、progressive disclosure 的编码级规模、显式 cache_control 断点工程、OTel collector 交付、分布式/多进程调度。

> 一句话判据：**结构照搬，深度克制。** 我们要的是编码 agent 的 *shape*（骨架与分层），不是它的 *scale/domain*（代码工具与多 agent 编排）。对照样本：pi/dsh 的 shape 可借，hermes 的 cache/线程/多平台深度克制，workbuddy 只借分维骨架。

### 2.3 修订后对各候选机制的处置（一句话）

- 事件溯源**状态机 / 全量重放** → **L4**（状态留在 MySQL，不引入）；但"关键动作可观测事件 + 幂等重放"→ **S0 起就做**（04）。
- 多工具**并行段编排 / progressive disclosure / Tool Search** → **S2**（工具面小，触发条件未到）；核心工具不延迟的原则照搬。
- **persist-before-execute 回滚语义** → 现阶段工具只读幂等，**S2**；一旦出现首个可写工具即上调（05 有守卫分层接缝）。
- **多缓存断点工程（cache_control）** → **L4**（DeepSeek 前缀缓存非显式断点）；但"system 分稳定/可变层、可变层押尾"→ **S0 做**（02，为前缀缓存服务）。
- **溢出恢复压缩续跑 / retainedTail / 压缩元数据落库** → 从 P2 上调 **S1**（记忆与压缩纵深轴；schema 变更经你批准）。
- **异步 outbox / 多 worker / 分布式 mailbox** → **S2**（规模轴触发条件：真到多进程/高并发）；mailbox/幂等接口现在就必须保持可替换（已满足）。

## 3. 本项目现状快照（as-is，供分册引用）

- 会话：一用户一会话；`sessions.summary_text/summary_tokens/summary_seq` 存摘要（`database-schema.md:32-45`）；消息单表 `type∈{user,agent,tool}` + `seq` + `token_count`（写入时估算，`repository/tokens.py:10-22`），工具/回复按 `origin_message_id` 挂回 user（`agent.py:265-286`）。
- Agent.run = 一次 while，`max_run_steps` 硬护栏超预算给体面兜底文案；LLM HTTPError / 工具异常各自给兜底，不重试（`agent.py:78-133`）。
- 压缩：token 预算超 `window_token_budget` 且可压缩轮 > `keep_recent_turns` 才触发；把旧摘要+待压缩轮文本喂 LLM（单一自由文本指令），失败则就地近似合并；boundary_seq 前移（`agent.py:141-215`）。
- 提示词：PERSONA 常量 + 摘要一段 + 提问 skill 注入，纯 `\n\n` 拼接（`agent.py:220-233`）；工具 spec = 两个静态 dict，`CapabilityRunner.run` 按 name 硬编码分发（`agent/capabilities.py:28-36`）。
- log：自研 JsonFormatter + BoundLogger，事件= logger.info("event", key=val)，带 scope/run/step；无类型化事件表、无遥测后端、无 usage 记账（`logging_setup.py`）。
- tool：静态 JSON schema + 执行函数成对出现（`agent/tool/`, `agent/mcp/`）；检索/搜索均只读、幂等、低风险。

## 4. 汇总建议（按阶段：S0 立骨架 / S1 主体 / S2 远期 / L4 领域外）

> 编号 R1–R20 与各分册内的建议编号**一一对应**（各册从自己首条建议续编到自己的末条，全局唯一）。**修订说明**：初版以 P0/P1/P2 分层；愿景修订后换 S0/S1/S2/L4 分层，并在 03 新增 loop 结构项 R12（故 04 → R13–R16、05 → R17–R20）。S1/S2 的机制建议不以 R 编号展开（涉及 schema/多进程等须单独立项），由 2.3 + 本表给出。

### S0 —— 本轮立骨架（R1–R20，均不改表，行为不破坏既有契约）

| # | 建议 | 分册 | 对应轴 |
|---|---|---|---|
| R1 | 压缩摘要结构化格式 + 增量合并（LLM 失败保留旧摘要不动） | 01 | 记忆纵深 |
| R2 | 压缩保留策略改 "recent tail token 预算 + 整轮回溯" | 01 | 记忆纵深 |
| R3 | 压缩过程可观测（compress_done 事件） | 01 | 观测 |
| **R12** | **loop 主干显式阶段化（verdict 管道）：裸 while → 有限阶段 × 显式 verdict** | **03** | **引擎结构** |
| R8 | LLM/工具瞬时故障 → 有限重试 + reason 事件，保留兜底 | 03 | 可靠性 |
| R9 | 超步数先"压缩一次后续跑"，仍超再兜底 | 03 | 记忆+可靠 |
| R10 | run 收尾显式 `finish_reason` | 03 | 观测 |
| R11 | 防重复工具调用用观测提醒 | 03 | 可靠性 |
| R13 | log 集中事件词汇表 | 04 | 观测 |
| R14 | LLM provider 透出 usage，事件/run 带 token 记账 | 04 | 观测/成本 |
| R15 | 压缩/预算原因字段落位 | 04 | 观测 |
| R16 | 观测/业务不混（不记正文/工具参数） | 04 | 观测 |
| R5 | 提示词组装分节 + 稳定层/可变层（persona 稳定前缀） | 02 | 引擎结构 + 成本 |
| R7 | skill 注入统一注入口（`_assemble_system_sections`） | 02 | 引擎结构 |
| R6 | 压缩摘要改独立摘要消息（DeepSeek 多段兼容待冒烟） | 02 | 记忆+成本 |
| R4 | 摘要输入预算内整轮取样（不硬截断） | 01 | 记忆纵深 |
| R17 | 工具单一声明源 + 白名单投影 | 05 | 引擎结构 |
| R18 | 工具入参声明式校验 | 05 | 可靠 |
| R19 | 工具执行元数据 + 同参重复护栏 | 05 | 观测/可靠 |
| R20 | skill/tool 声明分家但可枚举 | 05 | 引擎结构 |

> S0 内部：**R1-R3,R5,R7,R12,R13,R14,R17** 是"骨架接缝"（含 loop 结构 R12），其余是"主体补强"。若你按版本收口，骨架类优先。

### S1 —— 目标形态主体（上调：原"P2/不借鉴"里与四轴强相关的项）

| 机制 | 分册 | 为什么现在值得提前实现 |
|---|---|---|
| **压缩元数据 + retainedTail 落库**（tokensBefore/usage/触发原因/保留原文轮） | 01/04 | 记忆纵深轴的核心；R3 只给事件、不给持久化 → 做一次 schema 变更（sessions 加列或旁 JSON）即闭环，可回答"省了多少、留了什么原文" |
| **溢出恢复：单步打爆上下文 → 自动压缩续跑** | 01/03 | 记忆纵深 + 可靠：长会话/长工具结果迟早触发；当前 `llm_max_tokens` 小只是掩蔽。pi `response.ts:188-247`、dsh overflowRetries 为参照 |
| **工具分层注册语义**（register 进 scoped 层 + restrict 可见性 + 单调 guard） | 05 | 引擎结构：工具面会从 2 个长大，现在定义就预留 allow/deny/approve seam，避免将来推倒 |
| **提示词命名节注册表 + 严格插值**（轻量版 dsh section 表） | 02 | 引擎结构：S0 的分节组装升级成"有序命名节"面，新增 skill/记忆 = 注册一节而非改拼接函数 |
| **usage 驱动的压缩预算校准** | 01/04 | 成本轴：R14 收到真实 usage 后，压缩阈值从字符估算切到 usage 反馈 |

### S2 —— 远期 1/3 全貌（触发条件满足才做，接缝不阻塞）

| 机制 | 分册 | 触发条件 |
|---|---|---|
| 异步 outbox / 多 worker 进程 / 分布式 mailbox / Redis 锁 | 03 | 真到多进程或高并发吞吐（mailbox 接口已可换，不阻塞） |
| 事件日志导出到平台（OTel / collector） | 04 | 出现运维/指标平台诉求 |
| 并行/并发工具段编排（writer-conflict barrier） | 05 | 工具面 > ~5 且出现可写/慢工具 |
| progressive disclosure / Tool Search | 05 | 工具 schema 体积大到挤占上下文窗口 |
| persist-before-execute 回滚语义 | 05 | 出现首个非只读、可中断工具 |
| prompt cache 断点工程化 | 02 | 换到支持显式 cache_control 的 provider |

### L4 —— 领域外（任何阶段都不做）

全量事件溯源状态机（状态留在 MySQL）、多 agent/subagent 协作编排、LSP/文件沙箱/代码执行守卫、ptc/run_code 直调、verify-on-stop/kanban 续跑、dsh 插件宿主三大派发、DeepSeek 前缀缓存无需显式断点。

> 表结构约束提示：**S0 全部不改表**。**S1 的"压缩元数据/retainedTail 落库"需一次 schema 变更**（遵循 `database-schema.md` 就地更新，由你裁决是否引入）；S1 其余项不改表。S2 的多进程/导出按其自身立项。

## 5. 分册索引

- [`01-context-compaction.md`](01-context-compaction.md) — 会话压缩（阈值/切点/摘要格式/增量/保留尾/overflow）
- [`02-prompt-structure.md`](02-prompt-structure.md) — 提示词结构设计与组装（分节/分层/注入/缓存友好）
- [`03-loop-strategy.md`](03-loop-strategy.md) — loop 循环（终止语义/重试/护栏/预算/兜底）
- [`04-log-strategy.md`](04-log-strategy.md) — log 观测（事件化/遥测/usage/内容边界）
- [`05-tool-management.md`](05-tool-management.md) — tool 管理（schema 源/投影/执行管线/护栏/只读策略）

## 6. 调研取材与复现

本地 clone 位于 `_research/<repo>`（未入库，仅调研用）。引用行号对应上述 commit；如需复现请以 `git clone --depth 1` + checkout 对应 commit 后按行号核对。分册内的摘录均为逐字引用。
