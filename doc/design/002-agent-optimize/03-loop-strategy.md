# 03 loop 循环策略 — 调研与设计建议

> 样本与引用规范见 `00-research-and-design-summary.md` §0、§6。摘录均为逐字引用。

## ① 四家样本的真实实现

### pi（@ 3e4bc26）

- **不是裸 while，是事件溯源持久状态机**：run 期间一切以 append-only 会话 entry 落账；`driveOperation` 在 `for(;;)` 里按当前 durable 操作态 dispatch：
  ```ts
  // packages/agent/src/harness/runtime/drive.ts:48-105
  for (;;) {
      operation = currentOperation(lane, drive);
      ... switch (state.at) {
          case "starting": ... case "checkpoint": ... case "assistant.ready" / "assistant.retry_wait": ...
          case "tools": ... case "summary.deciding" / "summary.ready": ... case "deferred...": ...
      }
  }
  ```
- **终止/转向由响应内容驱动**（`runtime/drive/response.ts:292-318`）：
  - 有 tool_calls ⇒ settle 到 `tools` 态执行工具；
  - 无 tool_calls 且非 toolUse ⇒ `checkpoint` 态 `may_finish`（可正常收尾）。
  - error/aborted ⇒ `assistant.retry_wait` 重试等待或失败；overflow ⇒ `summary.deciding`（压缩后 resume，见 01）。
- **重试 = 持久等待态 + 策略**：全局默认 `{enabled, maxRetries:3, baseDelayMs:1000}`（`harness/config.ts:4`）；每次尝试计为一次 durable `step`，两次之间可取消等待（`runtime/drive/generation.ts:234-282`）。
- **防死循环不在循环体写硬上限，而是"进度不变即异常"**：`drive.ts:101-104` 若某次 procedure 无任何进展且非 cancel_requested 则抛 invariant error。工具 batch 无限制反复进入是靠模型 end 信号 + `terminate` hint 收束（`harness/types.ts:372-376`）。
- 无全局 max-step 计数器；"护栏"由 overflow/abort/重试策略构成。

### dsh（@ c389f96）

- **`ReactLoopAgent`：turn/step 边界由 durable 事件刻画，循环不内置计数器**：
  ```ts
  // packages/core/agent-loop/src/agent.ts:257-341
  while (true) {
      signal.throwIfAborted()
      const step = phase.step + 1
      const decision = await this.preStep(target, { turn, step })
      if (decision.kind === 'reject') { turnEnds = { kind: 'blocked' }; return false }
      ...
      if (turnEnds && decision.messages.length === 0) break
  ```
  step/start、step/end、turn/end(reason) 全部 append 为 durable 事件；turn/end reason ∈ `completed / max-tokens / blocked / aborted / error`。
- **LLM 返回三出路**（`agent.ts:424-475`）：error/aborted → `agent/request-error` waterfall **由插件决定重试或抛出**；max-tokens → sticky 返回；无 tool-call → completed；有 tool-call → `executeToolCalls`，`concludesTurn` 才 completed 否则 while(true) 继续。
- **并发预算而非"N 步上限"**：`DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10`（`agent-loop/src/constants.ts:6`），工具按 `isConcurrencySafe` 分 exclusive(barrier)/parallel(有界池)，abort 给未启动 call 补 synthetic result 保证重放合法（`agent-loop/src/tool-calls.ts:199-260`）。
- **重试是 provider 配置 + 事件化执行器**：默认 maxRetries 5、初始 500ms、最大 10s、抖动 0.1、可重试码白名单（`llm/llm/src/retry-policy.ts:14-24,149-195`）；执行器写 durable `llm/retry` 再可取消 delay 后 `llm/retry-started`，跨重启用 sessionProjections 恢复计数（`llm/llm-retry/src/index.ts:59-238`）。
- **防死循环 = 观测注入提醒插件，绝不否决**：repeat-tool-reminder 在 `tools/post-execute` 统计连续相同 name+canonical args，命中阈值 [3,5,8] 时把提示文本塞进 additionalContexts 当下一轮 user 消息（`packages/guard/repeat-tool-reminder/src/index.ts:213-232`）。超时用 tools/execute wrapper + 声明 timeoutMs → 结构化 TOOL_TIMEOUT 结果（`timeout-policy/src/index.ts:55-81`）。

### hermes（@ ee84ccd）

- **循环拆成 phase helper 管道**：`_LoopState` 承载所有循环局部变量，helpers 以同名字段传入/回拷，verdict ∈ return/break/continue（`agent/conversation_loop.py:1252-1259`）。
- **主循环结构**（`conversation_loop.py:1484-1504`）：
  ```python
  while (s.api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
      ... begin / prepare / assemble / run_preflight_gate（可 return/break/continue）
      ... announce_api_call → API retry loop → normalize → 按 tool_calls 分发
  ```
  迭代预算 = 线程安全计数器，parent cap 500、subagent 默认 50；`execute_code` 回合 refund（`agent/iteration_budget.py:25-40`）。
- **persist-before-execute 是硬不变量**：工具副作用前先把 tool-call turn 落库；持久化失败即结束该 turn，绝不在纯进程态跑破坏性工具（`agent/turn_tool_round.py:52-55,116-119`）。
- **文本 stop ≠ 终态**：模型给纯文本答案仍可被 verify-on-stop / pre_verify hook / kanban 守卫否决，把答案降级为 interim + 合成 user-role nudge 续一轮；预算耗尽时保留答案作 fallback（`agent/turn_stop_gates.py:104-139`）。

### workbuddy（@ 03108ac）

- **无 agentic 循环**：WorkerPool 从不执行任务函数（`plugins/task-orchestrator/index.js:65-71`）；MAO 只有一次性 spawn + 状态簿记，不驱动"ready jobs → spawn → 回收 → quality gate → 再分配"（`plugins/multi-agent-orchestrator/index.js:335-411,1501-1584`）。唯一真"循环"是 daemon 文件轮询合成 hook 事件（`engine/daemon.js:93-185`）。
- **不可作为 loop 实现采信**；仅"防死循环用观察注入提醒而非硬上限"这一思想与 dsh 一致，可参考。

## ② 共性取向

1. **循环与"最多 N 步"脱钩或降权**：成熟实现把迭代约束外部化——重试次数归 provider 策略、并发预算归工具、防循环靠观测提醒或进度不变即异常；硬 max-iteration 只是最后防线（hermes），不是主机制。
2. **终止/转向由"响应内容 + 明确的 reason"驱动**：tool_calls 有无、max-tokens、error、blocked、completed 都落成**可观测的原因事件**，错误不静默。
3. **外部故障走"可重试 + 退避 + 策略判定"**，不是立即兜底文案；重试是持久/可恢复状态。
4. **单步副作用有持久化纪律**（persist-before-execute / 可恢复）。
5. 极端防护（死循环、上下文打爆）都走"观测 → 注入/触发恢复"的路径，不改写循环主干。

## ③ 本项目现状与差距

现状：`Agent.run` 是单层 while：step 计数、`step > max_run_steps` 硬 break 给体面兜底（`agent.py:78-91`）；LLM HTTPError → 兜底文案（`agent.py:86-91`）；工具失败 → `{ok:false}` 交还模型（`agent.py:114-133`）；无重试、无等待态、无 overflow 恢复。run 只在日志，同 client_message_id 重发 = **整 run 从 worker 层续跑**（`worker.py:45-66`）而非断点续。

差距排序：
- ① **loop 没有自己的结构**：`Agent.run` 是"裸 while + 内联 if"，LLM 调用/工具分发/压缩/预算/兜底全挤在一层循环体里——护栏只能嵌进循环，读代码才能看出状态怎么流转。这是与"harness loop"最根本的差距（见 R12）。
- ② **LLM/工具故障"一次性兜底"**：与"外部故障可重试 + 记 reason"的成熟取向相反。真实 DeepSeek/Tavily 偶发 429/5xx 时，一次兜底体验差。
- ③ **超步数只有兜底文案**：没有"把当前上下文压缩一下再继续"的路径（pi/dsh 都做 overflow 恢复）。
- ④ 防死循环只有硬上限，无观测提醒（但我们的工具面小、桩测试已覆盖 loop_forever 护栏，硬上限仍有价值）。
- ⑤ 文本产出无质量 gate：空文本 / 极短 / 复读会直接当回复返回（桩与真机都可能），情感陪伴里这类"嗯嗯式废答"不该被当作有效陪伴（见 S1 文本质量 gate）。
- ⑥ run 不可断点恢复（当前无事件溯源，也不打算引入）——这是我们**有意的场景取舍**，不视为缺陷。

## ④ 设计建议

**R8（S0）LLM/工具瞬时故障改为"有限重试 + 明确 reason 事件"，保留最终兜底**
- 保留现有"体面兜底文案"语义（不破人设，`tests/test_e2e.py:280-290` 断言），但在其前插入**轻量重试**：
  - LLM HTTP 层瞬时错误（429/5xx/超时）重试 ≤2 次，指数退避（如 0.5s→1.5s）后再兜底；4xx 非瞬时（401/400）不重试直接兜底。
  - 工具失败当前把 `{ok:false}` 交还模型（`agent.py:118-123`），保留——但要给 `tool_failed` 事件补 reason 分类（瞬时/不可用/输入错），供 04 观测与将来 skill 决策。
- 对齐：dsh retry-policy 白名单码（`retry-policy.ts:14-24`）、pi retry_wait 态。实现仅 agent 内部，**不改表**。注意保持"一次 run 内 LLM 调用总数有界"（重试次数并入 max_run_steps 心智或单列配置）。

**R9（S0）超步数由"纯兜底"升级为"兜底 + 触发一次压缩并重试"（预算内）**
- 现状超预算直接 break（`agent.py:81-84`）。建议：当 `step > max_run_steps` 时，若本 run 尚未压缩过，**先压缩当前上下文（复用 01 的 R1/R2 路径）再给模型最后一次收尾机会**；仍超或压缩过则走现有兜底文案。对齐 pi overflow compaction（`response.ts:188-247`）但**频度节制**（情感对话极少需要多步；仅一次额外收尾）。
- 收益：当工具循环把上下文撑满时，压缩后可自然收尾而不是突然断句。

**R10（S0）把"循环终止原因"显式化**
- run 收尾只有 `log.info("run_finish", steps, tool_count)`（`agent.py:135`）。建议加 `finish_reason` ∈ {completed_text, tool_loop_recovered, llm_outage, step_budget, tool_failure} 与兜底文案成因，落 04 事件。可回答"这次回复为什么是软话"。

**R11（S0）防重复工具调用用"观测提醒"，作为硬上限的补充**
- dsh repeat-tool-reminder（`guard/repeat-tool-reminder/src/index.ts:213-232`）思想可低配移植：在 `agent.py` 工具循环内统计"同一工具+同参"连续命中（检索/搜索同 query 重试 ≥2 次），把"已按该 query 查过、未找到更多，别再重复检索"以 user/工具结果形式提醒模型，避免同 query 反复检索。我们工具只读，无副作用风险，纯省 token + 避免死循环。
- 现有 `max_run_steps` 硬护栏**保留**（它是最后防线，桩测试 `loop_forever` 依赖它：`tests/stub_servers.py:46-47`、`test_run_step_budget_guardrail_graceful`）。

**R12（S0）loop 主干显式阶段化（verdict 管道）——引擎结构轴的核心项**
- **现状问题**：`Agent.run` 是"裸 while + 内联 if 决定下一个动作"（`agent.py:78-133`）——LLM 调用、工具分发、压缩、预算判断、兜底全部挤在一层循环体里，各护栏（R8–R11）与将来的压缩续跑、观测都只能**嵌进循环体**，读代码才能看出状态怎么流转。这正是"看起来不像 harness、而像一段脚本"的根源。
- **建议**：把主循环提成 **"有限阶段 × verdict"** 形态——每阶段是独立函数，返回显式 verdict，一个 `_LoopState` 承载 run 局部状态，阶段间靠 verdict（return / break / continue / 下一阶段）流转：
  - 阶段轮廓（对齐现状，行为不变）：`begin → assemble → generate → route`（据 LLM 结果分 `tool_round` / `retry_wait` / `finish`）→ `tool_round` → 回 `assemble`。
  - 参照：hermes 把大循环拆成 `agent/turn_*.py` 的 phase helpers + `_LoopState`，verdict 只有 return/break/continue（`agent/conversation_loop.py:1252-1259,1484-1504`）；pi 的 `drive.ts:48-105` 也是"按当前状态分发下一步"。**我们只取其 verdict 管道这层 shape，不取事件溯源状态机**（见 L4）。
- **收益**：护栏/压缩/观测/将来新增阶段都变成"挂在某个阶段边界"，而不是改循环体；新增一条路径（如"文本质量 gate"）≈ 加一个 verdict 分支。**这是纯内部重构，行为不变 → 现有 E2E 原样绿**。
- **落地约束**：阶段拆分先按现状语义等价（不改终止/兜底/护栏行为），S1 再往阶段边界上挂压缩续跑（R9）与"文本质量 gate"（见下方 S1 项）。

### S1 —— 上调（规模与可靠性 / 记忆纵深轴）
- **run 级"可恢复重试"事件化**：S0 的 R8 是进程内有限重试；S1 把"一次失败的 LLM 调用该不该重试、退避多久、第几次"落成可观测/可恢复状态（对齐 dsh llm-retry 写 durable `llm/retry` 事件 + sessionProjections 恢复计数，`packages/llm/llm-retry/src/index.ts:59-238`；dsh 重试码白名单 `retry-policy.ts:14-24`）。仍不引入**checkpoint 式 run 断点续跑**（那是 S2），但重试状态本身值得持久化。
- **溢出恢复压缩续跑与 R9 合并成 S1 完整形态**：R9 是"超步数压缩一次"；S1 补上"单步上下文打爆（overflow）也走压缩续跑"（pi `response.ts:188-247`），让 loop 的恢复语义统一为"压一次再收尾"。
- **文本质量 gate（情感陪伴的 stop-gate 轻量版）**：在 R12 的 `route` 阶段加一个否决点——当 LLM 产出空文本 / 极短（如 <2 字）/ 与前一轮雷同 / 重复同句时，不是直接落为回复，而是**再给一次机会**（合成一条引导 nudge 继续，参照 hermes verify-on-stop 降级答案 + 合成 user-role nudge 续一轮的思路，`agent/turn_stop_gates.py:104-139`）；仍低质或已重试过一次才走体面兜底。情感陪伴里"嗯嗯""在的"这类空泛废答不该直接当成有效陪伴，此 gate 是 loop 结构化后最值得挂的第一条业务规则。实现只在 agent 内部、不改表；桩测试需补"空答/复读触发 gate"场景。

### S2 —— 远期（规模与可靠性轴触发）
- **异步 outbox / 多 worker 进程 / 分布式 mailbox / Redis 锁**：当吞吐或进程数上去后。mailbox（`worker/mailbox.py`）与幂等接口（`worker.py:45-66`）现在就已可替换、不阻塞；届时按 000 演进接缝立项。
- **durable run 断点续跑（checkpoint 式，非事件溯源）**：真需要"进程崩溃后从中间步恢复"时，用**周期性落 run 进度 checkpoint + 重试状态**（不是全量事件日志重放）实现——参照 hermes persist-before-execute（`turn_tool_round.py:52-55`）与 dsh `llm/retry` 投影的思路，但状态存 MySQL 不存事件日志。当前"重发=整 run 续跑"的幂等语义（`worker.py:45-66`）仍够用，故仅作 S2。

### L4 —— 领域外（任何阶段都不做）
- **全量事件溯源状态机**（pi/dsh 的 durable-entry drive，`pi runtime/drive.ts:48-105`）：状态留在 MySQL，重放会话不为状态服务。可观测所需的"可重放"由幂等键 + run 事件承担（见 04）。
- persist-before-execute 的回滚语义：现阶段工具只读幂等；若 S2 出现首个可写/可中断工具再上调（见 05，它正是 S2 的 persist 前提）。
- verify-on-stop / stop-gate 续跑（hermes）：情感陪伴无"验收/看板"。
- 并行/并发工具预算：工具面小，见 05 S2。

**验收影响**：`test_run_step_budget_guardrail_graceful`（loop_forever → 兜底不无限）、`test_llm_outage_graceful_and_session_alive`（fail_once → 兜底后会话仍活）、`test_failed_processing_resend_recovers_no_dup`（重发续跑）都是本轮**必须保持**的既有断言（`tests/test_e2e.py:182-195,259-290`）。R8/R9 引入重试时，要确保 `brain.fail_once`（只失败一次）类桩场景仍走兜底成功；真实 key 冒烟验证 429 重试路径。S2 引入多进程/断点续跑前，既有单进程 E2E 必须原样通过（回归基线）。
