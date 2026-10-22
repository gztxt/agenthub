#!/usr/bin/env bash
# 推前闸门（只读判据 + 可选推送）。口径照抄工作区
# 《agent-knowledge/36-agent-hub历史会话续聊与仓库适配坑.md》§4b「推前闸门（每次外推必做，只读）」五步：
#   ① HEAD 跟踪文件扫高危模式
#   ② 全历史 blob 同扫（用 git grep -l 只出路径，绝不 -n 免回显）
#   ③ 用 -F 定点扫本机已知 token 值
#   ④ 确认 .env / *.bak-* 未被跟踪
#   ⑤ 推后核 .git/config 里 token 命中数必须为 0
# 外加两条：测试闸门（L0 零跳过必过；本机再加跑 L1）；
# 本模块自检（真调 /api/intake/task，杜绝「改完只看代码」的假绿，见 selfcheck_intake.sh）。
#
#   bash scripts/prepush.sh              # 只闸门，不推送（默认，安全）
#   bash scripts/prepush.sh --push       # 闸门全绿后 git push
#   HUB_SKIP_TESTS=1 bash scripts/prepush.sh
#
# 铁律：本脚本任何分支都**不得打印密钥值**，只打印命中数/文件路径。
# 注意：不装 git hook（会影响此刻正在同一仓里施工的其他会话）。要长期生效请自己执行：
#   ln -sf ../../scripts/prepush.sh .git/hooks/pre-push      （工作区许可由用户决定）
set -euo pipefail
cd "$(dirname "$0")/.."

# 每个分支的首字符用 [x] 括号写法：模式文本自身就不会再命中自己
# （原写法把 'PRIVATE' + 'KEY' 两个词按原文连续放进模式，会被检查① 报
#  HEAD:scripts/prepush.sh 自匹配 —— 这是扫描器误报而非漏报；
#  用「豁免扫描器自身」做修法会把扫描盲区本身放进去，所以改模式写法。）
PAT='[g]hp_[0-9A-Za-z]{36}|[g]ithub_pat_[0-9A-Za-z_]{20,}|[x]ox[baprs]-[0-9A-Za-z-]{10,}|[A]KIA[0-9A-Z]{16}|[s]k-[0-9A-Za-z._-]{20,}|[P]RIVATE KEY|BEGIN ([R]SA|[E]C|[O]PENSSH) [P]RIVATE KEY'
FAILS=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS + 1)); }
info() { printf '  ......  %s\n' "$*"; }

echo "=== ① HEAD 跟踪文件高危模式 ==="
# 注意：不能写 `git grep ... | head -1 | grep -q .` —— pipefail 下 head 提前闭管会让
# git grep 吃 SIGPIPE(141)，整条管道当“失败”→ 有泄露反而走 else 报 PASS（空探针）。
head_hits=$(git grep -I -l -E -e "$PAT" HEAD -- 2>/dev/null | head -20 || true)
if [ -n "$head_hits" ]; then
  bad "HEAD 跟踪文件里出现高危模式，命中文件（只列路径，内容刻意不回显）："
  printf '%s\n' "$head_hits" | sed 's/^/          /'
else
  ok "HEAD 跟踪文件零命中（模式含 ghp_ / github_pat_ / xox* / AKIA* / sk-* / 私钥头）"
fi

echo "=== ② 未推送区间的全历史 blob ==="
if git rev-parse --verify -q origin/master >/dev/null; then
  RANGE="origin/master..HEAD"
else
  RANGE="--all"
fi
info "扫描区间：$RANGE"
n_commits=$(git rev-list --count $RANGE 2>/dev/null || echo 0)
info "涉及提交数：$n_commits"
blob_hits=$(git grep -I -l -E -e "$PAT" $RANGE -- 2>/dev/null | sort -u | head -20 || true)
if [ -n "$blob_hits" ]; then
  bad "历史 blob 命中（只列 <rev>:<path>，不列内容）："
  printf '%s\n' "$blob_hits" | sed 's/^/          /'
else
  ok "历史 blob 零命中"
fi

echo "=== ③ 本机已知 token 值定点扫（-F 固定串，永不回显值） ==="
SECRETS=$(mktemp /tmp/.hub-gate.XXXXXX)      # mktemp 直接建为 600；用 -u 再重定向会按 umask 落盘（可读）
trap 'rm -f "$SECRETS"' EXIT
if [ -f .env ]; then
  # 只抽"看起来像凭据"的键值，长度 <8 的（布尔/端口之类）不入指纹表
  python3 - >"$SECRETS" <<'PY'
import pathlib
keys = ("TOKEN", "KEY", "SECRET", "PASS", "AUTH", "WEBHOOK")
out = []
for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    k, sep, v = line.partition("=")
    if not sep:
        continue
    k = k.strip().upper(); v = v.strip().strip('"').strip("'")
    if any(t in k for t in keys) and len(v) >= 8:
        out.append(v)
print("\n".join(dict.fromkeys(out)))
PY
  nkeys=$(grep -c . "$SECRETS" || true)
else
  : >"$SECRETS"; nkeys=0
fi
info ".env 中纳入指纹的凭据条数：$nkeys（值未打印，也未被任何分支读取）"
if [ "$nkeys" -gt 0 ]; then
  known_hits=$(git grep -I -l -F -f "$SECRETS" $RANGE -- 2>/dev/null | sort -u | head -20 || true)
  if [ -n "$known_hits" ]; then
    bad "本机已知凭据出现在待推历史里：<rev>:<path> 如下"
    printf '%s\n' "$known_hits" | sed 's/^/          /'
    info "处置：这些值需要轮换（已入库的历史不能靠改写掩盖）；轮换后再推。"
  else
    ok "本机已知凭据在待推历史里零命中"
  fi
else
  info "无凭据可扫（.env 缺失或无长值），此步视为通过但不等于已验证"
fi

echo "=== ④ .env / *.bak-* 必须未被跟踪 ==="
tracked_env=$(git ls-files -- .env '*.env' | tr '\n' ' ')
tracked_bak=$(git ls-files -- '*.bak-*' | tr '\n' ' ')
[ -z "$tracked_env" ] && ok ".env 未被跟踪" || bad ".env 被跟踪：$tracked_env"
[ -z "$tracked_bak" ] && ok "零个 *.bak-* 被跟踪" || bad "备份件被跟踪（$(echo "$tracked_bak" | wc -w) 个）：$(echo "$tracked_bak" | cut -c1-120)…"
ign=$(git check-ignore -q .env && echo yes || echo no)
info ".gitignore 是否覆盖 .env：$ign"

echo "=== ⑤ .git/config 与 remote URL 不得内嵌凭据 ==="
cfg_hit=$(grep -cE '://[^/@:]+:[^@/]+@|/(ghp_|github_pat_|token/)' .git/config 2>/dev/null || true)
[ "${cfg_hit:-0}" = "0" ] && ok ".git/config 内嵌凭据命中数=0" || bad ".git/config 出现 $cfg_hit 处内嵌凭据（不打印值）"
url_shape=$(git remote -v | sed -E 's#//[^@/]*@#//***@#g' | head -2 | tr '\n' ' ')
info "remote（已把可能的 user:token 糊成 ***）：${url_shape:-无}"

echo "=== ⑥ 测试闸门 ==="
if [ "${HUB_SKIP_TESTS:-0}" = "1" ]; then
  info "HUB_SKIP_TESTS=1，跳过（此项记为未验证）"
else
  L0LOG=$(mktemp /tmp/.hub-gate-l0.XXXXXX); ALLLOG=$(mktemp /tmp/.hub-gate-all.XXXXXX)
  trap 'rm -f "$SECRETS" "$L0LOG" "$ALLLOG"' EXIT
  if bash scripts/run_tests.sh hermetic-clean >"$L0LOG" 2>&1; then
    ok "L0 hermetic 在空 HOME 下全绿且零跳过"
  else
    bad "L0 闸门未过（详见 $L0LOG 末尾）"
    tail -12 "$L0LOG" | sed 's/^/          /'
  fi
  if bash scripts/run_tests.sh all >"$ALLLOG" 2>&1; then
    ok "本机全量（L0+L1）绿"
  else
    bad "本机全量未过（详见 $ALLLOG 末尾）"
    tail -12 "$ALLLOG" | sed 's/^/          /'
  fi
fi

echo "=== ⑦ 本模块自检（真调用 /api/intake/task，不只看代码） ==="
# 改过本模块（本脚本会自动判）⇒ 连参考库一起验；无关改动只验快路。
# 口径见 scripts/selfcheck_intake.sh 头部；这是「模块被真的调用过」的唯一证据。
if [ "${HUB_SKIP_SELFCHECK:-0}" = "1" ]; then
  info "HUB_SKIP_SELFCHECK=1，跳过（接单前置/参考库记为未验证，不是通过）"
else
  if bash scripts/selfcheck_intake.sh 2>&1 | sed 's/^/          /'; then
    ok "自检通过：服务真被调用且结果回来了"
  else
    bad "自检未过（上面逐项见原因）——本模块当前不满足「能被自动调用」"
  fi
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "❌ 闸门未过（$FAILS 项）。禁止推送；按上面各项处置后重跑。"
  exit 1
fi
echo "✅ 闸门全绿。"
if [ "${1:-}" = "--push" ]; then
  echo "+ git push（推后立刻复检查 .git/config）"
  git push
  after=$(grep -cE '://[^/@:]+:[^@/]+@' .git/config 2>/dev/null || true)
  [ "${after:-0}" = "0" ] && ok "推后复查：.git/config 内嵌凭据命中数仍为 0" || bad "推后 .git/config 被写入凭据（$after 处）——立即处理"
else
  echo "（未推送。确认要推：bash scripts/prepush.sh --push）"
fi
