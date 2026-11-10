# MCP 示例

这里只有主机配置和测试夹具说明。没有单独的 Agent 客户端。

- `claude-code.mcp.json`：日常连接。启动前用 `read` 把权限 600 的 token 文件读进 `HELLO_AGENT_TOKEN`，不要打印这个值。见 `docs/部署与接入.md`。
- `claude-code-register.mcp.json`：只用于第一次登记。登记后换成上面的 Bearer 配置。
- `dsh-entry.yaml`：插件字段来自官方 README。`insert:` 外层来自官方示例 `examples/mcp-memory/README.md`。本机该写入哪个文件，两处都没写死，这里也不猜。dsh 还没跑过。图片要进对话，还得模型接受图片且 attachment 开着。

`scripts/mcp_worker.py` 是验证用的 MCP 协议进程。它不会替 Agent 做心跳或收信策略。不要把它装成产品命令。

两个 Agent 就是两个主机进程、两份凭证。它们可以在同一台机器上，也可以在不同机器上，但都必须连到同一个 Hello Center 的 HTTP 地址。本仓库只在一台 Linux 上用独立进程验证过前者。
