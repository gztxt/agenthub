#!/usr/bin/env bash
# selfcheck_intake.sh — 修复/升级「综合检索 · 参考情报」后**必须真的调用本模块**的自检。
#
# 为什么要有这个脚本（2026-10-22 用户裁定）：
#   接单前置（intake）默认 `deep=off`，参考库固定不查 ⇒ 以往所谓「自检」只验证了记忆/知识库/技能
#   三路，参考库一次都没被调用过。改完本模块只跑单测、只看代码，等于没验证它到底还能不能用。
#   判据：**服务真被调一次、结果真的回来**，才算逻辑通；否则本模块就是摆设。
#
# 三层判据（任何一层 FAIL 即整体 FAIL）：
#   0. 服务跑的就是磁盘上的代码（`git_sha_boot` == HEAD）—— 否则自检测的是旧代码，是假绿。
#   1. 快路：`POST /api/intake/task`（escalate=off）能出简报，五段齐全、判定不是 degraded。
#   2. 参考库：强制外扩（`escalate=always`）必走参考库那一级，要求 `external.ref.card_id`
#      非空且 `state ∉ {failed, unavailable}`。走的是**8s 提前取卡**那条路 —— 正是
#      `ae79221` 修的 bug（8s 时卡未落库被吞成空、去重分支丢卡号）的回归闸。
#      `--deep` 再补一层：等到完整卡（20~90s），要求实现桶非空。
#
# 成本：第 2 层 20~90s + GitHub 额度。**只在改动涉及本模块时自动跑**（未推送区间动了
#   `src/intake.py` / `src/refscout.py` 等），无关改动只跑第 0+1 层（<1s）。
#
#   bash scripts/selfcheck_intake.sh              # 自动：改过本模块就跑参考库（8s 提前取卡）
#   bash scripts/selfcheck_intake.sh --ref        # 强制跑参考库
#   bash scripts/selfcheck_intake.sh --deep       # 参考库再等完整卡（20~90s）
#   bash scripts/selfcheck_intake.sh --fast       # 只跑快路（记为「参考库未验证」）
#   bash scripts/selfcheck_intake.sh --allow-stale # 跳过版本判据（明示：结论不代表新代码）
#   bash scripts/selfcheck_intake.sh --json       # 机器可读
#
# 退出码：0 全过；1 有 FAIL；2 hub 不可达 / 无凭据。
set -u

REF_MODE="auto"; DEEP=0; ALLOW_STALE=0; JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)          REF_MODE="on"; shift ;;
    --deep)         REF_MODE="on"; DEEP=1; shift ;;
    --fast)         REF_MODE="off"; shift ;;
    --allow-stale)  ALLOW_STALE=1; shift ;;
    --json)         JSON=1; shift ;;
    -h|--help)      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
  esac
done

if locale -a 2>/dev/null | grep -qix -e 'C.UTF-8' -e 'C.utf8'; then
  export LC_ALL=C.utf8
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HUB="${HUB_URL:-http://127.0.0.1:${PORT:-3102}}"
TOKEN="${TERM_TOKEN:-${HUB_PASSCODE:-}}"
if [ -z "$TOKEN" ] && [ -r "$REPO/.env" ]; then
  TOKEN=$(grep -m1 '^TERM_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2-)
fi
if [ -z "$TOKEN" ]; then
  echo "FAIL: 无凭据（TERM_TOKEN / HUB_PASSCODE 未设置，且读不到 agenthub/.env）" >&2
  exit 2
fi

HEALTH=$(curl -sf -m 5 "$HUB/health" 2>/dev/null) || {
  echo "FAIL: hub 不可达（$HUB/health）——自检必须先有在跑的服务" >&2; exit 2; }

HEAD_SHA=$(cd "$REPO" && git rev-parse --short=8 HEAD 2>/dev/null || echo "")

# 自动判定：未推送区间是否动过本模块
if [ "$REF_MODE" = "auto" ]; then
  CHANGED=$(cd "$REPO" && git diff --name-only origin/master..HEAD 2>/dev/null \
            | grep -cE '^(src/(intake|refscout)\.py|tests/(test_intake|test_refscout)|scripts/selfcheck_intake\.sh)')
  [ "${CHANGED:-0}" -gt 0 ] && REF_MODE="on" || REF_MODE="off"
fi

post() {  # post <json-body> <timeout>
  curl -s -m "$2" -X POST "$HUB/api/intake/task" \
    -H 'Content-Type: application/json' -H "x-hub-token: $TOKEN" -d "$1" 2>/dev/null
}

FAST_BODY='{"task":"自检：接单前置四查能否正常出简报","caller":"selfcheck","escalate":"off","auto_deep":false}'
# 升级路径：why=off ⇒ `_escalate` 里必调 `_ref(..., 8s, early_card=True)`
REF_BODY='{"task":"服务健康探针与自动看护","caller":"selfcheck","deep":false,"auto_deep":false,"escalate":"always"}'
# 完整卡：deep 必查，等到 done
DEEP_BODY='{"task":"服务健康探针与自动看护","caller":"selfcheck","deep":true,"auto_deep":true,"escalate":"off"}'

FAST_RESP=$(post "$FAST_BODY" 60)
if [ -z "$FAST_RESP" ]; then echo "FAIL: 快路端点无响应（超时 60s）" >&2; exit 1; fi
REF_RESP=""; REF_RESP2=""
if [ "$REF_MODE" = "on" ]; then
  if [ "$DEEP" = "1" ]; then
    REF_RESP=$(post "$DEEP_BODY" 240)
  else
    # 第二次立刻再提同一模块：进程内还在跑 ⇒ 走 `submit()` 的去重分支。
    # 旧实现在这里只回 `{job_id, deduped}`，卡号丢成 null（`ae79221` 修的正是这个），
    # 所以「同任务第二单也有卡号」是本模块可用性的一部分，必须一起验。
    REF_RESP=$(post "$REF_BODY" 60)
    REF_RESP2=$(post "$REF_BODY" 60)
  fi
fi

HEALTH="$HEALTH" HEAD_SHA="$HEAD_SHA" \
FAST_RESP="$FAST_RESP" REF_RESP="$REF_RESP" REF_RESP2="$REF_RESP2" \
REF_MODE="$REF_MODE" DEEP="$DEEP" \
ALLOW_STALE="$ALLOW_STALE" JSON_OUT="$JSON" python3 - <<'PY'
import json, os, sys

fails, notes = [], []
ok = lambda m: notes.append(("PASS", m))
bad = lambda m: (notes.append(("FAIL", m)), fails.append(m))

head = os.environ["HEAD_SHA"]
h = json.loads(os.environ["HEALTH"])
boot = (h.get("git_sha_boot") or "")[:8]
fp_boot, fp_wt = h.get("code_fp_boot") or "", h.get("code_fp_worktree") or ""
# ── 0. 运行的就是磁盘上的代码 ────────────────────────────────
# 判据用**内容指纹**而不是 sha：先改文件、再重启、最后提交的顺序下 sha 必然不等，
# 但那时跑的就是修好的代码（2026-10-22 实测：boot a3a3522 / HEAD ae79221，指纹却相等）。
if not fp_boot or not fp_wt:
    bad(f"拿不到代码指纹（boot={fp_boot or '空'} worktree={fp_wt or '空'}）——无法证明跑的是新代码")
elif fp_boot == fp_wt:
    ok(f"运行代码 == 磁盘代码（指纹 {fp_boot[:12]}；boot sha {boot or '?'} / HEAD {head or '?'}）")
elif os.environ["ALLOW_STALE"] == "1":
    ok(f"运行代码 ≠ 磁盘代码（boot {fp_boot[:12]} / wt {fp_wt[:12]}），但 --allow-stale 已跳过 ⇒ 只代表旧代码")
else:
    bad(f"运行代码 ≠ 磁盘代码（boot {fp_boot[:12]} / wt {fp_wt[:12]}）⇒ 改完没重启，自检会测旧代码（假绿）")

# ── 1. 快路 ──────────────────────────────────────────────────
try:
    fast = json.loads(os.environ["FAST_RESP"])
except Exception as e:
    fast = None
    bad(f"快路响应不是 JSON（{e}）")

if fast is not None:
    brief = fast.get("brief") or ""
    missing = [s for s in ("## 0.", "## 1.", "## 2.", "## 3.", "## 4.") if s not in brief]
    verdict = (fast.get("plan") or {}).get("verdict", "")
    # ⚠️ `skill.ok=False` 是「本地没匹配到技能」，属正常 0 命中；只有带 `error` 才是那一路挂了。
    dead = [k for k in ("memory", "kb", "skill")
            if isinstance(fast.get(k), dict) and fast[k].get("error")]
    if missing:
        bad(f"简报缺段：{missing}（模块没真的出简报）")
    elif dead:
        bad(f"快路有路挂了：{dead} —— 这是故障，不是「没资料」")
    elif verdict not in ("ready", "thin"):
        bad(f"判定异常：verdict={verdict!r}（预期 ready/thin；degraded 说明有路挂了）")
    else:
        ok(f"快路出简报：verdict={verdict} 用时 {fast.get('elapsed_s')}s，五段齐全")

# ── 2. 参考库 ────────────────────────────────────────────────
if os.environ["REF_MODE"] != "on":
    notes.append(("SKIP", "参考库未验证（--fast 或本次改动未涉及本模块）——这一层不算过，只是没跑"))
else:
    try:
        resp = json.loads(os.environ["REF_RESP"])
    except Exception as e:
        resp = None
        bad(f"参考库响应不是 JSON（{e}）")
    if resp is not None:
        deep_mode = os.environ.get("DEEP") == "1"
        if deep_mode:
            box, label = resp.get("ref") or {}, "deep"
            if resp.get("deep_reason") != "manual":
                bad(f"deep_reason={resp.get('deep_reason')!r}，不是 manual —— 参考库没被触发")
        else:
            ext = resp.get("external") or {}
            box, label = ext.get("ref") or {}, "升级"
            if not ext.get("triggered"):
                bad(f"外扩未触发：{ext.get('reason') or '无原因'} —— 参考库那一级根本没走到")
        state = box.get("state") or ("ok" if box.get("ok") else "unavailable")
        cid = box.get("card_id")
        if state in ("failed", "unavailable"):
            bad(f"参考库[{label}] state={state}：{box.get('error') or box.get('reason') or '无原因'}")
        elif not cid:
            bad(f"参考库[{label}] state={state} 但 card_id 为空 —— 卡号丢失（ae79221 的回归点）")
        elif deep_mode and not (box.get("rows") or []):
            bad(f"参考库[{label}] 拿到卡 {cid} 但实现桶为空（state={state}）")
        else:
            ok(f"参考库[{label}]已调用：state={state} 卡号={cid} 等 {box.get('waited_s')}s "
               f"实现桶 {len(box.get('rows') or [])} 条")
            # 去重分支（同一模块在飞时第二次提交）必须同样带卡号
            try:
                r2 = json.loads(os.environ.get("REF_RESP2") or "{}")
            except Exception:
                r2 = {}
            if r2:
                b2 = (r2.get("external") or {}).get("ref") or {}
                c2 = b2.get("card_id")
                if not c2:
                    bad(f"参考库去重分支丢了卡号（第二单 state={b2.get('state')}）—— 与首单 {cid} 不一致")
                elif c2 != cid:
                    bad(f"参考库两单卡号不一致：{cid} ≠ {c2}（同模块应复用同一张卡）")
                else:
                    ok(f"参考库去重分支正常：第二单复用同一卡号 {c2}")

if os.environ["JSON_OUT"] == "1":
    print(json.dumps({"ok": not fails, "checks": [{"level": l, "msg": m} for l, m in notes]},
                     ensure_ascii=False, indent=2))
else:
    for level, msg in notes:
        color = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}[level]
        print(f"  {color}{level}\033[0m  {msg}")
    print()
    print("✅ 本模块自检通过：服务真被调用且结果回来了。" if not fails
          else f"❌ 自检未过（{len(fails)} 项）。本模块当前不满足「能自动调用」这一条。")
sys.exit(0 if not fails else 1)
PY
