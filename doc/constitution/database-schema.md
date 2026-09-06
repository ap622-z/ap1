# 数据表结构设计

偶像 Agent 陪伴系统（MVP）的 MySQL 表结构。设计原则见文末；领域语义以根 `CONTEXT.md` 为准，本文件是表结构的唯一事实源，随迭代就地更新、不按版本复制。

当前共 6 张表：`users`、`sessions`、`session_messages`、`idol_infos`、`songs`、`idol_lyrics`。偶像侧表按 `doc/asset/` 下真实素材定形。

**存储与隔离约定**

- `env`（开发/生产）隔离通过**独立的数据库实例**实现，不落任何表的列。
- `run_id` / `step_id` **只进日志**，不落库；历史中"回复/工具记录 ← 发起它的用户消息"用 `origin_message_id` 表达，日志侧用该 id 关联 run/step 全链路。
- 每条业务写入都携带 Scope（身份层 user_id/session_id），Repository 强制校验。

---

## users — 用户表

登录凭据为 **昵称 + 令牌**：注册时生成唯一昵称与一个登录令牌（令牌作为持有型凭据），库中只存令牌哈希。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 用户主键 |
| nickname | VARCHAR(32) | NOT NULL, UNIQUE | 登录标识 + 显示名 |
| token_hash | CHAR(64) | NOT NULL, UNIQUE | 登录令牌的 SHA-256 哈希，登录时按哈希反查 |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=停用 1=正常 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |
| last_login_at | DATETIME | NULL | |

约束：一用户一条 `sessions` 由 `sessions.user_id` 唯一键保证。

---

## sessions — 会话表

一个用户与偶像之间唯一的对话线。**压缩历史摘要存本表**。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 会话主键（即 scope 的 session_id） |
| user_id | BIGINT UNSIGNED | NOT NULL, UNIQUE, FK→users.id | **唯一键 = 一用户一会话**，结构上杜绝第二条会话 |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=关闭 1=正常 |
| summary_text | MEDIUMTEXT | NULL | 压缩历史摘要（由 LLM 重写生成，可空=尚未压缩） |
| summary_tokens | INT | NULL | 摘要估算 token 数，供窗口预算 |
| summary_seq | BIGINT | NULL | 已并入摘要的最大 seq：seq <= summary_seq 的内容在摘要中，窗口取 seq > summary_seq 的行 |
| last_active_at | DATETIME | NULL | 最后活跃时间 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

---

## session_messages — 会话历史表

**单表 + `type` 区分**三种载体：用户消息、agent 回复、工具记录。同会话消息由 Repository 经 mailbox 串行写入，`seq` 保证顺序。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 消息主键 |
| session_id | BIGINT UNSIGNED | NOT NULL, FK→sessions.id | 归属会话 |
| seq | BIGINT | NOT NULL | 会话内单调递增序号（从 1），回放/窗口按它取序 |
| type | VARCHAR(16) | NOT NULL | 载体类型，字符串名：`user` / `agent` / `tool` |
| payload | JSON | NOT NULL | 载体内容，结构见下方 |
| token_count | INT | NOT NULL DEFAULT 0 | 估算 token 数，供上下文窗口/压缩阈值计算 |
| client_message_id | VARCHAR(64) | NULL | 客户端幂等键（仅 user 消息携带，其它行 NULL） |
| status | VARCHAR(16) | NULL | 消息生命周期，仅 user 消息有值：`received` / `processing` / `replied` |
| origin_message_id | BIGINT UNSIGNED | NULL, FK→session_messages.id | agent/tool 行指向发起它的 user 消息行；user 行本身为 NULL |
| created_at | DATETIME | NOT NULL | |

**索引**

- UNIQUE `(session_id, seq)` — 会话内顺序唯一
- UNIQUE `(session_id, client_message_id)` — 幂等：同会话重发同键命中已有行（NULL 不参与去重）
- INDEX `(session_id, origin_message_id)` — 取某条用户消息的整组回复/工具记录

**payload 结构（约定）**

- `type=user`：`{ "text": "用户发言" }`
- `type=agent`：`{ "text": "整包回复" }`
- `type=tool`：`{ "name": "工具名", "input": {...}, "output": {...} }` — 内容结构由各工具自定

**生命周期**：user 消息写入时 status=received → worker 加锁处理时置 processing → 回复与工具记录落库后置 replied（与 reply 同批事务）。中途失败时消息停留在可见状态，客户端可凭 `client_message_id` 重发复用。

---

## idol_infos — 偶像信息表（知识条目）

源自 `doc/asset/idolinfo-清洗后.xlsx`（720 行）。每行是一条**知识条目**：一条事实/一段话 + 标签分类 + 可选时间。RAG 检索时一行一个向量。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 规范记录主键 = qdrant point id |
| tag | VARCHAR(32) | NOT NULL | 标签分类（见下方清单） |
| content | MEDIUMTEXT | NOT NULL | 条目正文（一条事实/一段话） |
| event_time | VARCHAR(32) | NULL | 事件时间，**自由文本**（源数据年份/日期混存且大量缺失，仅展示用，不做查询键） |
| content_hash | CHAR(64) | NOT NULL | 正文 SHA-256 |
| vector_synced | TINYINT | NOT NULL DEFAULT 0 | 0=待同步/失败 1=已同步到 qdrant |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=下架 1=上架 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

标签取值（源自素材，非 DB 约束）：`粉丝互动` `言论与观点` `个人特质` `喜好与厌恶` `影视与节目作品` `舞台与演出` `音乐作品` `成长与学习经历` `商业代言与合作` `职业生涯里程碑` `社会活动与公益` `人际关系` `基础信息` `行程`

索引：INDEX `(tag)`

---

## songs — 歌曲表（曲目级）

一首歌一条。承载**曲目级元数据**与**歌曲介绍**（源自 `doc/asset/歌曲介绍.md`，逐曲风格/主题/情绪分析）。它是 `idol_lyrics` 的父表。歌曲介绍作为可检索文本参与向量化。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 曲目主键 |
| song_title | VARCHAR(128) | NOT NULL, UNIQUE | 歌名（含别名，如 `Love In The Mirror | 理想画`）；是歌词行归属的稳定键 |
| album | VARCHAR(128) | NULL | 所属专辑（可空=单曲/未披露） |
| release_time | VARCHAR(32) | NULL | 发行时间，自由文本（如 `2016年10月26日`） |
| company | VARCHAR(128) | NULL | 发行公司/厂牌 |
| creators | VARCHAR(255) | NULL | 创作者：作词/作曲/编曲/制作人（素材原文） |
| collaboration | VARCHAR(255) | NULL | 合作/独立发行（素材中可达 200+ 字符，故放宽） |
| intro | MEDIUMTEXT | NULL | 歌曲介绍（风格/主题/情绪分析正文） |
| content_hash | CHAR(64) | NOT NULL | `intro` 正文 SHA-256 |
| vector_synced | TINYINT | NOT NULL DEFAULT 0 | 0=待同步 1=已同步（对应 intro 向量） |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=下架 1=上架 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

> 注：素材歌目在歌词 md（40 首）与歌曲介绍 md（41 首）间存在差异（如歌词 md 缺《世界准时进入浪漫时分》），且标题书写有出入（如 `隔壁泰山(Live|Remix)` 空格）。**摄入时以本表 `song_title` 为唯一归并键做归一**，无歌词正文的歌也保留歌曲行（歌词段表为空）。

---

## idol_lyrics — 歌词段表

一首歌的歌词正文，**一首多行**、按原文段落（空行分隔，≈ 主歌/副歌/桥）切成行。每段一行、一个向量，行级召回。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 歌词段主键 = qdrant point id |
| song_id | BIGINT UNSIGNED | NOT NULL, FK→songs.id | 所属歌曲 |
| seg_no | INT | NOT NULL | 曲内段落顺序（从 1） |
| content | MEDIUMTEXT | NOT NULL | 该段歌词文本 |
| content_hash | CHAR(64) | NOT NULL | 段正文 SHA-256 |
| vector_synced | TINYINT | NOT NULL DEFAULT 0 | 0=待同步 1=已同步 |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=下架 1=上架 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

索引：UNIQUE `(song_id, seg_no)`

---

## 关系总览

```
users 1 ─── 1 sessions（user_id 唯一）
sessions 1 ─── N session_messages（seq 有序）
session_messages 自关联 origin_message_id（agent/tool → user）

songs 1 ─── N idol_lyrics（song_id 分组，seg_no 有序）
idol_infos：独立知识条目（无父表）
（偶像侧三表各自与 qdrant 同 id 关联，MySQL 内不建外键）
```

## 设计决策依据

- **单表 + type（字符串名）**：三载体读序一致、窗口重建（Summary + 最近 N 行）一次有序查询完成。`type` 与 `status` 用字符串名而非数字码，库内可读、免查表。
- **run/step 只在日志**：trace 归日志，DB 不背执行细节；`origin_message_id` 已足够表达业务关联与幂等回查。
- **一用户一会话用唯一键硬约束**：靠结构而非代码保证，对应 MVP"系统不存在开第二条会话的途径"。
- **摘要存 sessions 列而非独立表**：1:1、MVP 无摘要版本审计需求，一列够用。
- **幂等键唯一 `(session_id, client_message_id)`**：重发同键返回已有结果，不重复执行 agent。
- **`token_count` 落库**：压缩阈值按总 token 计，避免每次重算；由写入方估算。
- **idol_infos 一行一事实**：直接对应 xlsx 条目，标签仅作检索过滤/展示，时间作自由文本展示、不做结构化。
- **songs 与 idol_lyrics 分离**：曲目级元数据是 1:1 于歌曲，歌词正文是多段 1:N；合一表会造成段行重复承载元数据。`song_title` 是歌词 md 与歌曲介绍 md 摄入归并的稳定键。
- **一首多行按段切**：段（主歌/副歌）语义完整、向量质量高于单句，召回粒度足以命中"接歌词"式提问。
- **知识表带 `content_hash` / `vector_synced`**：知识摄入的双写一致性在表内可对账（同 spec 的低代价协议）；`status=0` 下架即删向量、检索不再召回。
