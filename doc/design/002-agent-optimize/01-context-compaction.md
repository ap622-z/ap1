# 01 会话压缩（context compaction）— 调研与设计建议

> 样本与引用规范见 `00-research-and-design-summary.md` §0、§6。摘录均为逐字引用。

## ① 四家样本的真实实现

### pi（earendil-works/pi @ 3e4bc26）

- **阈值触发，不数轮数**：`packages/agent/src/harness/compaction/compaction.ts:246-249`
  ```ts
  export function shouldCompact(contextTokens: number, contextWindow: number, settings: CompactionSettings): boolean {
      if (!settings.enabled) return false;
      return contextTokens > contextWindow - settings.reserveTokens;
  }
  ```
  默认 `reserveTokens: 16384, keepRecentTokens: 20000`（`compaction.ts:157-161`）。即：context 满到只剩保留余量才压。
- **token 计量：优先用 provider 真实 usage，缺省回落字符启发**：`estimateContextTokens` 取最后一次 assistant usage 块 + 其后消息按 `chars/4` 估（`compaction.ts:214-243,270-310`）；图片按 ~4800 字符计。
- **切点保"recent tail"、不切开工具配对**：`findCutPoint` 从尾部累积 token 到 ≥ keepRecentTokens 找候选切点（`compaction.ts:370-418`）；`findValidCutPoints` 明确 **toolResult 不能作切点**（`compaction.ts:311-340`）；切点若落在 turn 中间则回溯到 turn 起始（`findTurnStartIndex` `compaction.ts:343-357`）。
- **LLM 生成摘要，独立 system prompt + 严格结构化格式**：
  `SUMMARIZATION_SYSTEM_PROMPT`（`compaction.ts:420-422`）："Do NOT continue the conversation… ONLY output the structured summary."
  `SUMMARIZATION_PROMPT`（`compaction.ts:424-455`）：固定章节 `## Goal / ## Constraints & Preferences / ## Progress(Done/In Progress/Blocked) / ## Key Decisions / ## Next Steps / ## Critical Context`。
- **增量合并而非全量重写**：`UPDATE_SUMMARIZATION_PROMPT`（`compaction.ts:457-494`）——"PRESERVE all existing information from the previous summary. ADD new progress… UPDATE the Progress section… PRESERVE exact file paths/function names/error messages"。首压用新格式，之后都走合并。
- **摘要请求独立、不计入缓存、带重试**：`createSummaryRequestOptions`（`compaction.ts:117-125`）`cacheRetention:"none"`、fresh sessionId；`completeSimpleWithRetries` 走 retryAssistantCall（`compaction.ts:127-144`）。
- **压缩自身是持久条目**：存 summary + tokensBefore + usage + retainedTail + 文件操作明细；旧摘要被"上一次压缩的 retainedTail + 其后新消息"迭代更新（`prepareCompaction` `compaction.ts:634-707`）。`CompactionResult` 结构见 `compaction.ts:98-109`。
- **注入回上下文是独立消息角色**（`compactionSummary`，外裹 `<summary>` 容器）而非塞进 persona：`messages.ts:9-13` 前缀/后缀、`session/context.ts:37-38` 把 compaction entry 映射回该角色。
- **overflow 恢复**：单步回答超过 contextWindow 或命中长度上限 → 触发"overflow"压缩后 resume（`runtime/drive/response.ts:188-247`），而不是简单兜底。
- 压缩原因三态 `manual | threshold | overflow`（`runtime/drive/structural.ts:138-142`）。

### dsh（deepseek-ai/deepseek-harness @ c389f96）

- 压缩 = 可替换后端引擎（`abstract class CompactionEngine`），自动触发仅两类：`'pressure' | 'context-overflow'`（`packages/compaction/compaction/src/index.ts:25`）。
- **压力检测挂在 agent 事件 seam 上**（`agent/pre-step`），失败仅告警不断言、不打断 turn（`packages/compaction/compaction-basic/src/index.ts:148-166`）——压缩不是循环内部逻辑，是事件监听器，插件可换。
- context-overflow 与重试联动：仅当 `failure.code === CONTEXT_WINDOW_EXCEEDED_CODE` 且重试未超 `maxOverflowRetries` 才压后返回 `{kind:'retry'}`（`compaction-basic/src/index.ts:180-224`）。
- **预算公式按模型 contextWindow 换算绝对 token**：阈值 80%、保留尾部 16% 默认，可覆盖：
  ```ts
  const thresholdTokens = Math.floor(contextWindow * policy.thresholdRatio)   // 默认 0.8
  const retainTokens = ... contextWindow * policy.retainRatio                // 默认 0.16
  ```
  （`packages/compaction/compaction-basic/src/config.ts:20-23,144-147`）
- **范围选择 head-anchored 但留尾部原文，且不切 tool 配对**：`region.ts:100-135`（`toolPairingBalancedBefore` 回退 `:124-129`）。
- **压力 = 对 durable 事件日志的确定性重放计量**（token-meter `measure(session)`），与事件溯源一致（`packages/llm/token-meter/src/index.ts:124-152`）。
- 压缩以 `compaction/start|summary|end|prune` 等**仅进日志的事件**记录；surface 的真实替换由紧随其后的 `user/message` 事件承载（shadow 遮蔽），摘要指令以最后一条 user 消息形式追加，并复用前缀保 KV cache（`compaction/src/types.ts:17-38`、`compaction-basic/src/summarizer.ts:31,121-195`）。

### hermes（NousResearch/hermes-agent @ ee84ccd）

- **多层压缩梯队**：服务端 native（gpt-5.6 直连，阈值压到本地触发点之下让服务端先出手，本地压缩器保持武装作 fallback）→ 本机批量 summary 压缩器（aux 小模型**汇总中段，head 与 tail 受保护**）→ 可选 micro rolling（默认关）。
  - `agent/native_compaction.py:22-27`、`:100-128`
  - `agent/context_compressor.py:1-2`："a cheap auxiliary model summarizes middle turns while head and tail are protected (iterative summaries, token-budget tail, tool-output pruning first, scaled budgets)"
  - `agent/micro_compaction.py:1-6`（默认关：每次 pass 重写 prompt 前缀会击穿 provider prompt cache）；`:203-213` cadence 门 + "只吞 assistant/tool 行、绝不吸收 user 原始文本"。
- **线程安全契约**：批量压缩跑在池化 daemon 线程，消息为私有深拷贝，仅经 `CompressionCommitFence` 提交才发布，超时的工作被丢弃；每会话同一时刻一个 pass（durable lock）（`agent/conversation_compression.py:1-8`）。
- 压缩提交后还会做会话 rotation（SQLite 子会话），并与缓存作用域解耦（`agent/prompt_cache_scope.py:1-10`）。

### workbuddy（zhuang-HE/workbuddy-harness @ 03108ac）

- 存在真实算法层但**从未绑定真实对话上下文/token 窗口**：memory-decay 的 `fourLayerCompression` + 指数衰减 + `_generateAutoSummary`（`plugins/memory-decay/index.js:383-515,520-538`）——摘要为字符串启发（regex 抽关键词），**无 LLM**。
- **HookRunner 的 `compress` action 只打日志、什么都不压**（`engine/hook-runner.js:175-178`）；`sync` 同理（`:170-173`）。即 hooks.json 里声明的自动压缩触发点（`hooks/hooks.json:52-61,128-138`）是**空转**的。
- 结论：workbuddy 作为压缩实现**不可采信**；仅"压缩是插件事件而非循环内逻辑"的结构创意可参考。

## ② 共性取向

1. **触发 = token 预算阈值**（近满才压），并显式保留一段 **recent tail 原文**（pi keepRecentTokens、dsh retainTokens、hermes tail 保护）。保留"最近 N 条原始轮次"是它的具体形态，不是原则本身。
2. **摘要 = LLM + 独立 system prompt + 结构化固定格式**；**增量合并**（旧摘要保留，新消息并入），避免从头重写与信息漂移。
3. **摘要独立请求**：不计入主链缓存、可重试；压缩过程可观测（reason / usage / tokensBefore 记账）。
4. **剪断安全**：绝不切开 tool-call/result 配对；切点在 turn 边界回溯。
5. **溢出恢复是闭环**：单步超长不是"给句软话"，而是压缩后续跑。
6. 事件溯源系的 harness 把压缩当"shadow 遮蔽 + 日志事件"，非删除历史。

## ③ 本项目现状与差距

现状（详见 `notes-our-system.md` / 主档）：
- 触发 = `summary_tokens + rows.token_count > window_token_budget(6000)` **且** 可压缩轮数 > `keep_recent_turns(10)`；否则仅警告并**全留**（`agent.py:163-171`）→ 这是"保留 N 轮 + 一次性剪光"，不是"保留 tail 的 token 预算"。
- 摘要 = 一段自由文本，指令 `SUMMARY_INSTRUCTION` 单一字符串（`agent.py:33-36`）；**每次全量重写**（旧摘要 + 待压缩轮文本喂一个 LLM，`agent.py:174-215`），非增量合并；无固定格式章节。
- 压缩失败 → 就地近似合并旧摘要 + 前 2000 字符（`agent.py:205-206`）——**旧摘要可能被破坏**。
- 摘要输入有 `content[:20000]` 硬截断（`agent.py:198`）。
- summary_seq 边界由压缩轮内最大 seq 决定（`agent.py:207-214`）——能正确表达"已并入摘要的旧消息"，这是好底子。
- 工具轮：**工具的 tool 结果在压缩后被丢弃**（不保留 retainedTail 原文，只有摘要文本里可能提到）；retrieve/web_search 均只读、低风险、幂等，切分安全需求低。
- 无 overflow 恢复：单条超长回答/上下文打爆 → 目前无该路径（DeepSeek max_tokens 固定 1024，且模型侧截断走 finish_reason；不存在我们主动超长）。

**关键差距排序**：① 摘要格式自由文本、无增量合并 → 情感长线易漂移/丢线索；② 保留策略是"固定轮数"而非"tail token 预算"，长轮次场景会误触发把近期重要内容剪掉；③ 压缩无元数据记账（tokensBefore/usage/触发原因）→ 无法观测、无法回答 A22"压缩是否发生、省了多少"。

## ④ 设计建议

**R1（S0）压缩摘要结构化 + 增量合并，失败保留旧摘要不动**
- 把 `SUMMARY_INSTRUCTION` 升级为两段固定指令：首压与合并各一；摘要输出固定中文章节，贴合情感陪伴需要保留的维度：
  - `## 粉丝（用户）已知信息`（昵称/喜好/近况/情绪线索）
  - `## 当前话题与互动`（正在聊什么、进行到哪）
  - `## 偶像回应基调与承诺`（聊过什么约定）
  - `## 未竟事项 / 想再聊的`
  参考 pi 的结构化章节做法（`pi compaction.ts:424-494`）但**业务章节自定**，不搬 coding 场景的 Goal/Next Steps。
- 合并 prompt 必须含"保留旧摘要全部信息、仅更新与追加"约束；**LLM 失败路径改为保留旧摘要原样并记 ERROR 事件**（当前失败会污染旧摘要）。
- 落库仍只写 `sessions.summary_text/summary_tokens/summary_seq`（`database-schema.md:40-42`），**不改表**。为 S1 压缩元数据落库预留：内容仍为单段文本，但内部以固定章节排版，可由机器再切分。

**R2（S0）保留策略从"固定 N 轮"改为"tail token 预算 + turn 边界回溯"**
- 现有 `keep_recent_turns` 保留的是**消息行数**（`list_messages_window_tail` 后按轮分组）。情感对话轮长短差异极大（一句"在吗"与一条长倾诉差数十倍），固定轮数会偶尔把 tail 撑爆窗口。
- 建议新增配置 `keep_recent_tail_tokens`（默认如 2500，占 budget 6000 的大部分），压缩切点从尾部累积 `token_count` 到 ≥ tail 预算为止，且**整轮边界对齐**（不切开 user→tool→agent 的轮）。实现可在 `agent._assemble_context` 现有"读 tail → 分轮"流程后加一次"从尾部回溯到首个超过 tail 预算的整轮"，无需改表。旧的 `keep_recent_turns` 可降级为下限保护（至少保 N 轮）。
- 对齐点：pi `keepRecentTokens`（`compaction.ts:154-161`）、dsh `retainTokens`（`config.ts:144-147`）。

**R3（S0）压缩过程可观测（事件化）**
- 在现有 `log.info("context_compress_trigger")` / `compress_llm_failed`（`agent.py:166,205`）基础上补结构化字段：`tokens_before / tokens_after / summarize_rows / boundary_seq / llm_ok(bool)`，并新增 `compress_done` 事件（含 `summary_tokens`）。让运行期能回答"哪一次触发、原因、省了多少、LLM 是否成功"。见 `04` 的事件词汇表，此处只约定字段。

**R4（S0）摘要输入不再硬截断，改为"可分批/预算内取样"**
- 当前 `content[:20000]` 一刀切（`agent.py:198`）会随机丢尾部。改法：待压缩轮已按 token_count 记账，先按 token 预算（如 16000）从**最早轮**开始取满即止——旧信息丢弃有据可查，且每轮整体保留（轮内完整）。

### S1 —— 上调（目标形态主体，记忆与压缩纵深轴）
- **压缩元数据 + retainedTail 原文落库**（tokensBefore / usage / 触发原因 / 保留的最近原文轮）：R3 只把压缩写进日志事件，未持久化。若你要"能回答某次压缩到底省了多少、丢掉了哪些原文、保留了哪段最近轮"（记忆纵深），需要在 sessions 上加列或 summary 旁 JSON —— **一次 schema 变更**（由你批准，遵循 `database-schema.md` 就地更新）。参照 dsh `compaction/*` 事件带 shadowedRange（`compaction/src/types.ts:17-38`）与 pi compaction entry 带 retainedTail + details（`compaction.ts:98-109`）。注意我们不必引入 dsh 的 shadow/投影——只在 summary 旁多存一段 retainedTail 文本即可。
- **溢出恢复自动压缩续跑**（pi `response.ts:188-247`、dsh overflowRetries `compaction-basic/src/index.ts:180-224`）：当单条超长输出 / 超长工具结果把上下文打爆时，不是给兜底文案而是先压再续。当前 `llm_max_tokens=1024` 且无流式，触发概率低——但工具结果一旦变大（如将来检索返回整页），这是可靠路径。与 03 的 R9 同源，S1 一并落。
- **压缩频率/预算自校准**（按 provider usage 而非字符估算）：R14 收到真实 usage 后，把 `window_token_budget` 从字符估算校准到 usage 反馈（参照 hermes native threshold 钳制思路 `native_compaction.py:22-27,76`）。

### S2 / L4
- 事件溯源式全量重放、shadow 遮蔽、会话 rotation 与缓存血统解耦（hermes `prompt_cache_scope.py`）→ **L4/场景外**：状态在 MySQL，不引入事件溯源投影。真正需要的是 S1 的"摘要 + retainedTail 落库"这一持久化形态，而非投影机制。

**验收影响**：现有 A22（长会话压缩后仍连贯、`test_long_session_compresses_and_stays_coherent`）断言只查 `summary_text/summary_seq` 非空 + 后续可答（`tests/test_e2e.py:225-241`）。R1/R2/R3 均应保持该断言成立；`test_summary_scoped_to_own_session`（隔离）不受影响。S1 的 schema 变更若引入，需补 A22 对"压缩后 retainedTail 可检索"的断言。真实 key 冒烟另验。
