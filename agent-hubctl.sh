#!/bin/bash
# Agent Hub 控制脚本 —— 生命周期唯一真源是 systemd user unit，本脚本只做「代理 + 取证」。
#
# 为什么重写（2026-09-23 实测取证，P0-3）：
#   旧版自带一套 nohup + data/agent-hub.pid 的私有生命周期，与
#   ~/.config/systemd/user/agent-hub.service（Restart=always，实际在管这个服务）并存，
#   于是出现三类真事故：
#     ① `status` 读 PID 文件判活 ⇒ 服务其实 active、/health 200，它却报「❌ 未运行」；
#        （工作区里那枚 data/agent-hub.pid 内容是 85512，而该 PID 早就不存在 —— 纯 stale）
#     ② `stop` 是 `kill $(cat pidfile)`，**不校验那个 PID 到底是不是 hub**；
#        NAS 上 PID 会复用，一旦被别的进程认领，这条命令就会误杀无关进程；
#     ③ `start` 无条件 nohup 再起一个 uvicorn ⇒ 与 systemd 抢 :3102，
#        或养出一个没有 Restart=always 照看的孤儿实例。
#   这三条正踩在本机最高约束上（MEMORY.md 2026-09-06：不得未分析就接管生命周期/占端口）。
#
# 现在的口径：
#   · 起停一律走 systemctl --user（唯一拥有者），本脚本不再 nohup、不再写 PID 文件；
#   · 任何 kill 前必须过 /proc/<pid>/cmdline 身份校验，对不上就拒绝并报告；
#   · stop/restart 前先读 /health 的 term_sessions（活终端会话数）——非 0 则拒绝，
#     因为服务关闭钩子会 kill_all() 把终端会话连坐杀掉（施工期不得打断在跑会话）；
#   · 确有必要才用 FORCE=1 明示承担后果。
#
# 自检开关：HUBCTL_DRY=1 只打印将要执行的 systemctl 命令，不实际执行（供回归测用）。
set -euo pipefail

# 2026-10-12 修默认值漂移：原默认指向旧家目录仓 `$HOME/agent-hub`（已不存在）与旧 unit 名
# `agent-hub.service`（已改名），本机靠 env 兜住、对外 clone 会读旧名。改为按脚本所在目录
# 推导仓路径 + 新 unit 名（`APP_DIR`/`UNIT` 仍可被 env 覆盖）。
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
UNIT="${UNIT:-agenthub.service}"
# 自路径必须从 BASH_SOURCE 取。`bash agent-hubctl.sh restart` 时 $0 == "agent-hubctl.sh"
# （没有 ./ 前缀），拿它当命令执行就是 command not found —— 而它藏在 start/restart 分支的收尾，
# 主流程（systemctl 委托）已跑完，所以只在末尾安静留一行错误、退出码仍是 0。
# 09-23 真重启实测到（line 120: agent-hubctl.sh: command not found）。
# 提示文案里的 `$0 status` 保留：那是给人看的示例，不是执行路径。
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PORT="${PORT_CHECK:-3102}"
PID_FILE="$APP_DIR/data/agent-hub.pid"          # 历史遗留，只用于体检，不再作为判据
BASE="http://127.0.0.1:$PORT"
DRY="${HUBCTL_DRY:-0}"

c_ok()   { printf '\033[32m%s\033[0m\n' "$1"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$1"; }
c_bad()  { printf '\033[31m%s\033[0m\n' "$1"; }

have_unit() { systemctl --user list-unit-files "$UNIT" >/dev/null 2>&1 \
              && [ -n "$(systemctl --user list-unit-files "$UNIT" 2>/dev/null | awk 'NR>1&&NF{print $1}')" ]; }

unit_state() { systemctl --user is-active "$UNIT" 2>/dev/null || true; }

# 只有 cmdline 里同时出现 uvicorn 与 src.main:app 才认定是 hub 本尊
is_hub_pid() {
  local p="${1:-}"
  [ -n "$p" ] && [ -d "/proc/$p" ] || return 1
  local cl
  cl=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null || true)
  case "$cl" in
    *uvicorn*src.main:app*|*src.main:app*uvicorn*) return 0 ;;
    *) return 1 ;;
  esac
}

hub_pid_from_port() {
  ss -ltnp "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true
}

json_get() {  # json_get <url> <key>
  curl -sS -m 4 "$1" 2>/dev/null | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('$2',''))
except Exception: print('')"
}

guard_live_sessions() {  # 停服前：有活终端会话就拒绝（除非 FORCE=1）
  local n
  n=$(json_get "$BASE/health" term_sessions)
  [ -z "$n" ] && n=0
  if [ "$n" != "0" ]; then
    if [ "${FORCE:-0}" = "1" ]; then
      c_warn "⚠️  FORCE=1：将杀掉 $n 个活终端会话（shutdown 钩子 term.kill_all()）"
      return 0
    fi
    c_bad "✋ 拒绝：当前有 $n 个活终端会话，重启会连坐杀掉它们。"
    echo "   先在前端关掉会话，或确要承担则：FORCE=1 $0 ${ACTION:-restart}"
    return 1
  fi
  return 0
}

diagnose_pidfile() {
  [ -f "$PID_FILE" ] || return 0
  local p; p=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$p" ] && is_hub_pid "$p"; then
    c_warn "PID 文件 $PID_FILE 指向 $p（确为 hub）。注意：判活已改走 systemd，此文件不再使用。"
  else
    c_warn "PID 文件陈旧：$PID_FILE 写着 ${p:-<空>}，该 PID 不是 hub（多半已复用/不存在）。已按新口径忽略。"
  fi
}

detect_foreign() {
  local p; p=$(hub_pid_from_port)
  if [ -n "$p" ] && ! is_hub_pid "$p"; then
    c_bad "发现占 :$PORT 的非 hub 进程 PID=$p：$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | cut -c1-90)"
  elif [ -n "$p" ] && [ "$(unit_state)" != "active" ]; then
    c_warn ":$PORT 由 PID=$p（hub 本尊）在听，但 systemd 单元并非 active ⇒ 存在游离实例（nohup 遗留）。"
    echo "   这类实例没有 Restart=always 照看。要收编：先确认它可停，再 systemctl --user start $UNIT"
  fi
}

ACTION="${1:-status}"
case "$ACTION" in
  start)
    if ! have_unit; then c_bad "未找到 systemd 用户单元 $UNIT —— 拒绝退回 nohup（那正是本次要消灭的双头）。"; exit 2; fi
    if [ "$(unit_state)" = "active" ]; then c_ok "已在运行（unit active），不重复启动。"; bash "$SELF" status; exit 0; fi
    if [ "$DRY" = "1" ]; then echo "[dry] systemctl --user start $UNIT"; exit 0; fi
    systemctl --user start "$UNIT"
    for _ in $(seq 1 15); do
      [ "$(json_get "$BASE/health" status)" = "ok" ] && { c_ok "✅ 已启动并通过 /health"; bash "$SELF" status; exit 0; }
      sleep 1
    done
    c_bad "❌ 15s 内 /health 未就绪 —— 看 journalctl --user -u $UNIT -n 50"; exit 1
    ;;
  stop|restart)
    if ! have_unit; then c_bad "未找到单元 $UNIT，拒绝操作。"; exit 2; fi
    guard_live_sessions || exit 3
    if [ "$DRY" = "1" ]; then echo "[dry] systemctl --user $ACTION $UNIT"; exit 0; fi
    systemctl --user "$ACTION" "$UNIT"
    c_ok "已委托 systemd 执行 $ACTION（状态见 $0 status）"
    [ "$ACTION" = "restart" ] && sleep 2 && bash "$SELF" status || true
    ;;
  status)
    if ! have_unit; then c_warn "无 systemd 单元（本脚本不做 nohup 兜底）"; fi
    s=$(unit_state)
    [ "$s" = "active" ] && c_ok "unit: active（$UNIT）" || c_bad "unit: ${s:-unknown}（$UNIT）"
    diagnose_pidfile
    detect_foreign
    if [ -n "$(curl -sS -m 4 "$BASE/health" 2>/dev/null || true)" ]; then
      curl -sS -m 4 "$BASE/health" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  /health : %s  v%s  pid=%s  port=%s' % (d.get('status'),d.get('version'),d.get('pid'),d.get('port')))
print('  代码 sha: boot=%s now=%s %s' % (d.get('git_sha_boot','?'), d.get('git_sha_now','?'),
      '← ⚠️ 代码已改但服务未重启' if d.get('code_stale') else '（一致）'))
print('  运行时长: %ss   库: %s   活终端会话: %s (最闲 %ss)' % (
      d.get('uptime_s','?'), 'ok' if d.get('db_ok') else 'DOWN',
      d.get('term_sessions','?'), d.get('term_idle_max_s','?')))
" 2>/dev/null || curl -sS -m 4 "$BASE/health"
    else
      c_warn "/health 无响应（$BASE/health）"
    fi
    ;;
  log)
    if [ "${2:-}" = "-f" ]; then
      journalctl --user -u "$UNIT" -n 100 --no-pager -f
    else
      journalctl --user -u "$UNIT" -n "${2:-100}" --no-pager
    fi
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log}   （起停一律委托 systemctl --user）"
    echo "      停服确要连带杀终端会话：FORCE=1 $0 stop"
    exit 1
    ;;
esac
