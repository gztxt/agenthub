#!/usr/bin/env bash
# 主工作区未提交改动的自动快照。
#
# 【为什么要这个东西】2026-10-19 17:44:25，主工作区被一条外部 `git reset --hard`
# 冲掉了三个文件的未提交改动（`.git/logs/HEAD` 末行 `reset: moving to HEAD`，
# ts=1792403065）。仓内没有任何脚本会发这条命令——它来自某个 agent 的临时动作，
# 因此**拦不住**（git 没有 pre-reset 钩子，`reference-transaction` 也不在 HEAD
# 值不变时报告）。既然拦不住，就保证「丢了能找回来」：
# 每次运行把当前未提交改动铸成一个 commit，挂到 `refs/wip/auto` 并记日志。
#
# 【边界，别当成万能】
#  · `git stash create` **不含 untracked 文件**。本脚本会把这些路径如实写进日志
#    （标 `UNTRACKED`），但不打包——真丢了只能靠日志知道丢了什么。
#  · 快照是只读的：不移动工作区、不改 index，正在编辑的人无感。
#  · 无改动时直接退出，成本 < 50ms，可以放心高频跑。
#
# 用法：
#   scripts/wip_snapshot.sh [repo]        # 默认 /fs/1000/ftp/技术文档/agenthub
#   scripts/wip_restore.sh                # 列出快照并给恢复命令
set -euo pipefail

REPO="${1:-/fs/1000/ftp/技术文档/agenthub}"
cd "$REPO"

# 工作区干净就什么都不做（最常见的路径）
if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
    exit 0
fi

SHA="$(git stash create "wip-auto $(date -Is)")"
if [ -z "$SHA" ]; then
    exit 0
fi

# 挂到 ref 上，防止被 gc 回收
git update-ref refs/wip/auto "$SHA"

LOG="$REPO/.git/wip-snapshots.log"
{
    printf '%s %s refs/wip/auto\n' "$(date -Is)" "$SHA"
    # untracked 不备份，但必须留痕：否则事后只知道"丢了文件"，不知道丢了哪些
    git ls-files -o --exclude-standard 2>/dev/null | sed 's/^/    UNTRACKED /'
} >> "$LOG"

# 只留最近 2000 行，避免日志无限长
if [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
