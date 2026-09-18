#!/usr/bin/env bash
# 仓库根目录的入口：转发到 server/run.sh，这样在根目录直接 ./run.sh 也能启动。
#
# 说明：`python3 -m app` 这类命令必须在 server/ 目录里执行（app 包在那里），
# 所以优先用这个脚本，不用记目录。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
echo "[转发] $here/run.sh → $here/server/run.sh $*" >&2
exec "$here/server/run.sh" "$@"
