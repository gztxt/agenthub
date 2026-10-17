#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `技术文档/skills/` 下的技能包**登记**进 `skill_assets`（A9 的 owner/version/scope/digest）。

起因：`src/main.py` 的已知缺口 —— `skill_assets` 表生产为 0 行，而盘上有 18 个包
⇒ `release.py::_dim_skills` 恒为 0 条（读数如实，但覆盖面为零）。

【本脚本只登到 `draft`，不推进状态机】—— 这条不是偷懒，是口径：

  · `published` 是本模块**唯一可被依赖**的态（`skill_assets.DEPENDABLE`），
    而盘上这些包的许可证状况由 `skills/PROVENANCE.md` 统管：16 个第三方包里
    **1 个有许可证文本（Anthropic 专有条款）、15 个未确认**，该台账第四节明写
    「随 agent-hub / 技术文档仓对外分发 —— 禁止」。
  · 把「来源/许可证说不清」的包推进到 `published`，等于给它盖「可依赖」的章，
    这正是 `admission.py` 第 1 阶段（source）要拦的事。**登进台账 ≠ 准它入役。**
  · 状态机的四段 draft→tested→reviewed→published 本来就该逐包走，不允许跳级
    （`transition_reason()` 直接拒 draft→published）。

【幂等】已存在的行只更新 owner/version/scope/note，**不动 status/path/digest**
（`upsert()` 的 ON CONFLICT 本来也不写这三列）⇒ 别人手工推进过的资产不会被本脚本打回 draft。

【两次看见的包】`agent-browser-cli`（frontmatter `name: agent-browser`）与
`web-anti-crawl-search`（`name: web-search`）的目录名与 `name` 不一致。
slug 取**目录名**（与 `skill.package_digest()` 的路径口径、`skills/PROVENANCE.md` 的表一致），
`name` 差异原样写进 note —— 不自动改名，改名会让既有引用静默断掉。

【版本】`1.0.0` 是**首次登记占位**，不是声称上游发布过 1.0.0；
frontmatter 自带 `version:` 的（`summarize` / `tencent-docs`）才取真值。

用法（**默认 dry-run**，`--apply` 才写）：

    DATA_DIR=<影子目录> venv/bin/python scripts/skill_assets_register.py            # 预览
    DATA_DIR=<影子目录> venv/bin/python scripts/skill_assets_register.py --apply    # 落影子
    venv/bin/python scripts/skill_assets_register.py --apply --db <生产 agents.db>  # 落生产

`--db` 优先；不给 `--db` 时走 `config.Config().db_path`（受 `DATA_DIR` 环境变量影响，
见 `config.py` 的 `ENV_OVERRIDABLE`）。**默认必须打到生产时先预览、再落影子、最后落生产。**
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

SKILLS_ROOT = pathlib.Path("/fs/1000/ftp/技术文档/skills")

#: 台账 PROVENANCE.md 第四节：本机自用允许，对外分发/打包进公开仓禁止。
LICENSE_NOTE = ("许可证：见 skills/PROVENANCE.md 台账（未确认项不得对外分发）；"
                "登记为 draft 表示「在账上」，不等于「可依赖」")
#: 首次登记占位版本。**不是**上游发布过 1.0.0 —— 见模块 docstring「版本」。
PLACEHOLDER_VERSION = "1.0.0"
OWNER = "techdocs/skills"

#: 自研（不在 PROVENANCE.md 的 16 个第三方包里），来源可追。
SELF_AUTHORED = {"unified-memory", "agent-dispatch"}


def _frontmatter(path: pathlib.Path) -> dict:
    """只取顶层标量键（够读 name/version/description）。解析失败 ⇒ 空 dict（不猜）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if not ln or ln[0].isspace() or ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def discover(root: pathlib.Path = SKILLS_ROOT):
    """返回 [(slug, pkg_path, fm)]，按 slug 排序；**没有 SKILL.md 的目录不算技能包**。"""
    if not root.is_dir():
        raise SystemExit("技能根不存在：%s" % root)
    items = []
    for p in sorted(root.iterdir(), key=lambda x: x.name):
        skill_md = p / "SKILL.md"
        if not p.is_dir() or not skill_md.is_file():
            continue
        fm = _frontmatter(skill_md)
        items.append((p.name, p, fm))
    return items


def row_for(slug: str, pkg: pathlib.Path, fm: dict) -> dict:
    """单个包 → upsert 参数 + 需要人看的告警。纯函数（L0 可测）。"""
    warns = []
    version = (fm.get("version") or "").strip()
    if not version:
        version = PLACEHOLDER_VERSION
        warns.append("版本无出处，用占位 %s" % PLACEHOLDER_VERSION)
    name = (fm.get("name") or "").strip()
    if name and name != slug:
        warns.append("frontmatter name=%r 与目录名不符（slug 取目录名，与 manifest/摘要口径一致）" % name)
    if not fm:
        warns.append("frontmatter 解析为空")
    bits = ["登记自 %s" % pkg, LICENSE_NOTE]
    if slug in SELF_AUTHORED:
        bits.append("自研（不属 PROVENANCE.md 的第三方 16 项，来源可追）")
    if warns:
        bits.append("｜".join(warns))
    return {"slug": slug, "owner": OWNER, "version": version, "scope": "global",
            "note": "；".join(bits), "warns": warns, "pkg": str(pkg)}


def _register(target: pathlib.Path, root: pathlib.Path, apply: bool = True) -> list:
    """核心：一次性把 `root` 下所有技能包 upsert 进 `target`，返回逐包记录。

    **调用前必须已 `db.init_db(target)`**（本函数不碰全局连接 —— 那会被
    `release._ensure_db` 判为「重指」，进程内把 tmp 库换成生产库）。
    """
    import skill
    import skill_assets

    skill_assets.ensure_schema()
    rows = [row_for(slug, pkg, fm) for slug, pkg, fm in discover(root)]
    out = []
    for r in rows:
        d = skill.package_digest(r["pkg"])
        cur = skill_assets.get(r["slug"])
        rec = {
            "slug": r["slug"], "version": r["version"],
            "prev_status": (cur or {}).get("status"),
            "pkg_digest": d.get("digest"), "pkg_files": d.get("files"),
            "pkg_bytes": d.get("bytes"), "pkg_error": d.get("error"),
            "warns": r["warns"],
        }
        if apply:
            got = skill_assets.upsert(r["slug"], r["owner"], r["version"], r["scope"],
                                      note=r["note"], by_whom="script/skill_assets_register")
            rec["status"] = got["status"]
        out.append(rec)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把技能包登记进 skill_assets（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真写入（默认只预览）")
    ap.add_argument("--db", default=None, help="直接指定 sqlite 路径（优先于 DATA_DIR）")
    ap.add_argument("--root", default=str(SKILLS_ROOT), help="技能根（默认 技术文档/skills）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    a = ap.parse_args(argv)

    import db
    import config

    if db.is_open():
        # 进程内重复 init 会重指全局连接（见 release._ensure_db 的 docstring）
        print("[register] 已存在 db 连接，拒绝重新指向（本脚本只做冷启动场景）", file=sys.stderr)
        return 2
    target = pathlib.Path(a.db) if a.db else config.Config().db_path
    if not a.apply:
        print("[register] dry-run：目标库 %s（不存在则建表但不写入）" % target)
    else:
        print("[register] apply：目标库 %s" % target)

    db.init_db(target)
    out = _register(target, pathlib.Path(a.root), apply=a.apply)

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("%-30s %-10s %-10s %-10s %s" % ("slug", "version", "prev", "digest", "备注"))
        for r in out:
            print("%-30s %-10s %-10s %-10s %s" % (
                r["slug"], r["version"], r["prev_status"] or "-",
                (r["pkg_digest"] or "-")[:10],
                "；".join(r["warns"]) or ""))
    n_bad = [r for r in out if r["pkg_error"]]
    if n_bad:
        print("\n[register] %d 个包整包摘要算不出来：%s"
              % (len(n_bad), ", ".join(r["slug"] for r in n_bad)), file=sys.stderr)
        return 1
    print("\n[register] %s %d 个资产（全部停在 draft，未推进状态机）"
          % ("已登记" if a.apply else "将登记", len(out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
