#!/usr/bin/env bash
# 影子实例启动器（P0 编目复验用）：隔离 DATA_DIR/LOG_DIR/端口，带一次性 TERM_TOKEN。
# ⚠ 不写 crontab、不改生产单元；只用精确 PID 停。
set -euo pipefail
WT=/fs/1000/ftp/技术文档/agenthub-wt-cat1025
WORK=/fs/1000/ftp/技术文档/agenthub/work
TOKEN_FILE="$WORK/shadow-token.txt"
[ -s "$TOKEN_FILE" ] || { venv_pw=$("$WT/venv/bin/python3" -c "import secrets;print(secrets.token_urlsafe(32))"); echo "$venv_pw" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; }
TOKEN="$(cat "$TOKEN_FILE")"

cd "$WT"
exec setsid env -i HOME="$HOME" PATH=/usr/bin:/bin \
  PYTHONPATH="$WT/src" \
  DATA_DIR="$WORK/cat-p0-data" \
  LOG_DIR="$WORK/cat-p0-data/logs" \
  AGENTHUB_NATIVE_DB=/fs/1000/ftp/技术文档/agenthub/data/agents.db \
  TERM_TOKEN="$TOKEN" \
  PORT=3199 HOST=127.0.0.1 HUB_CATALOG=1 \
  EMBED_UNIFY=0 REF_AUTO_REFRESH=0 HUB_MODEL_DRIFT_REPAIR=0 \
  "$WT/venv/bin/python3" -m src.main > "$WORK/shadow-3199.log" 2>&1 < /dev/null
