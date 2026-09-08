# Research notes — Hermes Agent (Nous Research) @ ee84ccd

Verified against `_research/hermes-agent`. Paths repo-relative (`agent/*`).

## 1. Compaction / context-window
- Multi-tier "ladder": native (server-side, gpt-5.6 direct OpenAI) → local batch summarizer → optional micro rolling. Native fires margin below local trigger so server gets first shot; local compressor stays armed as fallback. (`agent/native_compaction.py:22-27`, `:100-128`).
- Local batch summarizer (`agent/context_compressor.py:1-2`): cheap aux model summarizes MIDDLE turns while HEAD and TAIL protected; token-budget tail; tool-output pruning first; scaled budgets.
- Micro compaction (`agent/micro_compaction.py:1-6`): rolling per-exchange folding, OFF by default because each pass rewrites prompt prefix and breaks provider prompt cache. Cadence gate every N turns; only absorbs assistant/tool lines, never raw user text (`:203-213`).
- Batch compression thread-safety contract (`agent/conversation_compression.py:1-8`): pooled daemon thread, private deep-copied message snapshot, publish ONLY via admitted CompressionCommitFence (timeout discards in-flight), one pass/session durable lock, sessions concurrent.
- Entry point `agent/conversation_compression.py:3546-3567`; after commit splits SQLite session (rotation / in_place).

## 2. Prompt structure & assembly
- system prompt = three ordered cache tiers stable / context / volatile; volatile last to preserve longest common prefix; build_system_prompt cached `_cached_system_prompt`, rebuilt only after compression (`agent/system_prompt.py:604-611,662`).
- Prompt caching budget: default 4 cache_control breakpoints shared TTL 5m/1h (`agent/prompt_caching.py:1-6`).
- Logical cache scope decoupled from physical session_id rotation: resolve to ROOT of compression lineage so cache bucket survives compaction (`agent/prompt_cache_scope.py:1-10`).
- Request assembly: ORDER IS LOAD-BEARING — cache breakpoints injected only after whitespace normalization / orphan sweep / thinking-only drop / surrogate strip so same row bytes constant across turns (`agent/turn_request_assembly.py:106-113`).
- Cache-control application idempotent (strip old markers first) (`agent/prompt_caching.py:277-282`, `build_prompt_cache_plan` :243-271).

## 3. Agentic loop
- Loop split into pure phase helpers in `agent/turn_*.py`; `_LoopState` sole carrier of loop-local vars; verdict = return/break/continue (`agent/conversation_loop.py:1252-1259`).
- Main loop while over api_call_count < max_iterations AND iteration_budget.remaining > 0 (grace call escapes): begin/prepare/assemble/preflight-gate → announce → API retry loop → normalize → dispatch to tool round or text finish (`:1484-1504`).
- Persist-before-execute invariant: tool-call turn persisted to session DB BEFORE any tool side effect; failed canonical append ends turn, never runs tools from process-only state (`agent/turn_tool_round.py:52-55,116-119`). execute_code refunds iteration budget (`:185-186`).
- IterationBudget thread-safe counter: parent cap 500, subagent cap default 50; execute_code refunded (`agent/iteration_budget.py:25-40`).
- Text stop ≠ terminal: verify-on-stop → pre_verify hook → kanban stop guard can veto; answer demoted to interim + synthetic user-role nudge to continue; answer preserved as fallback if budget exhausts (`agent/turn_stop_gates.py:104-139`).

## 4. Log / observability
- Monitoring = fire-and-forget queue + background dispatcher, single seam. Hot-path invariant: emit() microseconds, no disk/network, never raises; full queue drops OLDEST (newest wins); nothing persisted — egress not store (`agent/monitoring/emitter.py:1-7,40-60`).
- Monitoring events TYPED and content-free: no prompts/messages/tool args/results/history/usage (`agent/monitoring/events.py:1-7`).
- OTLP/HTTP export optional extra, lazy import; headers read at export time never logged (`agent/monitoring/otlp_exporter.py:1-8`).
- Trace: transcript → Claude Code JSONL (user/tool_result mapping), deterministic, zero LLM turns, private default + forced redaction before HF upload (`agent/trace_upload.py:145-165`).
- Trajectory (training side): success/failure appended ShareGPT JSONL (`agent/trajectory.py:23-27`).
- Content-free JSON log lines for compaction telemetry etc. (`agent/micro_compaction.py:313-314,339`).
- Lesson: observation split into 3 non-confusable egress classes: OTel monitoring plane (content-free, redacted, batched), per-line JSON content-free log events (retrievable), content-carrying trajectory/trace files (default private + forced redaction).

## 5. Tool management
- Agent tool face = agent.tools (OpenAI function defs) + valid_tool_names set (`agent/agent_init.py:1064-1070`).
- Definition assembly pipeline: toolset select → registry.get_definitions (check_fn per tool, drop if fail) → dynamic schema rewrite → sanitize → progressive disclosure (Tool Search) when deferrable surface exceeds share of context window; core tools never deferred; LAST step, idempotent (`model_tools.py:444-478`).
- Schema rewrites centralized by tool name; descriptions always reflect actually-available tools (prevent model hallucinating unavailable vocab) (`model_tools.py:421-432,349-355`).
- Execution unified observe → commit → project pipeline, one tool-result wire shape (`agent/tool_executor.py:1-7`).
- Mixed batches cut into parallel/sequential segments; path-writer conflicts are barriers — reader↔reader overlap stays parallel, any writer overlap closes run (`agent/tool_dispatch_helpers.py:133-143`; `tool_executor.py:1712-1736`).
- Result classification by side-effect: NO_EFFECT_TOOL_NAMES whitelist; unknown/plugin/MCP default effect-capable (`agent/tool_result_classification.py:9-23`).
- Loop guardrails = pure side-effect-free controller: per-turn observations → decisions; runtime turns decision into warning / synthetic result / controlled halt. Stall-guard counts identical (tool,args,result); 2nd byte-identical big result → dedupe reference stub (tool still executes, context deduped) (`agent/tool_guardrails.py:1-6,403-440`).

## Design posture summary
- Compression is layered and always traded against provider prompt-cache prefix stability.
- Concurrent-write correctness via lease + commit fence + durable lock; half-done work discarded.
- Persist-before-execute is a non-negotiable durability invariant.
- Prompt treated as cacheable-prefix engineering (stable/context/volatile tiers).
- Loop = strongly-typed phase pipeline with _LoopState; text-stop ≠ terminal (stop gates veto + synthetic nudge).
- Observability strictly separated from content (content-free monitoring vs content-carrying private redacted trajectory).
- Tools explicitly classified read/write, idempotent/volatile, effect/no-effect; parallelism only in writer-conflict-free segments.
