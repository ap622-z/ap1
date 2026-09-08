# Research notes — 我们系统的 as-is（002 调研基线）

全部为 `d:\work\ap1` 实际代码阅读所得，path:line 相对仓库根。

## 约束与目标
- 偶像 Agent 情感陪伴；单例零用户状态 Agent + 多用户数据隔离。术语见根 `CONTEXT.md`（Scope/Repository/Worker/Mailbox/Outbox）。
- 外部依赖全 Docker；后端 FastAPI/Python；表结构见 `doc/constitution/database-schema.md`。
- 上线验收 = 用户本人手动；tests/ 为开发期正确性产物，只测用户可见外部行为（E2E 接缝唯一）。
- MVP 已验收通过（用户手动）。本轮 002 = agent-optimize，允许动 agent 内部实现，但不可引入产品功能/表结构破坏。

## 关键表（schema 宪法）
- `sessions`：summary_text / summary_tokens / summary_seq（压缩摘要落本表，`database-schema.md:32-45`）。
- `session_messages`：单表 type∈{user,agent,tool}；seq 单调；payload JSON；token_count 写入时估算；origin_message_id 归并轮次（`:49-79`）。
- 一用户一会话（sessions.user_id UNIQUE）；隔离靠 env 实例 + Scope。

## token 估算
- `repository/tokens.py:10-22`：CJK 字符 × token_estimate_per_char(0.6) + 其它/4，`math.ceil`。无 provider usage 校准。

## Agent（backend/agent/agent.py）
- `PERSONA` 常量 + `SUMMARY_INSTRUCTION`（`agent.py:26-36`）。
- `run()`：一次 run = while 循环，`step` 计数；`max_run_steps`(8) 超预算 → 体面兜底文案 break（`agent.py:78-91`）。
- 每 step 调 LLM；`tool_calls` → 逐工具 `self._runner.run`，异常不入死循环 → 把 {ok:false} 交还模型（`agent.py:114-133`）。
- LLM HTTPError → ERROR 日志 + 体面兜底（`agent.py:86-91`）。
- 上下文组装 `_assemble_context`（`agent.py:141-172`）：
  - 读 session.summary_* → min_seq；`list_messages_window_tail` 取 `(summary_seq, user.seq]` 最新 limit=max_turns_in_window*3 行。
  - `_group_rounds` 按 origin_message_id 把 tool/agent 挂回发起 user（`agent.py:265-286`）。
  - token 预算：summary_tokens + rows.token_count；> window_token_budget 且可压缩轮数>keep → 压缩；仅保留轮也超预算 → 保守全留（warning，`agent.py:169-171`）。
- 压缩 `_compress`（`agent.py:174-215`）：旧摘要 + 待压缩轮组装文本（[用户]/[工具]/[偶像] 行）→ LLM（SUMMARY_INSTRUCTION，max_tokens 600）→ 新摘要；LLM 失败 → 就地近似合并前 2000 字符。boundary_seq = 压缩轮内最大 seq；`write_summary` 落库。注意：`content[:20000]` 硬截断输入。
- system prompt `_system_prompt`（`agent.py:220-233`）：PERSONA + "（此前对话摘要…）" + 提问 skill 注入（question_skill_trigger → guidance）。无其它结构化分区、无稳定 XML 容器。
- `_rounds_to_messages`（`agent.py:235-263`）：历史轮 user/tool/agent 重放为 assistant tool_calls + tool 消息（忠实重放）。
- `_call_llm`：tools = runner.tool_specs，max_tokens=llm_max_tokens(1024)（`agent.py:291-299`）。
- `AgentRunResult`：reply_text + tool_records + run_id + compressed（records.py:56-61，compressed 当前未用）。

## Worker / Mailbox / 幂等
- `worker/worker.py`：幂等（client_message_id）+ 生命周期 + 调 agent + 结束事务统一落库（`worker.py:32-72`）。处理中失败 → revert_to_received 供重发续跑。
- `worker/mailbox.py`：进程内按 session_id 的 asyncio.Lock，弱引用回收。
- 异步投递是演进接缝（Redis 锁 / MQ），当前未用。

## 能力层
- `agent/capabilities.py`：CapabilityRunner.tool_specs = [RETRIEVE_TOOL, WEB_SEARCH_TOOL]（静态列表）；run() 按 name 硬编码分发（capabilities.py:28-36）。
- `agent/tool/__init__.py`：RETRIEVE_TOOL 静态 JSON dict + retrieve()。工具输出 `{ok,found,items}`。
- `agent/mcp/__init__.py`：WEB_SEARCH_TOOL 静态 dict + web_search()；provider.search → {title,content,url}。
- `agent/skill/__init__.py`：question_skill_trigger 规则式（代词/短句/疑问词），命中把 guidance 注入 prompt，tag `[question-skill]` 供观测。skill 非可调用函数。
- 无 schema 类型、无 per-tool replay/parallel、无动态加工具、无 before/after hook。

## LLM/搜索 provider
- `agent/providers.py`：LLMProvider chat → `/chat/completions`（model/messages/max_tokens/temperature 0.7 + 可选 tools/tool_choice auto），超时 120s；WebSearchProvider.search → `{base}/search`（Tavily 兼容），超时 30s。均 Bearer key，可指向本地桩。
- 对话本身不流式（一次完整返回）；tool call 内参数 json.loads 失败容错为 {}（providers.py:71-73）。

## Log（logging_setup.py）
- JsonFormatter + BoundLogger：事件 = logger.info("event_name", key=val, ...) 关键字段进 extra → JSON 行（ts/level/logger/event/…）。绑定 scope(env/user/session) 与 run_id/step。
- 事件命名约定：run_start/run_finish/step_start/step_finish_text/tool_call/tool_failed/llm_unavailable/context_compress_trigger/compress_llm_failed/skill.question 等。
- 无 typed span、无事件总线、无全局 trace id、无 usage 上报。日志=观测；run/step 不落库。
- 噪音 logger（httpx/sqlalchemy 等）压到 WARNING。

## 配置（config/settings.py）
- 上下文/压缩参数：window_token_budget=6000、keep_recent_turns=10、max_turns_in_window=400、max_run_steps=8、llm_max_tokens=1024、token_estimate_per_char=0.6。
- 无 compaction reserve/keepRecent tokens 分离；无 provider usage 反馈。

## 当前 loop 的已知取舍
- 无步骤间持久化（run 只进日志；同 client_message_id 重发=整 run 续跑而非从断点续）。
- 无 overflow compaction（超长回答/超上下文 = 直接兜底文案，不压缩后重试）。
- 无重试等待态（LLM 失败立即兜底，不按退避重试）。
- 单用户一次 run 的 step 护栏是硬 break；工具失败不 abort。
- 摘要无 tokensBefore/usage 记账；不保留跨压缩的 retainedTail；工具历史压缩后丢失（只进摘要文本）。
