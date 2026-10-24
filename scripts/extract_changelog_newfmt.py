#!/usr/bin/env python3
"""把 src/main.py 里挂在 VERSION 上的逐版根因注释**抽取**到 CHANGELOG.md，
并把搬走的内容从 main.py **裁剪掉**（只留当前版本）。

【为什么需要这个脚本】
`scripts/extract_changelog.py`（2026-10-05 写）只认**旧标记格式**：
    # ── 以下为 v0.13.93 的根因（保留供追溯，非本版条目）──
而 2026-10-24 实测：main.py 里该标记只剩 6 处（L974/986/1014/1048/1085/1115），
其余 **56 处换成了新格式**（`#   v0.13.148：…` 顶格 + 缩进续行）。
⇒ 旧脚本在新格式段落前就停住，**v0.13.94~v0.13.148 共 56 版全留在 main.py 里**
（实测 main.py 142KB / 1734 行，其中注释 1157 行 = 66%）。
本脚本补齐新格式，并把**搬走的内容从 main.py 删掉**——旧脚本只写 CHANGELOG 不删源，
所以「搬完 main.py 也不变小」正是它一直没人用的原因。

【块边界怎么定】
从 `VERSION = "…"` 行之后开始，到**第一个既不是注释也不是空行**的行为止。
（`VERSION` 行本身以 `VERSION` 开头而非 `#`，若按「非注释即结束」判会抽到 0 个块 ——
这是旧脚本 docstring 已记的坑，此处沿用同一判据。）

【条目怎么切】
两种格式各算一条：
  ① 新格式：`#   v0.13.148：…`（`#` + ≥1 空格 + `v` + 版本号 + `：`/`:`）
  ② 旧格式：`# ── 以下为 v0.13.93 的根因…──`
一条的内容 = 该标题行起，到下一个标题行前（或块末）的所有非空行。

【搬运原则：逐字，不改一个字】
只去掉行首的注释对齐空格与 `#` 前缀；正文一字不改、不重排、不删减。
（第一版曾把每行包成 Python list 的 repr 写进文件，已回滚重做。）

【裁剪的安全阀】
裁剪前**逐条校验**：每个待搬版本的正文行必须能在 CHANGELOG 里逐行找到，
否则**中止且不写 main.py**。⇒ 「内容丢了」这件事不可能静默发生。

【幂等】
CHANGELOG 里已存在的版本号会跳过；main.py 里已无历史条目时直接返回 0。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "src" / "main.py"
CHANGELOG = ROOT / "CHANGELOG.md"

MARK_OLD = re.compile(r"──\s*以下为\s+(v[\d.]+)\s*的根因")
MARK_NEW = re.compile(r"^#\s+(v[\d.]+)\s*[：:]")

#: main.py 里同一版本可能有**两行标题**：一行短标题（一句话）＋一行完整标题。
#: 实测 2026-10-24：v0.13.123 / v0.13.122 / v0.13.118 / v0.13.116 各有两行
#: （如 `#   v0.13.123：P2 A10` 紧跟 `#   v0.13.123：P2 第 7/9 项补位（…）`）。
#: 若把两行都当条目头 ⇒ 生成两个同名 `## vX.Y.Z` 段 ⇒ 撞 `test_changelog_drift`
#: 的「纯版本号标题不得重复」闸门。⇒ 同版本的连续标题行**合并为一条**。
#: 判据：标题行后紧邻的另一行若也是**同版本**标题 ⇒ 并入本条（不是新条）。
SAME_VER_NEXT = re.compile(r"^#\s+(v[\d.]+)\s*[：:]")


def current_version() -> str:
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', MAIN.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else ""


def strip_comment(line: str) -> str:
    """去掉行首注释对齐空格与 `#` 前缀，正文一字不动。"""
    s = line.strip()
    if s.startswith("#"):
        s = s.lstrip("#").strip()
    return s


def split_entries(lines, start, end):
    """把 [start, end) 的注释行切成 [(version, [行…])]，按出现顺序（新 → 旧）。

    同版本连续标题行合并为一条（见 SAME_VER_NEXT 的说明）。
    """
    heads = []          # (idx, version)
    for i in range(start, end):
        ln = lines[i]
        m = MARK_NEW.match(ln)
        if m:
            heads.append((i, m.group(1)))
            continue
        m = MARK_OLD.search(ln)
        if m:
            heads.append((i, m.group(1)))
    # 合并同版本连续标题：若 heads[k+1] 与 heads[k] 同版本且两行相邻，
    # 则 heads[k+1] 不是新条目，删掉它（其内容自然并入 heads[k] 的正文区间）。
    merged = []
    for k, (i, ver) in enumerate(heads):
        if merged and merged[-1][1] == ver and i == merged[-1][0] + 1:
            continue          # 同版本相邻标题 ⇒ 并入前一条
        merged.append((i, ver))
    entries = []
    for k, (i, ver) in enumerate(merged):
        j = merged[k + 1][0] if k + 1 < len(merged) else end
        body = [ln for ln in lines[i:j] if ln.strip()]
        entries.append((ver, body))
    return entries


def main() -> int:
    text = MAIN.read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        vi = next(i for i, l in enumerate(lines) if l.startswith("VERSION = "))
    except StopIteration:
        print("✗ main.py 里找不到 `VERSION = ` 行")
        return 2

    # 块结束：VERSION 之后第一个既不是注释也不是空行的行
    end = None
    for i in range(vi + 1, len(lines)):
        if not lines[i].strip():
            continue
        if lines[i].lstrip().startswith("#"):
            continue
        end = i
        break
    if end is None:
        print("✗ main.py 的 VERSION 注释块找不到结束行")
        return 2

    entries = split_entries(lines, vi + 1, end)
    if not entries:
        print("✗ 没抽到任何版本条目 ⇒ main.py 的注释格式变了，先看 src/main.py:100-120")
        return 2

    chlog = CHANGELOG.read_text(encoding="utf-8")
    chlog_lines = set(l.strip() for l in chlog.split("\n"))

    # ── 跳过判据：**内容行**是否已在 CHANGELOG 里，不是「版本号在不在」──
    # ⚠ 2026-10-24 实踩的坑：CHANGELOG 里有 152 个 `## vX.Y.Z` 标题，但那是
    # **人工整理版**（短、有结构），main.py 里是**逐版根因原文**（长、逐字）。
    # 两个集合的版本号大量重合，但内容几乎不重合（实测：62 个条目里 61 个
    # 「版本号已存在」，却有 987 行正文在 CHANGELOG 里找不到）。
    # 若按「版本号已存在就跳过」⇒ 987 行根因原文被**静默丢弃**且看不出来。
    # ⇒ 判据必须是「这一条的正文行是否都已存在于 CHANGELOG」。
    #
    # ⚠ 幂等性（2026-10-24 二次实踩）：**当前版本永不搬**。
    #   当前版本的条目是「本版根因」，它本来就该留在 main.py；
    #   若也搬走，第二次跑时 main.py 里只剩指针 ⇒ 指针行不在原正文里
    #   ⇒ 内容判据说「不在 CHANGELOG」⇒ 又搬一次 ⇒ 每次跑都往 CHANGELOG
    #   追加一段 v0.13.148 ⇒ 撞「纯版本号标题不得重复」闸门。实测复现。
    cur = current_version()
    cur_key = "v" + cur
    todo = []
    skipped = []
    for v, body in entries:
        if v == cur_key:
            skipped.append(v + "(当前版本，留在 main.py)")
            continue
        payload = [strip_comment(l) for l in body if l.strip()]
        if payload and all(s in chlog_lines for s in payload):
            skipped.append(v)
            continue
        todo.append((v, body))
    print("抽到 %d 个版本条目（块 L%d..L%d）；不搬的 %d 个；待搬 %d 个"
          % (len(entries), vi + 2, end, len(skipped), len(todo)))
    if skipped:
        print("  （不搬）%s" % ", ".join(skipped[:6])
              + (" …" if len(skipped) > 6 else ""))
    if not todo:
        print("幂等：无需改动")
        return 0

    sections = []
    for v, body in todo:
        sections.append("## %s\n\n%s\n" % (v, "\n".join(strip_comment(l) for l in body)))

    m = re.search(r"^---\s*$", chlog, re.M)
    if not m:
        print("✗ CHANGELOG 里找不到头部 --- 分隔线，插不进去；人工确认格式")
        return 2
    head, tail = chlog[:m.end()], chlog[m.end():]
    new_chlog = head + "\n" + "\n".join(sections) + "\n" + tail.lstrip("\n")

    # ── 安全阀：每个待搬版本的正文行必须能在新 CHANGELOG 里逐行找到 ──
    new_lines = set(l.strip() for l in new_chlog.split("\n"))
    missing = []
    for v, body in todo:
        for l in body:
            s = strip_comment(l)
            if s and s not in new_lines:
                missing.append((v, s))
    if missing:
        print("✗ 安全阀拦截：%d 行在写入后找不到 ⇒ 中止，main.py 未改" % len(missing))
        for v, s in missing[:5]:
            print("   %s: %s" % (v, s[:90]))
        return 2

    # ── 安全阀二：被跳过的版本，其正文行也必须真在 CHANGELOG 里 ──
    # （第一条安全阀只管「本次要搬的」，管不到「被跳过的」——
    #   而 2026-10-24 实踩的正是后者：跳过逻辑本身错了。）
    # ⚠ 当前版本**故意不搬**（它要留在 main.py）⇒ 不参与本安全阀。
    lost = []
    for v, body in entries:
        if v == cur_key:
            continue
        for l in body:
            s = strip_comment(l)
            if s and s not in new_lines:
                lost.append((v, s))
    if lost:
        print("✗ 安全阀二拦截：%d 行在写入后于 CHANGELOG 找不到（涉被跳过版本）"
              " ⇒ 中止，main.py 未改" % len(lost))
        for v, s in lost[:5]:
            print("   %s: %s" % (v, s[:90]))
        return 2

    CHANGELOG.write_text(new_chlog, encoding="utf-8")

    # ── 裁剪 main.py：只保留「当前版本」条目 + 一条归档指针 ──
    keep = [(v, b) for v, b in entries if v == cur_key]
    if not keep:
        print("⚠ 当前版本 %s 在注释块里没有条目 ⇒ main.py 不裁剪（CHANGELOG 已写入）"
              % cur_key)
        n_lines = sum(len(b) for _, b in todo)
        print("✓ 已写入 %d 个版本段（逐字搬运 %d 行）" % (len(todo), n_lines))
        return 0

    pointer = [
        "# ── v%s 及更早的逐版根因已于 2026-10-24 归档至 ──" % cur,
        "# ── CHANGELOG.md（脚本 scripts/extract_changelog.py + scripts/"
        "extract_changelog_newfmt.py 逐字搬运）──",
        "# 此处**只保留当前版本**的根因，避免两份会各自漂移的副本。",
    ]
    # 保留当前版本的**原始注释形态**（含 # 前缀与对齐）
    keep_lines = [l for l in keep[0][1] if l.strip()]
    new_block = keep_lines + pointer
    new_lines_all = lines[:vi + 1] + new_block + lines[end:]
    MAIN.write_text("\n".join(new_lines_all), encoding="utf-8")

    n_lines = sum(len(b) for _, b in todo)
    print("✓ 已写入 %d 个版本段（逐字搬运 %d 行）" % (len(todo), n_lines))
    print("✓ main.py 已裁剪：块 %d 行 → %d 行（保留当前版本 %s + 归档指针 3 行）"
          % (end - vi - 1, len(new_block), cur_key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
