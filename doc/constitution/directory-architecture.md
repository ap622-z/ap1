# 目录架构

- 后端目录分层的唯一事实源；只记录到倒数第二层（目录层），不列文件；随迭代就地更新、不按版本复制。

```
ap1/
├─ backend/                        # 后端 Python 包（MVP+ 由 app/ 更名）
│  ├─ config/                      # 全局统一配置（读 env/.env）
│  ├─ api/                         # 接入层：HTTP / 鉴权 / Scope
│  ├─ worker/                      # 编排层：消息生命周期 / 会话串行
│  ├─ agent/                       # 执行层：零状态 Agent 单例
│  │  ├─ tool/                     # 工具：偶像知识检索
│  │  ├─ skill/                    # 行为技能：反问澄清
│  │  └─ mcp/                      # 外部能力：联网搜索
│  ├─ repository/                  # 数据层：持久状态唯一入口
│  └─ cli/                         # 脚本入口：知识摄入
├─ frontend/                       # Vue 前端
│  └─ src/
├─ doc/
│  ├─ asset/                       # 可直接使用的素材
│  ├─ constitution/                # 全局唯一的结构与参数标准
│  └─ design/                      # 各版本：spec / plan / ticket
│     ├─ 000-mvp/
│     └─ 001-mvp-plus/
└─ tests/                          # 开发期端到端测试
```
