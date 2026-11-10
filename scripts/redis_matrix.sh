#!/bin/sh
# 用三个已编译的 redis-server 各跑一遍独立进程验证。
# 用法: scripts/redis_matrix.sh <redis-5.0.14> <redis-6.2.18> <redis-7.2.11>
set -eu
if [ "$#" -ne 3 ]; then
  echo "用法: scripts/redis_matrix.sh <redis-5> <redis-6> <redis-7>" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
PY=${HELLO_PYTHON:-.venv/bin/python}
export PYTHONPATH=.
"$PY" scripts/verify_e2e.py --redis-server "$1" --work /tmp/hello-verify-5 --report /tmp/hello-report-5.json
"$PY" scripts/verify_e2e.py --redis-server "$2" --work /tmp/hello-verify-6 --report /tmp/hello-report-6.json
"$PY" scripts/verify_e2e.py --redis-server "$3" --work /tmp/hello-verify-7 --report /tmp/hello-report-7.json
echo "三份结果在 /tmp/hello-report-5.json、6、7。这仍是同一台机器上的进程验证。"
