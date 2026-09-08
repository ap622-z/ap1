# Research notes — workbuddy-harness (zhuang-HE) @ 03108ac

Verified against `_research/workbuddy-harness`. Paths repo-relative.

## 总体定性（重要）
NOT a production end-to-end agent harness. It is an infrastructure/teaching skeleton in Node CommonJS + local JSON persistence (~/.workbuddy) + git CLI. Only real LLM call = Ollama `/api/generate` single-shot in multi-agent-orchestrator (default qwen models). No OpenAI/Anthropic client. Self-rated maturity 35-70% per its own reports. Multiple "looks real but is a stub" spots (verified below).

## 1. Compaction / memory (real algorithms, never bound to a real conversation/token window)
- context-awareness (`plugins/context-awareness/index.js:19-305`): 4-dim scan → context JSON snapshot in ~/.workbuddy; detectChanges only >5min.
- memory-decay (`plugins/memory-decay/index.js:20-581`): exponential decay + importance + fourLayerCompression (slide window → group summary → importance score → token budget) + forgetting/pruning + recall. But compression/summary are string heuristics, no LLM: `_generateAutoSummary` regex keyphrase extraction, returns `[摘要] ...` (`:520-538`).
- memory-git (`plugins/memory-git/index.js:21-340`): git add/commit/rollback of synced store.json.
- memory-graph (`plugins/memory-graph/index.js:12-291`): MD5 dedupe + keyword edges + BFS recall + cluster; simple TF no vectors.
- KEY GAP: `compress` action in HookRunner only logs (`engine/hook-runner.js:175-178`); `sync` action same (`:170-173`). hooks.json triggers context_auto_compress / decay_compress_on_overload but nothing compresses.

## 2. Prompt
- multi-agent-orchestrator `buildAgentPrompt` = string concat role template (architect/reviewer/...) + org + capabilities + task (`plugins/multi-agent-orchestrator/index.js:300-330`), sent as single `prompt` field to Ollama; no system field/messages array.
- context-awareness `enrichPrompt` prefix `[项目: x]` / suffix `(请简洁回复)` (`plugins/context-awareness/index.js:184-204`).
- No memory→prompt injection, no skill/tool schema into prompt.

## 3. Agentic loop
- No tool-call→observe→replan loop. task-orchestrator `_executeWithTimeout` NEVER calls task.fn — returns `{executed:true}` (`plugins/task-orchestrator/index.js:65-71`); no task ever becomes completed.
- multi-agent-orchestrator: spawnAgent = buildAgentPrompt + one ollama.generate + timeout race (`:335-411`); decomposeAndAssign creates queued template subtasks but no auto-dispatch (`:1501-1584`); aggregate = bookkeeping on stored states.
- Real "loop" only = engine/daemon.js:93-185: 2s polling mtime of dirs → synthesize events for hooks (turn_count*5 as context_usage).
- Condition evaluation half-stub: `_evaluateSimpleCondition` returns true for any non-literal-true branch (`engine/hook-runner.js:141-144`).

## 4. Log
- Logger timestamp console+file (`engine/utils.js:84-103`); ExecutionHistory push {timestamp,event,duration} to execution-history.json (`:105-136`); ConfigManager state.json metrics hooksTriggered/alertsRaised/etc (`:7-52`).
- Dashboard HTML: hardcoded DATA + "Load Real Data" fetch state.json (`dashboard/harness-dashboard.html:79-110,112-128`); file:// fetch ineffective.
- eval-runner `_simulateAgentOutput` generates keyword-matching "passing" text then autoScore (simulation not real eval) (`engine/eval-runner.js:265-342`).

## 5. Tools
- skill-analyzer (`plugins/skill-analyzer/index.js`): scan SKILL.md frontmatter, dependency DAG, circular/dead skill detection, usage heatmap — read-disk + score + report, no registration for model.
- plugin dynamic dispatch real: hook-runner requires plugins/<name>/index.js new, cached Map, harness action (`engine/hook-runner.js:194-244`).
- hooks.json = declarative event→plugin wiring (17 hooks) but events externally/manually synthesized.
- runtime-guardian preToolCheck blacklists 42 dangerous command/path patterns for Bash/PowerShell/Read/Write (`plugins/runtime-guardian/index.js:101-157`) — real interception.

## 该仓库真实形态小结
- CommonJS, zero npm deps, state in ~/.workbuddy JSON; teaching/self-eval nature (root .md reports + dashboard self-score constants). Real runnable pieces: decay, four-layer compression, memory graph, git versioning, workerpool skeleton, dangerous-command intercept, Ollama single-shot, file-watch daemon, JSON history. Stubs: compress/sync actions, workerpool never runs fn, eval-runner fake outputs, half condition eval. No agentic loop; no prompt/skill injection pipeline.
