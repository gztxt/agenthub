#!/usr/bin/env bash
# 改 nanobot WebUI 登录串（上游没有设置页，唯一落点是 config.json 的
# channels.websocket.tokenIssueSecret，见 cli/docker_bootstrap.py 里它被直接叫作
# "WebUI password"）。本脚本做三件事，顺序不能换：
#   1) 备份 + 写新值（不要 sed，JSON 会被改坏）
#   2) 重启 gateway（websocket 配置只在进程启动时读，热改不生效）
#   3) 往 hub 的 ui-start 日志追加新行 —— hub 嵌页地址就是从那里抓的
#
# 用法：
#   bash scripts/nanobot-set-webui-secret.sh              # 随机生成
#   bash scripts/nanobot-set-webui-secret.sh 'my-pass'    # 指定
set -uo pipefail

CFG="$HOME/.nanobot/config.json"
BIN=/vol1/tools/nanobot-0.4.2/venv/bin/nanobot
START=/fs/1000/ftp/技术文档/agenthub/scripts/nanobot-ui-start.sh
HUB_LOG=/fs/1000/ftp/技术文档/agenthub/data/logs/ui-start/nanobot.log

[ -f "$CFG" ] || { echo "配置不存在：$CFG" >&2; exit 10; }

NEW="${1:-}"
if [ -z "$NEW" ]; then
  NEW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
fi
case "$NEW" in
  *[[:space:]]*) echo "密码不能含空白" >&2; exit 11 ;;
esac

cp "$CFG" "$CFG.bak-$(date +%Y%m%d_%H%M%S)-webuipass"
python3 - "$NEW" <<'PY'
import json, os, sys
p = os.path.expanduser("~/.nanobot/config.json")
d = json.load(open(p))
d["channels"]["websocket"]["tokenIssueSecret"] = sys.argv[1]
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
PY
echo "已写入 $CFG"

"$BIN" gateway stop >/dev/null 2>&1 || true
sleep 2
mkdir -p "$(dirname "$HUB_LOG")"
bash "$START" >>"$HUB_LOG" 2>&1
rc=$?
tail -2 "$HUB_LOG"

PORT="$(python3 -c 'import json,os;d=json.load(open(os.path.expanduser("~/.nanobot/config.json")));print(d["channels"]["websocket"].get("port") or 18880)')"
CODE="$(curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' -m 8 -H "Authorization: Bearer $NEW" "http://127.0.0.1:${PORT}/webui/bootstrap")"
[ "$CODE" = "200" ] || { echo "自证失败：新密码调 /webui/bootstrap 返回 $CODE" >&2; exit 13; }

cat <<EOF

新登录串：$NEW
登录链接：http://127.0.0.1:${PORT}/#/?bootstrapSecret=$NEW
（旧串已失效；浏览器里存过旧串的标签页会显示 Incorrect password，用上面这条打开一次即可）
EOF
exit $rc
