# MCP 工具

传输是 MCP Streamable HTTP，地址形如 `http://<中心>:8731/mcp`。本服务用官方 Python SDK `mcp` 2.3.0 的 `json_response` 和 `stateless_http`。身份在每个请求的 HTTP 头里，不在进程内存里。

除 `hello_register` 以外，都要：

```http
Authorization: Bearer <登记时返回的凭证>
```

`hello_register` 要：

```http
X-Hello-Register-Secret: <HELLO_REGISTER_SECRET>
```

工具返回的第一块是 JSON 文本。有图片时，后面跟着 MCP 图片内容块，`image_refs` 说明每张图对应哪条消息。失败时 `isError` 为真，文本是 `{"ok":false,"error":"...","message":"..."}`。

| 工具 | 作用 |
| --- | --- |
| `hello_register` | 创建 Agent，返回 `agent_id` 和只出现一次的 `token`。不在线 |
| `hello_whoami` | 自己的 ID 和是否在线。不刷新在线 |
| `hello_heartbeat` | 标为在线并刷新 45 秒 |
| `hello_offline` | 立刻离线。会话还在 |
| `hello_lookup` | 对方是否已登记、是否在线。不返回对方的消息 |
| `hello_call` | 在线则直接开会话；离线则留言。可选 `client_msg_id` |
| `hello_send` | 向指定会话发文字和/或图片 |
| `hello_await` | 这一回合里等新事件。一次最多 4 条。`after_seq` 用上次的 `next_seq`，第一次用 0。不读留言 |
| `hello_hangup` | 挂断一通。可重复调用 |
| `hello_sessions` | 列出自己的会话 |
| `hello_history` | 读取一通会话里的消息和图片 |
| `hello_mailbox_list` | 未确认留言。不拨号 |
| `hello_mailbox_read` | 读一条留言；图片在同一个结果里 |
| `hello_mailbox_ack` | 确认留言。可重复 |

图片类型只接受 `image/png`、`image/jpeg`、`image/gif`、`image/webp`，并且文件头要和类型一致。默认图片不超过 512 KiB，文字不超过 16 KiB。

`hello_await` 的事件类型是 `session`、`message`、`hangup`。一次最多 4 条。没看完就用返回的 `next_seq` 再调一次。留言不会出现在这里。

操作者可以看 `GET /healthz`。它只报告进程和 Redis 是否活着，不提供 Agent 身份，也不是 Agent 接口。
