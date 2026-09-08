# 05 tool 管理策略 — 调研与设计建议

> 样本与引用规范见 `00-research-and-design-summary.md` §0、§6。摘录均为逐字引用。

## ① 四家样本的真实实现

### pi（@ 3e4bc26）

- **工具 = 声明式对象**：schema(参数，TypeBox) + label + execute + 可选 prepareArguments + replay + per-tool executionMode：
  ```ts
  // packages/agent/src/types.ts:386-412
  export interface AgentTool<...> extends Tool<...> {
      label: string;
      prepareArguments?: (args: unknown) => Static<TParameters>;
      execute: (toolCallId, params, signal?, onUpdate?) => Promise<AgentToolResult<...>>;
      replay?: "never" | "safe";
      executionMode?: ToolExecutionMode;   // "sequential" | "parallel"
  }
  ```
- **schema 兼作模型可见描述**：`tools/read.ts:26-45` 用 TypeBox 建参 schema，description 内联截断上限。工具结果可带 `addedToolNames`（**动态加工具**：某工具执行后可介绍新工具给模型，`types.ts:370-371`）。
- **结果 feedback 语义**：`terminate` hint——仅当一批内**所有**工具结果都 terminate 才提前结束 batch（`types.ts:372-376`）。
- **执行管线**（`execution/tools.ts`）：prepare → `validateToolArguments` → before_tool hook（可 block/replace args）→ 在 effect **gate** 下执行 → after_tool patch → finalize → durable toolResult 消息；工具 **throw 被转成 error 输出而非崩溃**（`tools.ts:125-158`）。
- 工具可用性按请求由 active names → config tools map 解析（`drive/generation.ts:79-99`）。

### dsh（@ c389f96）

- **四条 waterfall 事件定义全工具生命周期**：`tools/pre-execute`(allow/deny/ask)、`tools/execute`(around wrapper：timeout/retry/metrics)、`tools/post-execute`(accept/replace/block + 可挂 additionalContexts)、`tools/result`(最终观测 emit)（`packages/core/tools/src/index.ts:129-200`）。
- **ToolDefinition 强约束输出**：schema + execute 之外**必须**带 `output: ToolOutputDefinition`（canonical JSON 值 schema + 纯函数 render）；可选 finalizeContent、timeoutMs（**永不发给模型**）、isConcurrencySafe、presentCall/presentResult（`.../index.ts:213-280`）。返回必须是"无损 JSON + 声明 schema"，执行与展示分离。
- **模型可见 schema = 白名单投影**：只发 {name, description, parameters}（`.../index.ts:1245-1257,972-993`）；呈现模式 `native`(全量)/`ptc`(只发 run_code + SDK 提示，直调其它名被拒 UNKNOWN_TOOL)（`.../index.ts:51,847-855,1211-1216,1314-1316`）。
- **执行管线**：ordered pre-execute 审批 + monotonic guard → around-dispatch → tool body → post-execute → 冻结物化（`.../index.ts:1453-1550`）。`ToolRunContext` 提供 `deferContext(userMessage)` 与 `concludeTurn()`（`.../index.ts:397-414`）。
- **注册/权限分层**：`register` 进 scoped layers（近 scope 遮蔽全局）；`restrict({allow,deny})` 只过滤 agent scope 继承的全局工具；`guard(reason)` 单调否决（`.../index.ts:1027-1106`）。

### hermes（@ ee84ccd）

- **定义期与执行期分离**：
  - 定义：toolset 选择 → registry.get_definitions（每工具带 check_fn，过不了剔除）→ **动态 schema 重写** → 消毒 → 若可延迟工具面超出窗口份额则 **progressive disclosure / Tool Search**（bridge 工具，核心工具永不被延迟）（`model_tools.py:444-478`）。
  - **schema 描述永远只反映实际可用**：防模型幻觉不存在词汇（`model_tools.py:421-432,349-355`，如 execute_code 只列真实可用沙箱工具）。
- **执行统一 observe → commit → project 管线**，工具结果 wire shape 只产一次（`agent/tool_executor.py:1-7`）；混合批按路径写者冲突切成 parallel/sequential 段（reader↔reader 并行；任何含 writer 的重叠即收束并行 run）（`agent/tool_dispatch_helpers.py:133-143`）。
- **结果副作用分类**：`NO_EFFECT_TOOL_NAMES` 白名单（读类工具安全丢弃被打断结果）；未知/plugin/MCP 默认视为有副作用（`agent/tool_result_classification.py:9-23`）。
- **循环守卫 = 无副作用纯控制器**：观测 per-turn 调用 → 返回决策（警告/synthetic result/受控 halt）；连续相同 (tool,args,result) 达阈值出 notice；**第二个字节相同大结果 → 引用 stub**（工具仍执行，上下文去重省 token）（`agent/tool_guardrails.py:1-6,403-440`）。

### workbuddy（@ 03108ac）

- skill-analyzer 只读盘打分（`plugins/skill-analyzer/index.js:47-101`），不注册给模型；hook-runner 插件动态分派真实（`engine/hook-runner.js:194-244`）；runtime-guardian 对 Bash/Read/Write 有 42 条危险命令/路径黑名单拦截（`plugins/runtime-guardian/index.js:101-157`）。作为工具系统**结构性参考**（拦截层想法），执行层不可采信。

## ② 共性取向

1. **工具 = 声明式对象**（schema + execute + 元数据），schema 是唯一声明源，**模型可见面 = 白名单投影**，杜绝声明与执行不一致。
2. **执行走分层管线**（审批 → 执行 → 结果改写/观测），为权限/超时/防重复留 seam；工具异常**容器化**成 error 结果，不裸抛。
3. **结果带结构化输出 + 分类**（读/写、幂等/易变、有/无副作用），决定并行、去重、安全丢弃策略。
4. **防死循环/重复是"观测注入"**：计数→提醒/synthetic 结果/stub 去重，不改写工具本身。

## ③ 本项目现状与差距

现状：两个可调用工具（`retrieve_idol_info` 检索、`web_search` 联网）+ 一个 skill（提问，非可调用）。每个工具 = **一份静态 JSON schema dict + 一个执行函数成对**（`agent/tool/__init__.py` RETRIEVE_TOOL + retrieve；`agent/mcp/__init__.py` WEB_SEARCH_TOOL + web_search）。`CapabilityRunner.tool_specs` 硬编码列表、`run()` 按 name `if/elif` 硬编码分发（`agent/capabilities.py:28-36`）。工具全部只读、幂等、低风险；模型侧 function-calling 单轮（无并行多调用）。

差距：
- ① **schema 与执行函数是两份手写，靠 name 字符串硬连**：新增工具要同步改 schema dict + 执行函数 + `run()` 分支 + `tool_specs`，四处易漂移。模型看到的与 `run()` 能分发的**不是同一来源**。
- ② **无 schema 校验层**：`tool/__init__.py:30-32`、`mcp/__init__.py:30-32` 只做 `query` 空判断，缺参/类型错靠 Python 层兜；`_runner.run` 抛未知工具时 `CapabilityUnavailableError`（`capabilities.py:36`），但入参错误返回的是执行结果而非"参数不合法"。
- ③ **无执行元数据**：工具结果带 ok/found/items 业务态（`tool/__init__.py:37-40`），但无"耗时、调用次数、结果去重"观测（04 想记账就缺数据）。
- ④ 无"检索同 query 重复"护栏（03 R11 提到）。

## ④ 设计建议

**R17（S0）收敛为"单一声明源 + 白名单投影"**
- 把每个工具定义成一条**声明对象**：`name / description / parameters(schema) / 执行函数 / 分类标签(只读/幂等)`，由一个注册表持有；`CapabilityRunner.tool_specs` = 从注册表投影出模型可见面（name/description/parameters），`run()` = 按 name 查表分发。
- 落点：新增 `backend/agent/registry.py`（或扩展 `capabilities.py`）——把 `RETRIEVE_TOOL`/`WEB_SEARCH_TOOL` 两对"schema+fn"收成两个声明项，`tool_specs` 与 `run()` 都从同一表派生。**消除 `if/elif` 硬编码与四处手写**。这是纯内部重构，不改 schema dict 内容、不改协议、不动前端。
- 对齐：pi `AgentTool` 声明对象（`types.ts:386-412`）、dsh 白名单投影（`tools/src/index.ts:1245-1257`）。**不引入** dsh 的 scope-layered register + waterfall / hermes 的动态 schema 重写 + Tool Search（我们工具面太小，过度）。

**R18（S0）给工具入参加 schema 校验（轻量）**
- 两个工具参数都极简（query 必填 string；retrieve 另有 limit int 默认 3）。建议在注册表声明里带**声明式校验函数或 JSON-schema 片段**，执行前校验；不合法返回 `{ok:false, message:"参数不合法:..."}`（模型会据 message 自我纠正），而非 Python 层 TypeError。保持错误进工具结果、不 abort run（现有语义，`agent.py:118-123`）。
- 对齐 hermes"描述只反映可用、参数可校验"（`tool_guardrails` 思想），但实现克制。

**R19（S0）工具执行元数据 + 重复护栏（联动 03/04）**
- 在 `_runner.run` 外层记录 `tool_call`/`tool_result` 事件时带上：name、耗时（可选）、连续同参计数。连续同 (name, query) 命中 ≥2 时，把"已查过、未找到更多"以 `{ok:true, found:false, notice:...}` 提示模型（对齐 03 R11 与 dsh repeat-tool-reminder `guard/repeat-tool-reminder/src/index.ts:213-232`）。
- 检索/搜索都只读幂等 → 实现无需 gate/回滚/副作用分类，仅"去重 + 提示"。

**R20（S0，接 02）让"skill"与"tool"在声明上分家但同样可枚举**
- 现 skill 走 `question_skill_trigger` 独立注入（`skill/__init__.py:16-38`），不经 CapabilityRunner。建议保持分家（skill=行为指导注入、tool=可调用），但把 skill 的触发判定也放进注册表/清单，便于 02 的分节注入与 04 的 skill 事件统一枚举。**不改 skill 语义与 tag**。

### S1 —— 上调（引擎结构与可扩展性轴，工具面长大的前提）
- **分层注册语义（dsh `register`/`restrict`/`guard` 的轻量版）**：S0 的 R17 是一个平面注册表。当工具从 2 个长大（技能/检索点细分/未来运营工具）时，把注册表升级为带**可见性过滤**（哪些 name 发给本 scope）与**单调 guard**（哪些调用可直接拒绝）的分层形态。对齐 dsh `tools/src/index.ts:1027-1106`。**不做** waterfall 四段与 scope 遮蔽的插件宿主（L4）。
- **pre/post 执行 hook seam**：在 `_runner.run` 外包一层 `before(allow/deny) / after(改写/记录)` 回调位，先在 S0 留空实现、S1 填审批/护栏——这样将来加权限或守卫不必改执行函数。对齐 dsh 四事件（`tools/src/index.ts:129-200`）但只取薄 seam。

### S2 —— 远期（工具面/复杂度触发）
- **并行/并发工具段编排 + writer-conflict barrier**（hermes `tool_dispatch_helpers.py:133-143`）：当工具面 > ~5 且出现可写/慢工具时。
- **progressive disclosure / Tool Search**（hermes `model_tools.py:444-478`）：当工具 schema 体积大到挤占上下文窗口时。
- **persist-before-execute 回滚语义**：出现首个非只读、可中断工具时。
- **addedToolNames 动态加工具**（pi `types.ts:370-371`）：产品出现"工具引入工具"需求时。

### L4 —— 领域外（任何阶段都不做）
- ptc 呈现模式、run_code SDK 直调（dsh）：DeepSeek 走 function-calling，不需要 code 直调。
- deferContext/concludeTurn / 多步复合工具（dsh）：我们的工具单发、只读，无嵌套工具调用（直到 S2 的 persist 语义立项前都不需要）。

**验收影响**：R17-R19 为内部重构 + 增强。现有 E2E 断言工具名 `retrieve_idol_info`/`web_search` 出现在历史 tool 行（`tests/test_e2e.py:149-180`）与检索/搜索桩返回格式（`tests/stub_servers.py:54-65,153-168`）必须保持；`run()` 查表分发不得改变工具返回的 `{ok,found,items}` wire 结构。S1 分层注册不得改变默认全量的可见行为。
