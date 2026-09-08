# 数据表结构设计

- MySQL 表结构的唯一事实源；随迭代就地更新、不按版本复制。领域语义以根 `CONTEXT.md` 为准。

当前 6 张表：`users`、`sessions`、`session_messages`、`idol_infos`、`songs`、`idol_lyrics`。偶像侧表按 `doc/asset/` 真实素材定形。

**存储与隔离约定**

- `env`（开发/生产）隔离用独立数据库实例实现，不落表列。
- `run_id` / `step_id` 只进日志；历史中"回复/工具记录 ← 用户消息"用 `origin_message_id` 表达。
- 每条业务写入携带 Scope（user_id/session_id），Repository 强制校验。

---

## users — 用户表

登录凭据为昵称 + 令牌（令牌为持有型凭据），库中只存令牌 SHA-256 哈希。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 用户主键 |
| nickname | VARCHAR(32) | NOT NULL, UNIQUE | 登录标识 + 显示名 |
| token_hash | CHAR(64) | NOT NULL, UNIQUE | 登录令牌的 SHA-256 哈希，登录时按哈希反查 |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=停用 1=正常 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |
| last_login_at | DATETIME | NULL | |

---

## sessions — 会话表

一个用户与偶像之间唯一的对话线，**压缩历史摘要存本表**。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 会话主键（即 scope 的 session_id） |
| user_id | BIGINT UNSIGNED | NOT NULL, UNIQUE, FK→users.id | 唯一键 = 一用户一会话 |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=关闭 1=正常 |
| summary_text | MEDIUMTEXT | NULL | 压缩历史摘要（LLM 重写生成，可空=尚未压缩） |
| summary_tokens | INT | NULL | 摘要估算 token 数，供窗口预算 |
| summary_seq | BIGINT | NULL | 已并入摘要的最大 seq：窗口取 seq > summary_seq 的行 |
| last_active_at | DATETIME | NULL | 最后活跃时间 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

---

## session_messages — 会话历史表

单表 + `type` 区分 user / agent / tool 三种载体；同会话由 Repository 经 mailbox 串行写，`seq` 保证顺序。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 消息主键 |
| session_id | BIGINT UNSIGNED | NOT NULL, FK→sessions.id | 归属会话 |
| seq | BIGINT | NOT NULL | 会话内单调递增序号（从 1），回放/窗口按它取序 |
| type | VARCHAR(16) | NOT NULL | 载体类型：`user` / `agent` / `tool` |
| payload | JSON | NOT NULL | 载体内容，结构见下方 |
| token_count | INT | NOT NULL DEFAULT 0 | 估算 token 数，供窗口/压缩阈值计算 |
| client_message_id | VARCHAR(64) | NULL | 客户端幂等键（仅 user 消息携带） |
| status | VARCHAR(16) | NULL | 消息生命周期，仅 user 消息有值：`received` / `processing` / `replied` |
| origin_message_id | BIGINT UNSIGNED | NULL, FK→session_messages.id | agent/tool 行指向发起它的 user 消息行；user 行本身为 NULL |
| created_at | DATETIME | NOT NULL | |

**索引**

- UNIQUE `(session_id, seq)`
- UNIQUE `(session_id, client_message_id)`
- INDEX `(session_id, origin_message_id)`

**payload 结构（约定）**

- `type=user`：`{ "text": "用户发言" }`
- `type=agent`：`{ "text": "整包回复" }`
- `type=tool`：`{ "name": "工具名", "input": {...}, "output": {...} }`

**生命周期**：user 消息写入时 status=received → worker 加锁处理时置 processing → 回复与工具记录落库后置 replied（与 reply 同批事务）。中途失败时消息停留在可见状态，客户端可凭 `client_message_id` 重发复用。

---

## idol_infos — 偶像信息表（知识条目）

源自 `doc/asset/idolinfo-清洗后.xlsx`（720 行），每行一条知识条目；RAG 检索时一行一个向量。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 规范记录主键 = qdrant point id |
| tag | VARCHAR(32) | NOT NULL | 标签分类（见下方清单） |
| content | MEDIUMTEXT | NOT NULL | 条目正文 |
| event_time | VARCHAR(32) | NULL | 事件时间，自由文本，仅展示不做查询键 |
| content_hash | CHAR(64) | NOT NULL | 正文 SHA-256 |
| vector_synced | TINYINT | NOT NULL DEFAULT 0 | 0=待同步/失败 1=已同步到 qdrant |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=下架 1=上架 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

标签取值（源自素材，非 DB 约束）：`粉丝互动` `言论与观点` `个人特质` `喜好与厌恶` `影视与节目作品` `舞台与演出` `音乐作品` `成长与学习经历` `商业代言与合作` `职业生涯里程碑` `社会活动与公益` `人际关系` `基础信息` `行程`

索引：INDEX `(tag)`

---

## songs — 歌曲表（曲目级）

一首歌一条，承载曲目级元数据与歌曲介绍；是 `idol_lyrics` 的父表。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 曲目主键 |
| song_title | VARCHAR(128) | NOT NULL, UNIQUE | 歌名（含别名）；歌词行归属的稳定键 |
| album | VARCHAR(128) | NULL | 所属专辑（可空=单曲/未披露） |
| release_time | VARCHAR(32) | NULL | 发行时间，自由文本 |
| company | VARCHAR(128) | NULL | 发行公司/厂牌 |
| creators | VARCHAR(255) | NULL | 创作者：作词/作曲/编曲/制作人（素材原文） |
| collaboration | VARCHAR(255) | NULL | 合作/独立发行（素材中可达 200+ 字符，故放宽） |
| intro | MEDIUMTEXT | NULL | 歌曲介绍正文（参与向量化） |
| content_hash | CHAR(64) | NOT NULL | `intro` 正文 SHA-256 |
| vector_synced | TINYINT | NOT NULL DEFAULT 0 | 0=待同步 1=已同步（对应 intro 向量） |
| status | TINYINT | NOT NULL DEFAULT 1 | 0=下架 1=上架 |
| created_at | DATETIME | NOT NULL | |
| updated_at | DATETIME | NOT NULL | |

> 素材歌目在歌词 md 与歌曲介绍 md 间有出入（歌目、标题书写差异），摄入以本表 `song_title` 为唯一归并键做归一；无歌词正文的歌也保留歌曲行。

---

## idol_lyrics — 歌词段表

一首歌的歌词正文，按原文段落（空行分隔）切行，每行一个向量。

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
