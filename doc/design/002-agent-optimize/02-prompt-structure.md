# 02 提示词结构设计与组装 — 调研与设计建议

> 样本与引用规范见 `00-research-and-design-summary.md` §0、§6。摘录均为逐字引用。

## ① 四家样本的真实实现

### pi（@ 3e4bc26）

- **systemPrompt 是"函数化的上下文产物"，不是常量**：`packages/agent/src/harness/agent-harness.ts:526`
  ```ts
  systemPrompt?: string | ((toolContext: TContext, context: Context) => string | Promise<string>);
  ```
  在真正发请求时才解析：`resolveSystemPrompt` 先解析 toolContext（对象或函数），再调 prompt 函数（`runtime/drive/generation.ts:56-66`）。
- **消息在请求时从会话逐条构建**：`readBoundedContext`（`runtime/transcript.ts:69-84`）→ `buildSessionContext`（`session/context.ts`）；`buildContextEntries` 会把最后一次 compaction 之前的所有 entry **折叠成那一个 compaction 摘要 entry**（`session/context.ts:11-24`）。摘要以独立 `compactionSummary` 角色 + `<summary>` 容器呈现（见 01）。
- **预留请求前统一改写 seam**：`transform_context` hook 可改 messages/systemPrompt，`before_payload` 可改最终 payload（`drive/generation.ts:190-211`）。
- skills 以固定 XML 块 `<available_skills>` 注入 system（`system-prompt.ts:3-25`），skill 可 `disableModelInvocation` 决定是否对模型可见。

### dsh（@ c389f96）

- **提示词 = 注册表装配**：产物模型 `PromptAssembly { sections, contexts, tools, variables }`（`packages/core/system-prompt/src/index.ts:114-119`）。
- **命名 section × 有序数字槽位**：`SECTION_ORDERS` 全局表（identity -1000 … persona prefix 0、plan policy 500、tools SDK 5000、persona suffix 10200…），任意插件可注册进固定槽位（`.../index.ts:121-154`）。渲染 = 插值 `{{variable}}` + 丢弃空段 + 空行拼接（`renderPrompt` `.../index.ts:273-278`）；`interpolate` 对未知变量**严格 throw**（`.../index.ts:319-356`）。
- **scope 链遮蔽**：`assemble()` 按 global+scoped 各层合并（近层遮蔽远层同名 section/variable）→ 收集工具 schema → 排序 → 跑 `system-prompt/assemble` waterfall；waterfall 返回值权威，但存在 `complete: true` 的 section（如 persona 替换）则整体还原为它（`.../index.ts:552-627`）。
- **动态上下文以合成 `user/message` 进历史**而非塞 system：仅当快照文本变化才 project 出 user 消息（source 标记 plugin/form/sections），新快照 supersede 旧快照；消息历史本身由 durable 事件 surface 派生（`agent-loop/src/runtime-context.ts:64-75`、`packages/core/session/src/index.ts:781-820`）。

### hermes（@ ee84ccd）

- **system prompt 显式分三个 cache tier**：`stable`(identity/guidance/coding brief) / `context`(caller system_message/project context) / `volatile`(skills/memory/用户画像/时间戳/环境提示)。volatile 永远在最后以保持最长公共前缀可复用；`_cached_system_prompt` 只在压缩后重建，**会话中段不重渲染**（`agent/system_prompt.py:604-611`）。
- **缓存断点预算**：默认 4 个 `cache_control` breakpoint（system 前缀、system 末尾、最后两条非 system 消息），同享一个 TTL（`agent/prompt_caching.py:1-6`）。
- **请求装配顺序即语义**：缓存断点只在文本全部规范化（空白、工具 JSON canonicalize、孤儿清扫、surrogate 剥离）之后注入，保证同一行字节跨 turn 恒定（`agent/turn_request_assembly.py:106-113`）。应用幂等（先剥旧 marker 再放，防超 4 断点）（`agent/prompt_caching.py:277-282`）。
- **逻辑缓存 scope 与物理 session rotation 解耦**：`resolve_prompt_cache_scope()` 把物理 session_id 映射回压缩血统 root，缓存桶不因压缩 rotation 失效（`agent/prompt_cache_scope.py:1-10`）。

### workbuddy（@ 03108ac）

- 唯一像样的是 multi-agent-orchestrator 的 `buildAgentPrompt`——把角色人设 + 组织上下文 + 专长 + 任务拼成**单段字符串 prompt**（`plugins/multi-agent-orchestrator/index.js:300-330`），发给 Ollama 时无独立 system/messages 数组。context-awareness 的 `enrichPrompt` 只是前缀 `[项目: x]`/深夜后缀（`plugins/context-awareness/index.js:184-204`）。
- 无 memory→prompt 自动注入、无工具 schema 进 prompt 的管线。作为提示词工程**不可采信**。

## ② 共性取向

1. **提示词不是"一块文本"，是"可组装的复合结构"**：要么 section×槽位注册表（dsh），要么 stable/context/volatile 分层（hermes）。核心目的是让不同稳定度的内容各就其位，且**可被后续能力插入/替换而不纠缠**。
2. **按稳定度分层 / 缓存友好**是同一件事的两面：不变的人设在前、易变的内容（记忆/skill/时间戳）押尾，以求跨 turn 前缀稳定（复用 KV cache 或 provider 前缀缓存）。
3. **动态内容进"消息"而非"system"**：系统提示保人格与固定规则，运行时变化（上下文快照/记忆）以合成消息进历史——因为消息可被压缩折叠、system 不能。
4. **预留"发请求前最后改写"的 seam**（transform_context），让压缩/skill/护栏能在最后一刻统一处理 messages+systemPrompt。
5. 家级实现还会做**变量严格校验、重复注入幂等、防缓存断点越界**。

## ③ 本项目现状与差距

现状：`_system_prompt` 用 `"\n\n".join` 拼 PERSONA + 可选摘要段 + 可选 skill 注入（`agent.py:220-233`），纯字符串拼接、无分区、无分层；skill 注入判断在组装里内联（`question_skill_trigger`）。动态上下文（摘要）塞进 system 而非消息。

差距与取舍（按修订后的四轴愿景，见 00 §2）：
- **缓存断点工程（hermes 4-breakpoint + rotation 解耦）归 L4**：system 很短、DeepSeek 侧按前缀自动缓存、无显式 cache_control，做了也是白做。但"缓存友好"的**原则**仍属于引擎结构与成本轴——见下一条。
- 但 **"稳定内容在前、可变内容集中押尾"** 对 DeepSeek 前缀缓存仍有真实收益：persona + 指令若能字节稳定，跨 turn 的公共前缀更长，命中概率更高；且将来每次在末尾追加新块（skill/记忆）不扰动前缀。
- **摘要放 system 的自由文本 vs 放消息**：当前摘要随 system 一起发（`agent.py:225`）。若摘要很长或频繁变，放 system 会让 system 前缀整体不稳定。设计上更接近 dsh/pi 的做法是把"压缩摘要"当作**独立消息角色**（pi `compactionSummary`）置于 system 之后、历史之前。
- skill 注入目前是组装期一次性拼文本，若 skill 增加，需要一个稳定的"skill 块"位置与开关。

## ④ 设计建议

**R5（S0）提示词组装改为"分节 + 分稳定层"，为压缩与 skill 留固定接缝**
- 把 `_system_prompt` 从纯字符串拼接改为**有序节组装**，节类型：
  - 稳定层（字节尽量不变）：`persona`（现 `PERSONA`）+ 偶像通用行为准则（不编造、不破人设等固化条款）。
  - 可变层（放最后）：`summary` 摘要块（若走消息方案则移出 system，见下）、`skills` skill 注入块、`timestamp/其它` 若未来有。
  - 渲染 = 各节渲染后 `\n\n` join + 空节丢弃（对齐 dsh `renderPrompt` `.../index.ts:273-278`）。节与节之间用清晰标记分隔，便于日志/测试断言某节是否注入（沿用现有 `[question-skill]` 这类 tag 的观测思路，`skill/__init__.py:13`）。
- **目的**：① PERSONA 与注入文本不再纠缠，改人设不碰注入逻辑；② 新增 skill/记忆有固定槽位；③ system 前缀稳定，为 DeepSeek 前缀缓存与将来 prompt 观测打底。实现只在 agent 内部，不改表、不改协议。

**R6（S0）压缩摘要从"system 内嵌"改为"system 后、历史前的摘要消息"**
- 现状摘要拼进 system（`agent.py:225`）会随每次压缩变化而破坏 system 前缀。改为在组装 LLM 消息时：`[system: persona+skills] → [assistant-role 或专用摘要行：此前摘要] → [历史...]`。
- DeepSeek chat 无自定义 role，具体形态：把摘要作为**一条 system 后的独立 system 消息**，或合成一条带明确前缀的 user 消息（参考 dsh 动态上下文用合成 user 消息承载快照，`runtime-context.ts:64-75`）。**实现时须验证 DeepSeek 对多条 system / 中间 user 的兼容**（MVP+ 真机冒烟已证明单 system 可行；多段需一次真实 key 冒烟确认）。
- 若验证不兼容，退化为"摘要仍放 system 但置于**最后**且与 persona 分开成节"（保住 persona 前缀稳定）。

**R7（S0）给 `question_skill_trigger` 等注入件一个统一"注入口"**
- 现 skill 注入内联在 `_system_prompt`（`agent.py:227-232`）。建议定义 `def _assemble_system_sections(scope, user_message, rounds, summary) -> list[Section]`，persona/summary/skill 都走它，各节自带 tag 与条件；未来新 skill（如"安慰/共情节奏"）只是新增一个节工厂，不改主函数。这同时为 04 的"系统提示节级日志"打底。

### S1 —— 上调（引擎结构与可扩展性轴）
- **命名节注册表 + 严格插值（dsh section×槽位的轻量版）**：S0 的 R5 分节组装是写死的 `_assemble_system_sections`；当 skill/记忆/动态上下文要持续增加时，把它升级成"有序命名节 + 每节可注册 + 渲染时插值、空节丢弃"（对齐 dsh `system-prompt/src/index.ts:114-154,273-278`）。**不做** scope 遮蔽与 waterfall（那是插件宿主，L4）；只取"注册表 + 顺序 + 插值"这个 shape。
- **动态上下文 as 合成消息**：若 DeepSeek 兼容（R6 待冒烟确认），把"需随轮变化的上下文快照"以合成消息承载、新快照 supersede 旧快照（对齐 dsh `runtime-context.ts:64-75`）——这是记忆纵深下"动态内容不进 system"的正确归宿，S1 落地。

### L4 —— 仍排除（与领域/成本冲突）
- hermes 4 缓存断点 + 缓存血统 scope 解耦（DeepSeek 前缀自动缓存、无显式 cache_control → 收益 < 成本）。
- dsh 全量重放投影（`按文本变化才 project` 那套依赖事件溯源）——我们状态在 MySQL。
- dsh 插件宿主（emit/serial/waterfall + scope 遮蔽）——除非将来真上多插件体系。

**验收影响**：现有 E2E 通过 system 内 `[question-skill]` tag + 人设文案断言触发与不破人设（`tests/stub_servers.py:51,68`；`test_ambiguous_message_triggers_question_skill` 等）。R5/R6/R7 不得移除该 tag 语义或改变 PERSONA 关键句；改动后需本地 E2E 绿 + 一次真实 key 冒烟确认多段消息兼容。S1 注册表化后新增一节不得改既有节语义。
