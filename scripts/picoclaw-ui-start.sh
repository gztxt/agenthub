#!/usr/bin/env bash
# picoclaw 原生界面（launcher :18800）按需拉起 + 免二次登录
#
# 为什么需要这个脚本（2026-11-24）：
#   picoclaw-launcher 的 dashboard 是**自带密码登录**（bcrypt 存 ~/.picoclaw/launcher-auth.db，
#   会话 Cookie `picoclaw_launcher_auth` 的值是**进程级随机**，见上游
#   web/backend/middleware/launcher_dashboard_auth.go: NewLauncherDashboardSessionCookie）。
#   ⇒ 嵌进 hub 就是「第二套登录」。收口方式与 codebuddy 同款：hub 桥带
#   `upstream_cookie_file` 注入 Cookie，本脚本负责在拉起后**服务端登录一次**并刷新那枚
#   Cookie 文件。用户侧始终只认 hub 登录。
#
# 三个落点（都 0600）：
#   ~/.config/picoclaw-dashboard.secret   dashboard 口令（首次运行自动生成，无人工输入）
#   ~/.config/picoclaw-dashboard.cookie   桥读的上游 Cookie（每次拉起重写）
#   ~/.picoclaw/logs/ui-start.log         launcher 输出
set -uo pipefail

BIN=/vol1/tools/picoclaw-0.3.1/picoclaw-launcher
PORT=18800
BASE="http://127.0.0.1:${PORT}"
PW_FILE="$HOME/.config/picoclaw-dashboard.secret"
COOKIE_FILE="$HOME/.config/picoclaw-dashboard.cookie"
LOG_FILE="$HOME/.picoclaw/logs/ui-start.log"
COOKIE_NAME=picoclaw_launcher_auth

mkdir -p "$(dirname "$LOG_FILE")" "$HOME/.config"

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; }

if ! port_open; then
  [ -x "$BIN" ] || { echo "picoclaw-launcher 不在 $BIN" >&2; exit 10; }
  # -host 0.0.0.0：2026-11-24 用户令「所有 agenthub 网段 + VPN 网段都能访问」。
  # 访问控制不靠绑定面，靠 launcher 自己的 IPAllowlist：~/.picoclaw/launcher-config.json
  # 的 allowed_cidrs（192.168.5.0/24 局域网 / 100.64.0.0/10 Tailscale / 172.16.0.0/22 oray），
  # 外加 allow_localhost_bypass（hub 桥从回环进）。dashboard 会话仍在（同一枚 Cookie）。
  setsid nohup "$BIN" -no-browser -console -host 0.0.0.0 \
    >>"$LOG_FILE" 2>&1 </dev/null &
  # 有界等待：最多 ~15s，不做无限轮询
  for _ in $(seq 1 30); do
    port_open && break
    sleep 0.5
  done
  port_open || { echo "launcher 未在 ${PORT} 起来，见 $LOG_FILE" >&2; exit 11; }
fi

# 口令：首次生成，仅落 0600 文件（与 pi-web 的口令文件同款做法）
if [ ! -s "$PW_FILE" ]; then
  umask 077
  openssl rand -hex 18 > "$PW_FILE"
fi
PW="$(cat "$PW_FILE")"

# 首次运行要建口令（上游：POST /api/auth/setup，同源校验，须带 Origin）
INIT="$(curl -sS --max-time 5 "${BASE}/api/auth/status" 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("initialized"))' 2>/dev/null)"
if [ "$INIT" != "True" ]; then
  curl -sS --max-time 8 -X POST "${BASE}/api/auth/setup" \
    -H 'Content-Type: application/json' \
    -H "Origin: ${BASE}" \
    -d "{\"password\":\"${PW}\",\"confirm\":\"${PW}\"}" >/dev/null 2>&1
fi

# 登录换 Cookie（值进程级随机，所以每次拉起都要重取）
login_header() {
  curl -sS -D - -o /dev/null --max-time 8 -X POST "${BASE}/api/auth/login" \
    -H 'Content-Type: application/json' -H "Origin: ${BASE}" \
    -d "{\"password\":\"${PW}\"}" 2>/dev/null \
    | tr -d '\r' | sed -n "s/^[Ss]et-[Cc]ookie: ${COOKIE_NAME}=\([^;]*\).*/\1/p" | head -1
}
COOKIE_VAL="$(login_header)"

# 自愈：库里那枚 bcrypt 与口令文件对不上（典型成因——改库时 launcher 还活着，
# 进程退出时把旧库刷回；2026-11-24 实测）。清行 + 重设口令后重试一次。
if [ -z "$COOKIE_VAL" ]; then
  echo "口令与库不一致，重建 dashboard 口令…" >&2
  sqlite3 "$HOME/.picoclaw/launcher-auth.db" "DELETE FROM dashboard_credentials;" 2>/dev/null
  curl -sS --max-time 8 -X POST "${BASE}/api/auth/setup" \
    -H 'Content-Type: application/json' -H "Origin: ${BASE}" \
    -d "{\"password\":\"${PW}\",\"confirm\":\"${PW}\"}" >/dev/null 2>&1
  COOKIE_VAL="$(login_header)"
fi

[ -n "$COOKIE_VAL" ] || { echo "dashboard 登录失败（已重试重建口令）" >&2; exit 12; }

umask 077
printf '%s=%s\n' "$COOKIE_NAME" "$COOKIE_VAL" > "$COOKIE_FILE"

# 自证：带 Cookie 取首页应是 200，不带是 302
CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
        -H "Cookie: ${COOKIE_NAME}=${COOKIE_VAL}" "${BASE}/")"
[ "$CODE" = "200" ] || { echo "自证失败：带 Cookie 取 / 返回 ${CODE}" >&2; exit 13; }
echo "picoclaw launcher ready on ${BASE} (cookie refreshed)"
