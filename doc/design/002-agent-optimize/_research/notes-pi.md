# Research notes — pi (earendil-works/pi)

Cloned at `_research/pi`. Agent harness core = `packages/agent/src/harness/`. Paths below are repo-relative from repo root, file:line.

## 1. 会话压缩 / compaction

Trigger = threshold-based, `shouldCompact`:
- `agent/src/harness/compaction/compaction.ts:246-249` — `contextTokens > contextWindow - reserveTokens`.
- Settings `agent/src/harness/compaction/compaction.ts:147-161`: `enabled`, `reserveTokens:16384`, `keepRecentTokens:20000`.
- Token accounting from real provider usage when available, else char heuristic:
  - `estimateContextTokens` `compaction.ts:214-243` — uses last assistant usage block + trailing estimate.
  - `estimateTokens` `compaction.ts:270-310` — chars/4 conservative heuristic; image≈4800 chars; toolCall args serialized; bash command+output; summary length.
- Cut point keeps a recent-token budget, never splits inside a tool batch, prefers turn boundaries:
  - `findCutPoint` `compaction.ts:370-418`; `findValidCutPoints` `compaction.ts:311-340` (toolResult not a valid cut); turn-start back-off `findTurnStartIndex` `compaction.ts:343-357`.
- Summaries are produced by the LLM with a dedicated system prompt + strict structured format:
  - `SUMMARIZATION_SYSTEM_PROMPT` `compaction.ts:420-422` — "Do NOT continue the conversation… ONLY output the structured summary."
  - `SUMMARIZATION_PROMPT` `compaction.ts:424-455` — structured sections: Goal / Constraints & Preferences / Progress(Done,In Progress,Blocked) / Key Decisions / Next Steps / Critical Context.
  - Incremental merge: `UPDATE_SUMMARIZATION_PROMPT` `compaction.ts:457-494` — takes `<previous-summary>` + new messages, PRESERVE+ADD+UPDATE; first time uses fresh prompt.
- Summary generation is a standalone request: `createSummaryRequestOptions` `compaction.ts:117-125` (`cacheRetention:"none"`, fresh sessionId), retried via `retryAssistantCall` (`completeSimpleWithRetries` `compaction.ts:127-144`).
- Compaction stored as durable session entry carrying summary + tokensBefore + retainedTail + details:
  - `CompactionEntry` / `prepareCompaction` `compaction.ts:634-707` — walks back to previous compaction, re-materializes its retainedTail, finds cut, splits summarized vs turn-prefix vs retained tail.
  - split-turn handling: turn prefix summarized separately `TURN_PREFIX_SUMMARIZATION_PROMPT` `compaction.ts:709-722`, then concatenated `compact()` `compaction.ts:753-816`.
- Injected back into context as a distinct message role `compactionSummary` wrapped in `<summary>`:
  - `messages.ts:9-13,26-31` — role compactionSummary + prefix/suffix; `session/context.ts:37-38` maps entry → compactionSummary message.
- Compaction reasons: `manual | threshold | overflow` (`drive/structural.ts:138-142`); overflow compaction is the recovery when a reply exceeds context window mid-run (`drive/response.ts:188-194, 223-247`).
- Decision happens at the durable checkpoint (`drive/checkpoint.ts:100,124-159` — on threshold, transitions to `summary.deciding`).
- Branch summarization (separate concern, for tree navigation): `compaction/branch-summarization.ts` whole file; shares SUMMARIZATION_SYSTEM_PROMPT.

## 2. 提示词结构与组装

- systemPrompt is either a string or a *function of toolContext* (assembled lazily per request):
  - `agent-harness.ts:526` — `systemPrompt?: string | ((toolContext, context) => string|Promise<string>)`.
  - resolved in `drive/generation.ts:56-66` `resolveSystemPrompt` — resolves toolContext (object or fn) then calls prompt fn.
- Messages read at request time from the session transcript, bounded by last compaction: `readBoundedContext` `runtime/transcript.ts:69-84` → `buildSessionContext` `session/context.ts` → `buildContextEntries` collapses everything before last compaction into the single compaction entry (`session/context.ts:11-24`).
- Per-request hooks can transform messages/systemPrompt before payload: `transform_context` hook `drive/generation.ts:190-201`; `before_payload` hook `drive/generation.ts:203-211`.
- Skills are injected into system prompt as a bounded XML block `<available_skills>`: `system-prompt.ts:3-25`; skill def = name/description/location, gated by `disableModelInvocation`.
- Structural/summary text is emitted via content-block helper messages with stable prefixes (`<summary>` etc.) — see messages.ts.

## 3. Loop 循环策略

- No naive `while`; the harness is an **event-sourced durable state machine**: each run is an append-only session of `Entry` (message / compaction / branch_summary…). The "drive" loop dispatches on the current durable operation state:
  - `runtime/drive.ts:48-105` — `for(;;)` switch over `state.at`: starting / checkpoint / assistant.ready / assistant.effect_pending / tools / deferred… / summary.deciding / summary.ready / navigation…
- Lifecycle states: starting → checkpoint (before_run hook) → assistant.ready (LLM call) → response publish decides next state: tool calls ⇒ `tools`; text ⇒ checkpoint `may_finish`; overflow ⇒ `summary.deciding` (compact then continue); error/retry ⇒ `assistant.retry_wait`; aborted ⇒ checkpoint may_finish.
  - `drive/response.ts:292-318` — tool_calls present ⇒ settle to `tools`; else text (stop not toolUse) ⇒ `checkpoint` with `may_finish`.
- Retry policy global: `DEFAULT_RETRY_POLICY {enabled,maxRetries:3,baseDelayMs:1000}` (`config.ts:4`); normalized maxAttempts = maxRetries+1 (`drive/boundary.ts:31-40`); durable retry wait between attempts (`drive/generation.ts:234-282`).
- Overflow (context exceeded / output length) mid-run triggers an automatic compaction + resume with `overflowRecoveryUsed:true` (`drive/response.ts:188-247`).
- Steering/follow-up in-flight items are gated by `steeringMode/followUpMode` (`lane.ts:145-173`).
- Termination "graceful degradation" is via operation settle with may_finish; terminal.ts holds outcome records; no silent loops — drive throws if a procedure makes no progress (`drive.ts:101-104`).
- **No hard global step counter** seen; loop is state/progress-driven; "length" overflow handled by compacting and resuming. (Contrast: our max_run_steps hard stop.)

## 4. Log / 观测策略

- Layered: (a) passive typed **events** `HarnessEvent` bus (`events.ts`, `agent-harness.ts:...`) — run_start/end, turn_start/end, message_*/tool_*/entry_added, compaction_start/end, usage, fault, retry_* — isolated handler failures; (b) typed **telemetry spans** (schema-first) in `telemetry.ts`.
- Schema-driven span vocabulary with required attributes & low/high cardinality declared up front:
  - `AI_TELEMETRY_SCHEMA` `telemetry.ts:42-118` — `pi.ai.request` span (operation/provider/model/api/streaming + response/usage/cost/error attributes).
  - `HARNESS_TELEMETRY_SCHEMA` `telemetry.ts:233-592` — spans `pi.harness.run / compaction / navigation / checkpoint / turn / step / tool / hook / sleep / event_handler / session.write`.
  - typed start/end attributes + status ("ok" default; errorWhen rule) — validated at compile time (`ExactTelemetryAttributes`).
- Telemetry context threaded through chord Context (not global); no-op default (`context.ts:30-32`; `NOOP_TELEMETRY_CONTEXT`).
- Events also carry usage rows + per-tool execution updates (start/update/end with isError).

## 5. Tool 管理策略

- Tool = TS object `AgentHarnessTool` with **schema-first parameters** (TypeBox `Type.Object`) + typed execute + optional prepareArguments + replay policy + per-tool executionMode:
  - `types.ts:386-412` — label, prepareArguments, execute(toolCallId, params, signal, onUpdate), replay ("never"|"safe"), executionMode ("sequential"|"parallel").
- Schema doubles as the model-facing description: e.g. `tools/read.ts:26-45` builds `readSchema` from TypeBox and sets `description` with truncation/limits inline; tool result may carry `addedToolNames` (dynamic tool introduction mid-run, `types.ts:362-376`).
- Parallel-vs-sequential tool execution mode and result feedback with `terminate` hint (only ends batch when ALL tool results in the batch terminate, `types.ts:372-376`).
- Execution flow in `execution/tools.ts`: prepare → validate args (`validateToolArguments`) → before_tool hook (can block/replace args) → execute under the effect **gate** → after_tool patch → finalize → durable toolResult message. Tool *throws* are converted to error output, not crashes (`execution/tools.ts:125-158`).
- Tool availability resolved per request by active names → config tools map (`drive/generation.ts:79-99`); missing ⇒ typed configuration_failure.
- Tool result object supports structured `details` for logs/UI separate from model-facing `content` (`types.ts:362-376`).

## Notes vs our system (preliminary)
- We share: threshold compaction w/ keep-recent turns; summary persisted as text + boundary; char/token heuristic fallback; "do not answer, only summarize" style instruction; structured summary headings; tool results inline in transcript; per-message token estimate persisted (token_count).
- Gaps vs pi: summary as *dedicated role + stable `<summary>` container* vs our free-form f-string append; we drop tool transcripts across compaction boundary while pi deliberately *carries file ops + retained tail*; we don't record compaction tokensBefore/usage; no event bus / typed span schema (our log is free-form key=value per event); tools are static dicts not schema-typed with per-tool replay/parallel mode; loop is a simple while with hard step cap and graceful text bail-out, pi has overflow-compaction recovery and retry-wait states.
