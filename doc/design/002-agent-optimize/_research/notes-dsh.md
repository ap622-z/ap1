# Research notes — DeepSeek Harness (dsh) — deepseek-ai/deepseek-harness @ c389f96

All claims verified against real source under `_research/deepseek-harness`. Paths repo-relative.

## 1. Compaction / context-window
- Compaction modeled as optional pluggable engine behind `abstract class CompactionEngine`; automatic triggers only two: `'pressure' | 'context-overflow'` (`packages/compaction/compaction/src/index.ts:25`, service + events).
- Pressure detection is an event listener on agent loop seam `agent/pre-step`, NOT internal polling; failure → warn + continue (`packages/compaction/compaction-basic/src/index.ts:148-166`).
- Context-overflow recovery links to `agent/request-error`: compact + return `{kind:'retry'}` only when `failure.code === CONTEXT_WINDOW_EXCEEDED_CODE` and retries < maxOverflowRetries (`.../index.ts:180-224`).
- Token budget formula per model contextWindow: threshold = `contextWindow * 0.8`; retain tail = `contextWindow * 0.16` default; overridable (`packages/compaction/compaction-basic/src/config.ts:20-23,144-147`).
- Compact range head-anchored but keeps verbatim recent tail; cut never splits tool call/result pair — walks back until tool-pairing balanced (`packages/compaction/compaction-basic/src/region.ts:100-135`).
- Pressure measured by deterministic replay of the durable session log (`packages/llm/token-meter/src/index.ts:124-152`), consistent with event sourcing.
- Compaction recorded as durable log events `compaction/start|summary|end|prune` (log-only); the actual surface replacement is carried by an immediately-following `user/message` event that shadows the compacted range (`packages/compaction/compaction/src/types.ts:17-38`; summarizer appends summary instruction as last user message, wraps in `<compacted-summary>`).
- Summary LLM call reuses prefix `...input.system/system/tools/messages` to preserve KV cache (`compaction-basic/src/summarizer.ts:31,121-195`).

## 2. Prompt structure & assembly
- Assembly product model = named sections + contexts + tools + variables (`packages/core/system-prompt/src/index.ts:114-119`).
- Global ordered slot table `SECTION_ORDERS` (identity -1000 … persona prefix 0, plan policy 500, tools SDK 5000, persona suffix 10200) — plugins can register into fixed slots (`.../index.ts:121-154`).
- Render = interpolate strict `{{variable}}` (unknown/undefined throws), drop empty sections, join blank line (`.../index.ts:273-278,319-356`).
- `assemble()`: scope chain global+scoped merge (near shadow far) → collect tool schemas → sort sections → waterfall `system-prompt/assemble`; a `complete` section (persona replacement) wins overall (`.../index.ts:552-627`).
- Loop calls assemble per step → renderPrompt → buildRequest (`packages/core/agent-loop/src/agent.ts:236-254`).
- Dynamic context enters history as synthetic `user/message` snapshot when text changes; latest supersedes older; history is derived from durable events (`agent-loop/src/runtime-context.ts:64-75`, `packages/core/session/src/index.ts:781-820`).

## 3. Agentic loop
- `ReactLoopAgent` while(true) driven by turn + inbox; no built-in max-turn/step counter — budgets injected externally (`packages/core/agent-loop/src/agent.ts:257-341`, `:424-475`).
- Terminations: error/aborted → `agent/request-error` waterfall (plugin decides retry or throw); max-tokens sticky; no tool-calls → completed; tool-calls → executeToolCalls → concluded ? completed : continue.
- Step ends appended as durable events (`step/start`,`step/end`,`turn/end(reason)`).
- Concurrency budget rather than N-steps: `DEFAULT_MAX_PARALLEL_TOOL_CALLS=10`, tools classified by `isConcurrencySafe`, exclusive as barrier, parallel in bounded pool, abort writes synthetic result for not-yet-started calls (`agent-loop/src/constants.ts:6`, `tool-calls.ts:199-214,249-260`).
- Retry owned by provider config: default maxRetries 5, initial 500ms, max 10s, jitter 0.1, retryable codes [EMPTY_RESPONSE,RATE_LIMIT,SERVER,TIMEOUT,TRANSPORT] (`packages/llm/llm/src/retry-policy.ts:14-24,149-195`).
- Retry executor on `agent/request-error`, writes durable `llm/retry` before cancellable delay, `llm/retry-started` after; recovery via sessionProjections key `llmRetry`; exponential backoff × symmetric jitter, respects Retry-After (`packages/llm/llm-retry/src/index.ts:59-64,188-238`).
- Infinite-loop guard is observation + inject-reminder, never veto: repeat-tool-reminder counts same name+canonical args on `tools/post-execute`, thresholds [3,5,8], text into additionalContexts as next user message (`packages/guard/repeat-tool-reminder/src/index.ts:213-232`). Timeout via tools/execute wrapper + declared timeoutMs → structured TOOL_TIMEOUT result (`packages/guard/timeout-policy/src/index.ts:55-81`).

## 4. Log / observability
- Event-sourced log is source of truth: append-only merge-extensible `SessionEventMap` (turn/step/user/assistant/tool/request/context); "message history is derived from this log. Every event is lossless JSON" (`packages/core/session/src/types.ts:260-358`). Package modules augment event vocabulary.
- Append = persist first, then broadcast synchronously on `session/event` firehose, contained observers (`packages/core/session/src/index.ts:700-728`).
- Live agent event bus: `emit`(fire-and-forget, per-listener fault isolation) / `serial`(ordered can bail) / `waterfall`(around-middleware can rewrite); all scoped to agent (`packages/core/agent/src/dispatch.ts:54-149`).
- Telemetry capture abstraction: `SessionTelemetryRecord {channel:'ledger'|'ops', time, severity, attributes, body}`; sink emit must non-blocking enqueue (`packages/session/session-telemetry/src/index.ts:64-132`).
- Live capture subscribes `session/created|event|disposed` + `agent/error` relay (`.../coordinator.ts:92-124`).
- Concrete backend maps records to OpenTelemetry Logs (service.name/version + anonymous user.id), BatchLogRecordProcessor + OTLP exporter (`packages/session/session-telemetry-otel/src/index.ts:204-234`). Also DeepSeek session-log upload of durable increments with acceptance watermark recovery.

## 5. Tool management
- Tool calls = four scoped waterfall events: `tools/pre-execute` (allow/deny/ask approval), `tools/execute` (around wrapper: timeout/retry/metrics), `tools/post-execute` (accept/replace/block + additionalContexts), `tools/result` (final observe emit) (`packages/core/tools/src/index.ts:129-200`).
- `ToolDefinition extends ToolSchema`: schema + execute + REQUIRED `output: ToolOutputDefinition` (canonical JSON value schema + pure render fn), optional finalizeContent, timeoutMs (never sent to model), isConcurrencySafe, presentCall/presentResult (`.../index.ts:213-280`).
- Model-visible schema = whitelist projection {name,description,parameters} only; presentation modes native/ptc — ptc exposes only run_code, direct call of others rejected UNKNOWN_TOOL (`.../index.ts:1245-1257,972-993,51,847-855`).
- Execution pipeline: ordered pre-execute approval + monotonic guard → around-dispatch → tool body → post-execute → frozen materialization (`.../index.ts:1453-1550`). ToolRunContext provides deferContext(userMessage) + concludeTurn() (`.../index.ts:397-414`).
- Registration/rights: `register` scoped layers; `restrict({allow,deny})` filters inherited tools per agent scope; `guard` = monotonic denial (`.../index.ts:1027-1052,1061-1106`).
- Loop scheduling: model order, executionMode → parallel/exclusive groups, prepare/dispatch/finalize phases, durable `tool/call`/`tool/result` events, additionalContexts → next-step inbox (`agent-loop/src/tool-calls.ts:60-102`).

## Design posture summary
1. Everything is events; log is truth; messages/token-meter/compaction-range/telemetry/retry derived by deterministic replay/projection of one durable log.
2. Core loop deliberately zero-state, zero hard caps; budgets all external plugin policy (retry, concurrency, maxTokens, compaction, timeouts).
3. Cross-cutting concerns open via named-event + 3 dispatch semantics; plugins listen on seams, don't edit loop body.
4. Prompt = registry assembly: named sections × ordered slots × scope shadowing × strict interpolation × complete-section override.
5. Compaction = durable shadow transaction; old spans not deleted, shadowed by checkpoint user/message; never splits tool call/result; reuses prefix for KV cache.
6. Tool system heavily layered: registry/scoped layers, whitelist schema projection, presentation modes, four-segment waterfall pipeline, monotonic guard, scope visibility; output = canonical JSON + declared schema.
7. Durable & restart-recoverable state everywhere; failure surfaces as structured errors not silent degradation.
8. Errors/cancellation pervasive: AbortSignal threaded agent→tool/compaction/retry; aborted tools get synthetic results; tool exceptions containerized to isError results.
