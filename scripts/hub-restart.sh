#!/usr/bin/env bash
# hub-restart.sh — agenthub 生产单元的统一重启入口（活会话守卫 + 去抖 + 审计）
#
# 为什么需要它（2026-10-25 取证）：
#   `systemctl --user restart agenthub` 是 `KillMode=control-group`，stop 会把
#   cgroup 内**所有**进程一起杀掉 —— 包括用户正开着的全部终端会话（pty 子进程）。
#   2026-10-25 06:20–07:46 有 8 次重启，全部来自并行开发会话「改一行→重启→验」，
#   每次重启都让正在使用的页面断开、终端会话消失，用户体感是「agenthub 总是自动断开」。
#   服务本身没有崩溃记录（journal 每次都是 graceful "Shutting down"，NRestarts=0）。
#
# 用法：
#   scripts/hub-restart.sh --check                 # 只报活会话数与是否在冷却期，不动服务
#   scripts/hub-restart.sh --reason "改了什么"      # 有活会话则拒绝（退出码 3）
#   scripts/hub-restart.sh --force --reason "..."   # 确认承担后果才强制
#   scripts/hub-restart.sh --dry-run ...            # 只打印将执行的命令
#
# 判据：
#   活会话数取自 /health 的 term_sessions（与后端 api_system.py 同口径）。
#   冷却窗口内重复重启一律拒绝（默认 60s，HUB_RESTART_COOLDOWN 可改）——
#   「改一行重启一次」的连环重启在用户侧就是持续掉线。
#   每次放行/拒绝都写一行 JSON 到 data/logs/restart.log，事后可追责到时刻与原因。
set -uo pipefail

UNIT=${HUB_UNIT:-agenthub.service}
PORT=${HUB_PORT:-3102}
BASE=${HUB_BASE:-http://127.0.0.1:$PORT}
LOG_DIR=${HUB_LOG_DIR:-/fs/1000/ftp/技术文档/agenthub/data/logs}
AUDIT="$LOG_DIR/restart.log"
STAMP="$LOG_DIR/.last-restart-epoch"
COOLDOWN=${HUB_RESTART_COOLDOWN:-60}

FORCE=0
CHECK=0
DRY=0
REASON=""

while [ $# -gt 0 ]; do
  case "$1" in
    --force)   FORCE=1 ;;
    --check)   CHECK=1 ;;
    --dry-run) DRY=1 ;;
    --reason)  shift; REASON=${1:-} ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$LOG_DIR" 2>/dev/null || true

term_sessions() {
  curl -s --max-time 3 "$BASE/health" 2>/dev/null \
    | python3 -c 'import json,sys
try: print(int(json.load(sys.stdin).get("term_sessions") or 0))
except Exception: print(-1)'
}

audit() {  # audit <decision> <sessions> [detail]
  python3 - "$AUDIT" "$1" "$2" "${3:-}" "$REASON" "$UNIT" <<'PY'
import json, sys, time, os
path, decision, sessions, detail, reason, unit = sys.argv[1:7]
row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "decision": decision,
       "term_sessions": int(sessions) if sessions.lstrip("-").isdigit() else -1,
       "reason": reason, "unit": unit, "caller": os.environ.get("HUB_CALLER", "shell")}
if detail:
    row["detail"] = detail
try:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
except Exception as e:
    print("审计写入失败: %s" % e, file=sys.stderr)
PY
}

alive=$(term_sessions)
if [ "$alive" -lt 0 ]; then
  echo "❌ 取不到 $BASE/health（服务未起？）；term_sessions 不可判定，拒绝继续" >&2
  audit "abort_unreachable" -1
  exit 5
fi

# 冷却检查：最后一次成功重启至今
cooling=0
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  now=$(date +%s)
  if [ $((now - last)) -lt "$COOLDOWN" ]; then
    cooling=$((COOLDOWN - (now - last)))
  fi
fi

echo "unit=$UNIT  term_sessions=$alive  冷却剩余=${cooling}s  冷却窗口=${COOLDOWN}s"

if [ "$CHECK" -eq 1 ]; then
  [ "$alive" -gt 0 ] && echo "⇒ 有活会话：重启会杀掉它们（用户页面/终端会断开）"
  [ "$cooling" -gt 0 ] && echo "⇒ 冷却期内：再重启属于连环重启"
  [ "$alive" -eq 0 ] && [ "$cooling" -eq 0 ] && echo "⇒ 可以安全重启"
  exit 0
fi

# --force 的防呆（2026-10-25 踩过：测试脚本时 `--force --reason "冷却测试"` 真的重启了
# 生产单元，把 3 个活会话连坐杀掉 —— force 不能是「无脑绕过」）。
# 判据只有一个：显式 `HUB_RESTART_FORCE=1`。不认 TTY（agent 子进程也可能挂着 pty，
# 07:54 实测 `-t 0` 判断为真 ⇒ 防呆失效）。要强制就必须写下这个环境变量，
# 它本身就是「我知道我在杀会话」的签字。
if [ "$FORCE" -eq 1 ] && [ "${HUB_RESTART_FORCE:-0}" != "1" ]; then
  echo "⛔ 拒绝：--force 必须同时设 HUB_RESTART_FORCE=1（显式人工签字）。" >&2
  echo "   agent/脚本自动路径不得强制重启生产单元（军规：有活动会话严禁自动断开）。" >&2
  audit "deny_force_unsigned" "$alive"
  exit 7
fi

cmd=(/usr/bin/systemctl --user restart "$UNIT")
if [ "$DRY" -eq 1 ]; then
  echo "+ ${cmd[*]}   (term_sessions=$alive)"
  audit "dry_run" "$alive"
  exit 0
fi

if [ "$cooling" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
  echo "⛔ 拒绝：${cooling}s 前刚重启过（去抖窗口 ${COOLDOWN}s）。连环重启＝用户持续掉线。" >&2
  echo "   确要重启：加 --force，并在 --reason 里写清为什么必须现在重启。" >&2
  audit "deny_cooldown" "$alive" "cooldown_left=${cooling}s"
  exit 4
fi

if [ "$alive" -gt 0 ] && [ "$FORCE" -eq 0 ]; then
  echo "⛔ 拒绝：还有 $alive 个终端会话在跑，restart 会把它们一起杀掉。" >&2
  echo "   先在页面里关掉会话，或确认承担后加 --force（会话记录不会保留现场）。" >&2
  audit "deny_live_sessions" "$alive"
  exit 3
fi

audit "allow" "$alive"
"${cmd[@]}" || { echo "❌ ${cmd[*]} 失败" >&2; audit "failed" "$alive"; exit 1; }
date +%s > "$STAMP"

# 重启后健康复核：3s 内能回 200 才算完成
for _ in $(seq 1 20); do
  if curl -s -o /dev/null --max-time 2 "$BASE/health"; then
    curl -s --max-time 3 "$BASE/health" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("✅ 已恢复 v%s pid=%s code_stale=%s" % (d.get("version"),d.get("pid"),d.get("code_stale")))'
    audit "recovered" 0
    exit 0
  fi
  sleep 0.5
done
echo "⚠️ 重启后 10s 内 /health 未恢复，需人工检查" >&2
audit "unhealthy_after_restart" 0
exit 6
