# Hello Center

Agent 之间打电话用的中心服务。Agent 只通过 MCP 连接。状态放在 Redis 里。

两个 Agent 用两个 MCP 连接、两个凭证。它们可以在同一台机器上，也可以通过中心的 HTTP 地址分处两台机器。身份是 Agent ID，不是 IP。文字和图片都经过中心。

这不是完整验收。本仓库只在一台 Linux 虚拟机上，用独立进程验证了协议。没有跑过真实的 Claude Code、DeepSeek Harness（`dsh`），也没有 Linux 加 macOS 的跨机器验收。

## 运行

需要 Python 3.12。这次实际安装和运行用的是 3.12.3，没有在更旧的 Python 上试过。`redis-server` 换成你机器上的路径，例如 `command -v redis-server` 的结果。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
scripts/init_local_env.sh
mkdir -p "$HOME/hello-redis"
scripts/run_redis.sh "$(command -v redis-server)" "$HOME/hello-redis" 6379
scripts/run_center.sh
```

6379 上如果已经有 Redis，不要再启动一份。沿用现有实例，只把 `config/hello-center.env` 里的 `HELLO_REDIS_URL` 指过去。数据目录用自己能写的新目录，不要用 `/var/lib`。

`scripts/init_local_env.sh` 把登记口令写进 `config/hello-center.env`（权限 600），不打印口令。这个文件已被 git 忽略。示例只有空位，在 `config/hello-center.env.example`。

每个 Agent 再登记一次，凭证只进文件：

```sh
set -a; . ./config/hello-center.env; set +a
.venv/bin/python scripts/register_once.py --token-file config/agent-a.token --display-name 甲
.venv/bin/python scripts/register_once.py --token-file config/agent-b.token --display-name 乙
```

标准输出只有一行 Agent ID，例如 `80-000-001`。没有 JSON。每个 Agent 用自己的终端装入凭证，不要 `echo` 或 `cat`：

```sh
(
  IFS= read -r HELLO_AGENT_TOKEN < config/agent-a.token
  export HELLO_AGENT_TOKEN
  exec 这里换成该 Agent 的主机启动命令
)
```

另一个终端把 `agent-a.token` 换成 `agent-b.token`。父终端不会留下这个变量。不要把文件提交进仓库，也不要贴进对话。

MCP 地址是 `http://127.0.0.1:8731/mcp`。Redis 5.0.14、6.2.18、7.2.11 跑过完整验证；5.0.14 是已测下限，不是说每个中间版本都测过。细节在 [docs/部署与接入.md](docs/部署与接入.md)。

最小验收会另起一份 Redis，默认端口 56479，不占用已有的 6379。结束时它会关掉自己启动的那一份。这仍是本机上的 MCP 进程，不是真实主机或跨机器：

```sh
scripts/minimal_accept.sh "$(command -v redis-server)"
```

产品要求在 [docs/产品需求.md](docs/产品需求.md)。工具说明在 [docs/MCP工具.md](docs/MCP工具.md)。验证结果在 [docs/验收报告.md](docs/验收报告.md)。

给模型看的用法在 `skills/hello-center/SKILL.md`。它只教怎么调用工具，自己不通信。
