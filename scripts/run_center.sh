#!/bin/sh
# 启动 Hello Center。登记口令必须来自环境变量，不要写进命令行历史以外的仓库文件。
set -eu
cd "$(dirname "$0")/.."
if [ -z "${HELLO_REGISTER_SECRET:-}" ] && [ -f config/hello-center.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./config/hello-center.env
  set +a
fi
if [ -z "${HELLO_REGISTER_SECRET:-}" ]; then
  echo "没有登记口令。先运行 scripts/init_local_env.sh。它写入 config/hello-center.env，不打印口令。" >&2
  exit 2
fi
PY=${HELLO_PYTHON:-.venv/bin/python}
export PYTHONPATH=.
export PYTHONUNBUFFERED=1
exec "$PY" -m hello_center
