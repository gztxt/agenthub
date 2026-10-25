#!/usr/bin/env bash
# setup_zcode_web.sh — 搭 ZCode 原生 Web 入口（hub 卡片「启动原生界面」的后端）
#
# 背景（2026-10-25 实测）：
#   ZCode 官方 CLI **发行包**自带 `zcode --web`（Web client + Agent 同一命令；
#   官方 README 明写「这个入口直接运行 Agent CLI，不经过发行包的 --web 分流」）。
#   本机 `~/.local/bin/zcode` 指的正是源码入口 `apps/zcode-cli/packages/cli/dist/zcode.cjs`
#   ⇒ `--help` 里没有 web/serve，扫描只能判 tui_only，卡片上没有原生界面按钮。
#
#   修复方式不是自造界面：用源码仓自带的官方 runner
#   `scripts/zcode-distribution/runner.mjs` 按发行包布局（bin/web/server/agent）
#   搭一个 `dist/zcode-web`，再让 hub 的 UI_RECIPES["zcode"] 调它。
#
# 幂等：已构建的 web/server 产物存在时跳过重建；ZCODE_FORCE_BUILD=1 可强制重建。
# 用法：
#   bash scripts/setup_zcode_web.sh              # 缺什么补什么 + 探活
#   ZCODE_FORCE_BUILD=1 bash scripts/setup_zcode_web.sh
set -uo pipefail

ROOT=${ZCODE_ROOT:-/fs/1000/ftp/技术文档/zcode}
DEST="$ROOT/dist/zcode-web"
PNPM=${PNPM:-pnpm}
NODE=${NODE:-node}
PROBE_PORT=${ZCODE_PROBE_PORT:-3099}

die() { echo "FAIL $*" >&2; exit 1; }

[ -d "$ROOT" ] || die "找不到 ZCode 源码仓：$ROOT"
[ -f "$ROOT/scripts/zcode-distribution/runner.mjs" ] || die "缺官方 runner.mjs（升级后路径变了？）"

# ── 1. 构建产物 ───────────────────────────────────────────────
need=0
[ -f "$ROOT/packages/web/dist/index.html" ] || need=1
[ -f "$ROOT/packages/server/dist/entry-http.js" ] || need=1
[ -f "$ROOT/apps/zcode-cli/packages/cli/dist/zcode.cjs" ] || need=1
[ "${ZCODE_FORCE_BUILD:-0}" = "1" ] && need=1

if [ "$need" = "1" ]; then
  echo "[1/3] 构建 web/server/agent（首次约 2~4 分钟）"
  cd "$ROOT" || die "cd 失败"
  $PNPM --filter @zcode/cli... build || die "cli 构建失败"
  rm -rf packages/server/dist
  $PNPM --filter @zcode/server build || die "server 构建失败"
  $PNPM --filter @zcode/web build || die "web 构建失败"
else
  echo "[1/3] 构建产物已存在，跳过（ZCODE_FORCE_BUILD=1 可强制）"
fi

# ── 2. 发行包布局（与 build-zcode.mjs 的 stageZCodePackage 同形）──────
echo "[2/3] 搭 $DEST"
mkdir -p "$DEST/bin" "$DEST/agent"
cp "$ROOT/scripts/zcode-distribution/runner.mjs" "$DEST/bin/zcode.mjs"
chmod +x "$DEST/bin/zcode.mjs"
printf '{\n  "name": "zcode-runtime",\n  "private": true,\n  "type": "module",\n  "version": "%s"\n}\n' \
  "$(python3 -c 'import json;print(json.load(open("'"$ROOT"'/package.json"))["version"])')" \
  > "$DEST/package.json"
ln -sfn ../../packages/web/dist "$DEST/web"
ln -sfn ../../packages/server/dist "$DEST/server"
ln -sfn ../../../apps/zcode-cli/packages/cli/dist/zcode.cjs "$DEST/agent/zcode.cjs"

for f in "$DEST/web/index.html" "$DEST/server/entry-http.js" "$DEST/agent/zcode.cjs"; do
  [ -e "$f" ] || die "布局缺文件：$f"
done

# ── 3. 探活（起一次、要 200 text/html、然后停）────────────────────
echo "[3/3] 探活 :$PROBE_PORT"
if ss -ltn 2>/dev/null | grep -q ":$PROBE_PORT "; then
  echo "SKIP 端口 $PROBE_PORT 已被占用，跳过探活（不影响布局）"
  exit 0
fi
mkdir -p "$ROOT/work"
log="$ROOT/work/zcode-web-probe.log"
# 不用 `timeout` 包一层：收尾要按**精确 PID**杀（铁律禁 pkill -f），
# 而 timeout 会再套一层进程，kill 后还要等它退出，实测会卡住。
# setsid 必须：ZCode runner 会接管会话/进程组，直接后台起会让**调用它的脚本**
# 卡在随后的第一条命令上（2026-10-25 实测 sleep 6 不返回，加 setsid 即恢复）。
setsid "$NODE" "$DEST/bin/zcode.mjs" --web --host 127.0.0.1 --port "$PROBE_PORT" \
  --workspace /fs/1000/ftp/技术文档 --no-open --no-token </dev/null >"$log" 2>&1 &
probe_pid=$!
ok=0
sleep 6
# 重试交给 curl 自己（shell 里套循环 + sleep 在这台机器上实测会卡住不返回；
# curl 的 --retry-connrefused 覆盖「端口还没起」的拒连阶段）。
ct=$(curl -s --noproxy '*' -m 3 --retry 8 --retry-delay 1 --retry-connrefused \
      -o /dev/null -w '%{http_code} %{content_type}' \
      "http://127.0.0.1:$PROBE_PORT/" || true)
case "$ct" in
  200*text/html*) ok=1 ;;
esac
kill "$probe_pid" 2>/dev/null || true
for _ in $(seq 1 5); do
  kill -0 "$probe_pid" 2>/dev/null || break
  sleep 0.4
done
kill -9 "$probe_pid" 2>/dev/null || true
if [ "$ok" = "1" ]; then
  echo "OK ZCode Web 探活通过（200 text/html）"
else
  echo "FAIL 探活未通过，日志尾部：" >&2
  tail -30 "$log" >&2
  exit 1
fi
