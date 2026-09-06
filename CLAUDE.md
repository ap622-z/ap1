# CLAUDE.md

面向粉丝的"偶像 Agent"情感陪伴项目（多用户数据隔离 + 单例零状态 Agent）。

新建或改动文档前，先按下面这些定位该放哪。

## 文档归属

- `CONTEXT.md`（仓库根）
  - 领域词汇表：Scope / Repository / Worker / Mailbox / Outbox 等统一语言
  - 纯词汇、零实现细节，随时就地更新
  - 术语的唯一事实源，其余文档只引用不重复定义

- `doc/constitution/`
  - 全局唯一的参数/结构标准：数据表结构、参数大小取值、重要架构原则
  - 每个主题始终一份，随开发迭代就地更新，绝不按版本复制

- `doc/design/`
  - 存放每一版本开发的需求规格、技术设计与开发票据：`spec-*.md`、`plan-*.md`、`ticket`
  - 每一版本单开一个目录，序号递增，如 `000-mvp`

- `doc/asset/`
  - 开发过程中可直接使用的素材文件，与业务逻辑无关


## 技术约束

- 环境管理：使用 uv
- 后端语言：Python
- 前端：JavaScript / Vue
- 部署方式：所有中间件依赖（qdrant / mysql / redis 等）与最终项目打包均使用 Docker

