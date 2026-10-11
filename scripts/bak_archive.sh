#!/usr/bin/env bash
# 备份件归档并清理（默认 **dry**，只报不动；`--apply` 才真归档+删除）。
#
# 为什么要有出口：军规要求"改前必备份"，但没人管备份的归宿 ⇒ 09-24 实测本仓积压
# **341 份 / 20.5 MB**，单一目标文件最多 73 份（`templates/index.html`）。备份多到
# 分辨不出"哪份是回滚依据"时，它们与垃圾等价 —— 09-24 那次外部 `git clean` 就是把整片
# `*.bak-*` 一起收走的（连带 v0.13.16/17 的未提交工作）。
#
# 选择集与 `bak_inventory.sh` **完全一致**（两个脚本同一判据，不许各写一套）：
#   · 同一目标文件按 mtime 倒序保留最新 KEEP=3 份，超额者为候选；
#   · 文件名含 `rollback` / `pre-` 者**永不入选**（事故回滚依据）；
#   · 只匹配备份件的四种命名形态（`*.bak` / `*.bak-*` / `*.bak[0-9]` / `*.bak[0-9]-*`，
#     与 .gitignore 同源），不碰 venv/，不碰任何非备份文件。
#
# 安全序（任一步不过即中止，绝不"先删后补"）：
#   ① 候选清单 + **逐文件 sha256** 落 manifest
#   ② tar 打包到 /vol1/backups/agent-hub-bakclean-<ts>/
#   ③ **解包到临时目录，对每一个成员逐一 sha256 比对**（全量，不抽检；空壳归档视同无归档）
#   ④ 仅当 ③ 的 OK 数 == 候选数，才按 manifest 逐条 `rm -f --`
#   ⑤ 打印前后份数/字节数、归档落点、校验计数
#
#   bash scripts/bak_archive.sh            # dry：只报候选与将落点
#   bash scripts/bak_archive.sh --apply    # 真做（需用户授权；09-24 已授权 PT-20260924-14②）
#   KEEP=5 bash scripts/bak_archive.sh     # 改保留窗
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
KEEP=${KEEP:-3}
# 备份件命名的四种形态，与 .gitignore 第 6-11 行同源（判据不许各写一套）。
# 09-29 加：原只匹配 `*.bak-*`，漏掉编号命名（`bak2/bak3/bak4-claude-*`）与纯 `.bak`
# 后缀（`data/backups/` 下），实测两类共 26 份永久积压、既不在清点里也不在归档里。
#: 排除项有两类，判据各有出处（2026-10-12 补）：
#:   · `.git/` —— 备份脚本默认**不排除**它（本仓实测 `.git/*.bak` 5 个会被计入，
#:     `git config` 的备份含远端与 credential helper）。归档脚本对每个文件逐字节
#:     打包，把 git 内部件搬进备份区没有意义，且是白拿的暴露面。
#:   · `.env*` —— **凭据面**。`.env.bak-*` 与当前 `.env` 常含同一批凭据
#:     （本仓实测：3 条 key/token 指纹与现役 `.env` 逐条相同）。归档 = 把凭据复制到
#:     第二个位置，而"凭据只应存在于最小必要位置"是本机既有口径（README「凭据仅在
#:     .env(0600)」）。故它们永不入选，要清理请人工处置。
BEXPR=(-name '*.bak' -o -name '*.bak-*' -o -name '*.bak[0-9]' -o -name '*.bak[0-9]-*')
OUTROOT=${OUTROOT:-/vol1/backups}
TS=$(date +%Y%m%d_%H%M%S)
OUT="$OUTROOT/agent-hub-bakclean-$TS"

# ── 选候选（与 bak_inventory.sh 同判据）──────────────────────────────
cands=()
while IFS= read -r stem; do
  # 必须限定在 stem 自己所在目录：无目录限定的 `find . -name` 会让同名 .bak
  # 在 ./data/ 与 ./data/backups/ 各匹配一遍 ⇒ 候选清单出现重复条目（09-24 dry 跑出）。
  # 目标文件的四种备份命名都要算进来（09-29：`bak2/bak3/bak4` 编号命名原先漏匹配）。
  b=$(basename "$stem")
  mapfile -t files < <(find "$(dirname "$stem")" -maxdepth 1 \
                        \( -name "$b.bak" -o -name "$b.bak-*" -o -name "$b.bak[0-9]" -o -name "$b.bak[0-9]-*" \) \
                        -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
  n=${#files[@]}
  [ "$n" -le "$KEEP" ] && continue
  for f in "${files[@]:$KEEP}"; do
    case "$f" in *rollback*|*pre-*) continue ;; esac
    cands+=("$f")
  done
# stem 截取放宽到「从第一个 `.bak` 起全剥」（09-29）：原 `\.bak-[^/]*$` 匹配不到
# `.bak2-`/`.bak` 形态 ⇒ 那类文件的 stem 会退化成整条文件名，永远选不进候选集。
done < <(find . \( "${BEXPR[@]}" \) -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' 2>/dev/null \
          | sed 's/\.bak.*$//' | sort -u)
# 双保险：去重（保持首次出现顺序）
[ ${#cands[@]} -gt 0 ] && mapfile -t cands < <(printf '%s\n' "${cands[@]}" | awk '!seen[$0]++')

total_all=$(find . \( "${BEXPR[@]}" \) -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' 2>/dev/null | wc -l)
nc=${#cands[@]}
bytes=0; for f in "${cands[@]}"; do s=$(stat -c %s "$f" 2>/dev/null || echo 0); bytes=$((bytes+s)); done
printf '仓内 .bak 总份数=%s　候选(超出 KEEP=%d 且非 rollback/pre-)=%d　候选字节=%s (%s MB)\n' \
       "$total_all" "$KEEP" "$nc" "$bytes" "$(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1048576}')"
printf '归档落点=%s\n' "$OUT"
if [ "$nc" -eq 0 ]; then echo "无候选，无需动作。"; exit 0; fi
printf '候选样例（前 5）：\n'; printf '  %s\n' "${cands[@]:0:5}"

if [ "$APPLY" -ne 1 ]; then
  echo; echo "dry 模式：未创建目录、未打包、未删除任何文件。加 --apply 才执行。"
  exit 0
fi

# ── ① manifest ───────────────────────────────────────────────────────
mkdir -p "$OUT" || { echo "FAIL 无法创建 $OUT"; exit 1; }
printf '%s\n' "${cands[@]}" > "$OUT/files.txt"
( cd "$REPO" && sha256sum -- "${cands[@]}" ) > "$OUT/SHA256SUMS" || { echo "FAIL sha256 生成失败"; exit 1; }
nsum=$(wc -l < "$OUT/SHA256SUMS")
echo "① manifest: files.txt=$nc 行  SHA256SUMS=$nsum 行"
[ "$nsum" -eq "$nc" ] || { echo "FAIL 清单与校验和条数不符，中止（未删任何文件）"; exit 1; }

# ── ② 打包 ───────────────────────────────────────────────────────────
tar czf "$OUT/bak.tgz" -T "$OUT/files.txt" || { echo "FAIL 打包失败，中止"; exit 1; }
echo "② 打包: $(stat -c %s "$OUT/bak.tgz") 字节 → $OUT/bak.tgz"

# ── ③ 解包 + 全量逐文件校验 ───────────────────────────────────────────
V="$OUT/verify"; mkdir -p "$V"
tar xzf "$OUT/bak.tgz" -C "$V" || { echo "FAIL 解包失败 ⇒ 归档不可用，中止（未删任何文件）"; exit 1; }
ok=$(cd "$V" && sha256sum -c "$OUT/SHA256SUMS" 2>/dev/null | grep -c ': OK$')
echo "③ 校验: 解包后逐文件 sha256 比对 OK=$ok / 候选=$nc"
if [ "$ok" -ne "$nc" ]; then
  echo "FAIL 归档未通过全量校验（$ok/$nc）⇒ **不删除任何原文件**；请人工检查 $OUT"
  exit 1
fi
rm -rf "$V"   # 校验副本清掉，只留 tgz + manifest

# ── ④ 删除（按 manifest 精确路径，逐条）──────────────────────────────
del=0
while IFS= read -r f; do
  case "$f" in *rollback*|*pre-*) continue ;; esac   # 双保险：删除前再过滤一次
  [ -f "$f" ] || continue
  rm -f -- "$f" && del=$((del+1))
done < "$OUT/files.txt"

# ── ⑤ 前后对比 ───────────────────────────────────────────────────────
after=$(find . \( "${BEXPR[@]}" \) -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' 2>/dev/null | wc -l)
printf '④ 删除=%d 条　⑤ 仓内 .bak 份数 %s → %s　保留窗内(含 rollback/pre-)未动\n' "$del" "$total_all" "$after"
printf '归档=%s （tgz + files.txt + SHA256SUMS）\n' "$OUT"
[ "$del" -eq "$nc" ] && echo "结论: PASS（候选全数已归档并校验通过后才删除）" || \
  { echo "结论: WARN 删除数($del) != 候选数($nc)，请核对"; exit 2; }
