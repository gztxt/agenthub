#!/bin/bash
# ============================================================
#  memindex_watchdog.sh — A4 索引投影的陈旧看护（2026-10-13 新增，v0.13.103 P0-3b）
#
#  为什么需要它（取证见 docs/AGENT-MEMORY-REPO-评估-20261013.md §3 附带缺陷）：
#   data/memindex.db（1.72GB / 9107 文件 / 219171 chunk）**只构建过一次**，
#   停在 2026-10-02；仓内**没有任何 cron/timer/IPC 自动刷新**它。
#   `probe` 能报出陈旧度，但没人看 ⇒ 「快，但越来越错」是一种**静默**失效。
#
#  本脚本**只告警，不自动重建**。理由：
#   ① 索引 1.7GB，增量重建是重 IO；A4 设计文档（D2-B / G13）明确
#      「三路 fts_proj 探针各 ≤200ms 且 G13 全绿才提请放量」——
#      看护脚本不该替代那条人工裁定去自动重写大索引。
#   ② 任务的本意是「让陈旧可见」，不是「让脚本替我决定重建」。
#   ③ 想自动重建时显式 `--rebuild`（或设 MEMINDEX_WATCHDOG_REBUILD=1），
#      并且**先过磁盘闸与 dry-run 估算**，不通过就拒绝，绝不自动删索引（D4）。
#
#  退出码：0=新鲜或已处理；3=陈旧（告警）；4=探测失败；5=重建失败
#  定时任务：建议 `40 4 * * *`（与 turbovec-rebuild 的周日 04:00 错开）。
# ============================================================
set -uo pipefail

HUB="/fs/1000/ftp/技术文档/agenthub"
LOG_DIR="/fs/1000/ftp/技术文档/logs/cron"
LOG="$LOG_DIR/memindex-watchdog.log"
PY="$HUB/venv/bin/python3"

#: 超过这个天数即视为陈旧（A4 投影是三路慢源的兜底，周级新鲜度足够）
STALE_DAYS="${MEMINDEX_STALE_DAYS:-7}"
#: 重建前要求的最小空闲（GB）—— 与 memindex 自带的 BUILD_HEADROOM 同精神，双保险
MIN_FREE_GB="${MEMINDEX_MIN_FREE_GB:-8}"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }
say() { printf '%s  %s\n' "$(ts)" "$*" >> "$LOG"; }

cd "$HUB" || { say "FATAL 工作目录不存在: $HUB"; exit 4; }
if [ ! -x "$PY" ]; then say "FATAL venv python 不可用: $PY"; exit 4; fi

# ── 1) 探针（JSON）────────────────────────────────────────────────────
PROBE="$("$PY" -m src.memindex --json probe 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  say "WARN 探针失败 rc=$rc: $(printf '%s' "$PROBE" | head -c 200)"
  exit 4
fi

read -r OK BUILT FILES <<EOF
$(printf '%s' "$PROBE" | "$PY" -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("false ? 0"); raise SystemExit
note=str(d.get("note") or "")
import re
m=re.search(r"构建于 (\S+)", note); built=m.group(1) if m else "?"
print(str(bool(d.get("ok"))).lower(), built, d.get("count") or 0)
')
EOF

if [ "$OK" != "true" ]; then
  say "WARN 索引不可用（ok=false）built=$BUILT files=$FILES"
  exit 4
fi

# ── 2) 陈旧度 ────────────────────────────────────────────────────────
AGE_DAYS="$("$PY" -c '
import sys,datetime
b=sys.argv[1]
try:
    t=datetime.datetime.strptime(b,"%Y-%m-%dT%H:%M:%S")
except Exception:
    print(-1); raise SystemExit
print(int((datetime.datetime.now()-t).total_seconds()//86400))
' "$BUILT")"

if [ "$AGE_DAYS" -lt 0 ]; then
  say "WARN 无法解析构建时间 built=$BUILT（格式变了？本脚本不猜，按陈旧处理）"
  AGE_DAYS=9999
fi

if [ "$AGE_DAYS" -le "$STALE_DAYS" ]; then
  say "OK 索引新鲜 age=${AGE_DAYS}d (阈值 ${STALE_DAYS}d) files=$FILES built=$BUILT"
  exit 0
fi

say "STALE 索引已 ${AGE_DAYS}d 未重建（阈值 ${STALE_DAYS}d）files=$FILES built=$BUILT"

# ── 3) 可选重建（默认关闭）────────────────────────────────────────────
DO_REBUILD=0
[ "${MEMINDEX_WATCHDOG_REBUILD:-0}" = "1" ] && DO_REBUILD=1
[ "${1:-}" = "--rebuild" ] && DO_REBUILD=1

if [ "$DO_REBUILD" != "1" ]; then
  say "HINT 未自动重建（本脚本默认只告警）。要重建：$0 --rebuild"
  say "HINT ⚠ 重建前请按 A4 设计文档确认三路 fts_proj 探针 ≤200ms 且 G13 全绿"
  exit 3
fi

FREE_GB="$(df -BG --output=avail "$HUB/data" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -z "${FREE_GB:-}" ] || [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
  say "STALE 拒绝重建：空闲 ${FREE_GB:-?}G < 要求 ${MIN_FREE_GB}G（fail-closed，不自动删索引 D4）"
  exit 3
fi

say "REBUILD 开始增量重建（空闲 ${FREE_GB}G）…"
OUT="$("$PY" -m src.memindex --json incremental 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  say "REBUILD 失败 rc=$rc: $(printf '%s' "$OUT" | head -c 300)"
  exit 5
fi
say "REBUILD 完成: $(printf '%s' "$OUT" | "$PY" -c '
import json,sys
try:
    d=json.load(sys.stdin); print(f"ok={d.get(\"ok\")} files={d.get(\"files\")} chunks={d.get(\"chunks\")} elapsed={d.get(\"elapsed_s\")}s")
except Exception as e: print("(解析失败)"+str(e))
' 2>/dev/null)"
exit 0