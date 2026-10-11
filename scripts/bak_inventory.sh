#!/usr/bin/env bash
# 备份件清点（**只读**，不删不移）。09-24 立：军规要求"改前必备份"，但没人管"备份的出口"，
# 结果本仓 09-24 实测积压 **340 份 / 20.5 MB**，同一目标文件最多 73 份（templates/index.html）。
# 后果不是"占地方"这一条：
#   ① 09-24 那次外部 `git clean` 把整片 `*.bak-*` 当成垃圾一起收走 —— 备份件多到无人能分辨
#      "哪份是回滚依据"时，它们与垃圾等价；真需要的回滚点会在清理中一起消失。
#   ② `git clean -xdff`（L0 影子树的标准清场步骤）不区分"我的回滚点"与"历史残留"。
# 判据（写死在本脚本里，别再靠人眼数）：
#   保留序：同一目标文件按 mtime 倒序保留最新 N=3 份；其余为"可归档"（不是"可删"——删除须用户批准）。
#   另：文件名含 rollback / pre- 者**永不进入可归档集**（它们是事故回滚依据）。
#   09-29：四种命名形态（`*.bak` / `*.bak-*` / `*.bak[0-9]` / `*.bak[0-9]-*`，与 .gitignore
#   同源）才算备份件；原只认 `*.bak-*`，编号命名与纯 `.bak` 后缀两份共 26 份连清点都进不来。
#   **本脚本与 bak_archive.sh 必须是同一判据**（其头注有同款约束），不许各写一套。
set -uo pipefail
cd "$(dirname "$0")/.."
KEEP=${KEEP:-3}
#: 排除项有两类，判据各有出处（2026-10-12 补）：
#:   · `.git/` —— 备份脚本默认**不排除**它（本仓实测 `.git/*.bak` 5 个会被计入，
#:     `git config` 的备份含远端与 credential helper）。归档脚本对每个文件逐字节
#:     打包，把 git 内部件搬进备份区没有意义，且是白拿的暴露面。
#:   · `.env*` —— **凭据面**。`.env.bak-*` 与当前 `.env` 常含同一批凭据
#:     （本仓实测：3 条 key/token 指纹与现役 `.env` 逐条相同）。归档 = 把凭据复制到
#:     第二个位置，而"凭据只应存在于最小必要位置"是本机既有口径（README「凭据仅在
#:     .env(0600)」）。故它们永不入选，要清理请人工处置。
BEXPR=(-name '*.bak' -o -name '*.bak-*' -o -name '*.bak[0-9]' -o -name '*.bak[0-9]-*')
echo "备份件清点（KEEP=$KEEP，只读）"
echo "────────────────────────────────────────────────────────────"
total=0; arch=0
while IFS= read -r stem; do
  # 限定目录：同名 .bak 可能存在于多个目录（如 ./data/ 与 ./data/backups/），
  # 无目录限定的 find 会重复计数（与 bak_archive.sh 同一修法，09-24）。
  b=$(basename "$stem")
  mapfile -t files < <(find "$(dirname "$stem")" -maxdepth 1 \
                        \( -name "$b.bak" -o -name "$b.bak-*" -o -name "$b.bak[0-9]" -o -name "$b.bak[0-9]-*" \) \
                        -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
  n=${#files[@]}; total=$((total+n))
  [ "$n" -le "$KEEP" ] && continue
  printf '%-52s 共 %3d 份' "$stem" "$n"
  for f in "${files[@]:$KEEP}"; do
    case "$f" in
      *rollback*|*pre-*) continue ;;   # 事故回滚依据，永不计数
    esac
    arch=$((arch+1))
  done
  printf '（超出保留窗 %d 份）\n' "$((n-KEEP))"
done < <(find . \( "${BEXPR[@]}" \) -not -path './venv/*' -not -path './.git/*' -not -path './.git' -not -name '.env*' 2>/dev/null \
          | sed 's/\.bak.*$//' | sort -u)
echo "────────────────────────────────────────────────────────────"
echo "总份数=$total ；超出保留窗（可归档候选，**非可删**）=$arch"
echo "归档动作需用户批准；本脚本不做任何写/删。"
