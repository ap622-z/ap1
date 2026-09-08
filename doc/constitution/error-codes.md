# 错误码

- HTTP 结构化错误与 error_type 清单的唯一事实源；随迭代就地更新、不按版本复制。实现见 `backend/errors.py`。

## 返回结构

非 2xx 的业务错误统一返回：

```json
{
  "error": {
    "error_type": "unauthorized",
    "detail": "昵称或令牌不正确",
    "scope": { "env": "dev", "user_id": 1, "session_id": 1 }
  }
}
```

- `error_type`：错误类型标识，见下表（判断分支用）。
- `detail`：面向调用方的人类可读原因。
- `scope`：可选，仅当该错误关联到某会话/用户时携带（`env` / `user_id` / `session_id`）。
- 其余键为错误类型的附加信息（如参数校验的 `errors` 数组）。

## 类型清单

| error_type | HTTP | 触发场景 | 调用方应对 |
|---|---|---|---|
| `unauthorized` | 401 | 请求无 Bearer 令牌、令牌为空/无效，或昵称+令牌登录不匹配 | 重新注册或凭正确令牌登录 |
| `session_out_of_scope` | 403 | 访问不属于当前用户的会话（数据隔离越界） | 视为程序错误；正常调用不应出现 |
| `idempotency_conflict` | 409 | 同一会话以相同 `client_message_id` 投递了不同内容 | 换新 `client_message_id` 或先取回已有结果 |
| `processing_failed` | 500 | 消息处理中途意外失败 | 凭同一 `client_message_id` 重发（不会重复执行/重复入账） |
| `capability_unavailable` | 503 | 检索/联网等外部能力瞬时不可用 | 一般不会以该响应回给前端：agent 会转成兜底回复继续对话 |
| `not_found` | 404 | （预留） | — |
| `validation_error` | 422 | 请求参数不合法（如缺字段、超长），`errors` 数组给出明细 | 按 `errors` 修正参数后重发 |

## 约定

- 除上表外，未捕获的运行时异常走框架默认 500，不承诺此结构。
- 消息发送类接口的 5xx：重发必须沿用原 `client_message_id`，服务端保证幂等（命中既有行则不重复执行）。
