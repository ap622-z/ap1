# 04 log / 观测策略 — 调研与设计建议

> 样本与引用规范见 `00-research-and-design-summary.md` §0、§6。摘录均为逐字引用。

## ① 四家样本的真实实现

### pi（@ 3e4bc26）

- **双层观测**：被动事件总线 `HarnessEvent`（`harness/events.ts`）+ 类型化遥测 span（`harness/telemetry.ts`）。
- 事件词汇表（`telemetry.ts:163-192`）覆盖 run_start/run_end、turn_start/turn_end、message_*/tool_start/tool_update/tool_end、retry_scheduled/start/end、compaction_start/end、navigation_*、usage、fault、handler_error、entry_added…**同一份枚举贯穿**。
- **遥测 schema-first、带编译期类型约束**：每个 span 声明 required 属性与低/高基数（`HARNESS_TELEMETRY_SCHEMA` `telemetry.ts:233-592`）。例：`pi.harness.run` span 带 outcome∈{completed,aborted,failed,suspended}；`pi.harness.step` 带 step.kind/attempt/compaction.reason；`pi.harness.tool` 带 tool.replay/recovery/is_error。
- 遥测上下文挂在 chord Context 上（`context.ts:30-37`），无全局单例，缺省 NOOP。

### dsh（@ c389f96）

- **事件溯源日志即真相**：append-only 可 merge 扩展的 `SessionEventMap`（turn/step、user/assistant/tool、request/header/context 事件），"Message history is derived from this log. Every event is lossless JSON"（`packages/core/session/src/types.ts:260-358`）；各包 module augmentation 增补词汇（如 `compaction/*`、`llm/retry*`）。
- **append 即广播**：先持久入 log，后 `session/event` firehose 同步派发，监听器 contained（异常不回流）（`session/src/index.ts:700-728`）。
- **面向 agent 的 live 总线三种派发语义**：`emit`(fire-and-forget, per-listener 容错)、`serial`(顺序可 bail)、`waterfall`(around 中间件可改写结果)，都以 agent 为 scope carrier（`core/agent/src/dispatch.ts:54-149`）。
- **遥测捕获/导出可替换**：`SessionTelemetryRecord {channel:'ledger'|'ops'; severity; attributes; body}`，一条记录 = canonical 事件的一对一镜像；sink emit 必须非阻塞入队；OTel 后端映射 Logs（resource 带 service 名/版本 + 匿名 user.id）（`session/session-telemetry/src/index.ts:64-132`、`session-telemetry-otel/src/index.ts:204-234`）。

### hermes（@ ee84ccd）

- **观测平面与业务解耦到极致**：fire-and-forget 队列 + 后台分发线程 = 唯一 seam；热路径不变量 `emit()` 微秒级、不碰盘/网、绝不向调用方抛错；队列满丢**最旧**；nothing persisted——monitoring 是 egress 不是存储（`agent/monitoring/emitter.py:1-7,40-60`）。
- **观测事件 content-free**：不携带 prompt/messages/工具参数结果/会话历史/usage 分析（`agent/monitoring/events.py:1-7`）。携带内容的一律脱敏私有再出口（trace JSONL 强制脱敏，`agent/trace_upload.py:145-165`；trajectory 训练文件 `agent/trajectory.py:23-27`）。
- **内容无关的 JSON 事件行**也广泛存在，如 compaction 遥测：`logger.info("micro compaction telemetry: %s", json.dumps(payload))`（`agent/micro_compaction.py:313-314,339`）——自述 content-free 且字段固定（tokens_delta、*_total）。

### workbuddy（@ 03108ac）

- 朴素方案：timestamp logger（`engine/utils.js:84-103`）、JSON execution-history（`:105-136`）、state.json 指标（`:7-52`）、dashboard 硬编码 DATA + fetch state.json（`dashboard/harness-dashboard.html:79-128`）。可参考其"指标计数 + 历史文件"思路，但成熟度低。

## ② 共性取向

1. **观测三分离**：① append-only / 事件化的**持久日志**（真相源，可重放派生状态）；② 与业务解耦的 **live/遥测通道**（fire-and-forget、非阻塞、绝不向调用方抛错）；③ 带内容的 trace/trajectory（默认私有 + 脱敏）。
2. **事件是类型化词汇表**：每事件带 scope 与固定字段（含 usage），不是自由文本 key=value；reason/outcome 枚举化，保证可检索与可比对。
3. **观测不携带热路径内容**：prompt/消息体/工具参数不进通用观测；需要内容的出口单独走脱敏。
4. 对**失败、重试、压缩、预算**有专门事件（retry_scheduled/compaction_start/step outcome），这是"可诊断性"的命门。

## ③ 本项目现状与差距

现状：已有一套自研结构化 JSON 日志——`JsonFormatter` + `BoundLogger`，`logger.info("event", key=val, ...)`，每条带 scope(env/user/session) + run_id/step，事件名近似受控（`logging_setup.py`）。事件有 run_start/step_start/tool_call/llm_unavailable/context_compress_trigger 等（`agent.py` 各处）。

差距排序：
- ① **事件名无"受控词汇表"**：目前是散落的字符串约定，无集中枚举；字段无 schema，靠人工一致。
- ② **usage / token 无记账**：LLM 调用不把 usage 落日志（providers 也没透出 usage），无法回答成本与上下文增长。
- ③ **压缩/预算/失败的原因字段不全**：`run_finish` 只有 steps/tool_count（`agent.py:135`），无 finish_reason；压缩有 trigger 日志但无 tokens_before/after（见 01）。
- ④ 观测与业务同进程同线程：虽已写文件（`setup_logging` 可指 log_file），但无"后台队列 + egress"解耦；当前单进程低并发可接受，暂不需改。
- ⑤ 无内容脱敏 / 上传路径（我们也无需上传；但"内容不进观测"原则要守住——目前 logger 不记消息正文，是对的）。

## ④ 设计建议

**R13（S0）引入"事件词汇表"：集中枚举事件名与关键字段，散落事件收敛**
- 新增一个纯常量模块（如 `backend/agent/events.py`，或沿用 `logging_setup` 扩展）定义本系统事件枚举与字段：
  - run：`run_start / run_finish(reason, steps, tool_count, llm_calls, tokens_est)`
  - step：`step_start / step_finish_text / step_budget / step_compress_retry`
  - llm：`llm_call(step, attempt) / llm_retry(attempt, backoff_ms) / llm_unavailable(reason, attempt)`
  - tool：`tool_call / tool_retry(attempt) / tool_failed(reason)` / tool_result(tool, ok)
  - compress：`compress_trigger(budget_tokens, compressible_rounds) / compress_done(summary_tokens, tokens_before) / compress_failed(reason)`
  - skill：`skill.question_injected`
  - idempotency：`worker.idempotent_reuse / message_retry_resume / message_processing_failed`
- 所有 `logger.info("字面串"...)` 改走枚举名（字符串字面量仍作为 logger 名？不——事件名是 `record.getMessage()` 里的第一参数，见 `JsonFormatter` `event: record.getMessage()`）。**落地时注意**：现 `event` 字段即 `getMessage()`（`logging_setup.py:29`），所以枚举的是事件字符串本身；建议设模块级常量 `EV_RUN_FINISH = "run_finish"` 等，统一引用，避免拼错。
- 这是纯内部重构，不改外部行为；`test_run_step_logs_carry_scope_and_replayable`（`tests/test_e2e.py:362-371`）只查 run_start/step_start 存在 + scope 字段，保持兼容。

**R14（S0）补 usage 与 token 记账**
- `LLMProvider.chat` 返回值已有 `finish_reason`，但未透出 usage（`providers.py:64-84` 只取 choices 消息体，丢弃 `data.usage`）。建议 `LLMResult` 增加 `usage: {prompt_tokens, completion_tokens, total_tokens}`，`_call_llm`（`agent.py:291-299`）把它并入 `llm_call` 事件与 run 汇总。
- 收益：A22/长会话验收能回答"每 run 消耗与上下文增长"；也为 01 的 token 预算（现靠 `estimate_tokens` 字符启发，`tokens.py:10-22`）将来接真实 usage 做校准打底。注意 `estimate_tokens` 仍保留（它用于**入库存档**每条消息；真实 usage 用于观测与预算校正，两者不冲突）。

**R15（S0）压缩/预算原因字段落位（联动 01）**
- 按 01 R3 给 compress_* 事件带 `tokens_before / tokens_after / boundary_seq / llm_ok`；run_finish 带 `finish_reason`（见 03 R10）。可回答"为什么这句是软话 / 压缩省了多少"。

**R16（S0）观测/业务不混：保持"日志不记消息正文、不记工具参数值"，必要时单独脱敏**
- 现代码已基本守这条（工具失败只记 `error=str(exc)`，不记入参，`agent.py:119`）。建议把这一约定写成日志模块的明确 docstring 条款（见 hermes content-free 原则 `monitoring/events.py:1-7`），防止后续有人把用户隐私文本打进日志。若要调试工具入参，走 debug 级且明确字段，仍不带全文。

### S1 —— 上调（观测与成本轴）
- **"观测事件 ← 状态变更"的结构化接缝**：现在事件是散点 `logger.info`；S1 让"一次 run/tool/压缩/重试的关键状态变更"都产出**可枚举、可检索的事件行**（含 usage、scope、duration），可回答"这次回复为什么是软话、花了多少 token、哪一步卡了"。这是四轴中观测与成本的直接落点，R13/R14/R15 是它的 S0 骨架。
- **event sink 抽象（半程）**：把 `logging_setup` 的写出口抽象成"结构化事件 sink"（当前 = JSON 文件行），为将来接平台留 seam；**不做** hermes 的 fire-and-forget 后台线程队列（单进程低并发时是过度设计），只在出现真实吞吐压力时升到 S2。

### S2 / L4 —— 远期触发 / 领域外
- **OTel/OTLP/外置 collector**（hermes/dsh）→ **S2**：出现运维/指标平台诉求时再接（届时事件 sink 抽象已就位，只加 exporter）。不做为现状。
- **事件溯源 full replay / 派生消息历史**（dsh/pi）→ **L4**：状态仍在 MySQL，日志是观测不是状态源；观测所需的"可重放"由幂等键 + run 事件承担，不引入全量投影。
- **live 三派发语义 + hook 化扩展**（dsh）→ **L4**：无插件系统；若 S1 真需要多消费者，用 sink 订阅而非三大派发。

**验收影响**：以上均为日志/观测增强，不改协议与行为。既有 `test_run_step_logs_carry_scope_and_replayable` 保持绿；新增字段不破坏既有断言（JSON 事件多字段不会让现有 key 断言失败）。
