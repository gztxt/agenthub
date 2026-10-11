#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：本机项目清单（src/localprojects.py + 前端页面四件套）。

夹具：tmp 根目录里手工造 .git 目录（普通仓库 / git worktree 的 .git 文件 /
深度超限 / node_modules 内 / 点目录内），cloudcli.AUTH_DB 换到 tmp 夹具 db。
零宿主依赖：不起服务、不 import src.main、不 spawn git。

为什么这些用例必须存在：
1. **扫描剪枝**：node_modules/venv/.隐藏目录里的 .git 不是用户项目——全收会把
   依赖山当项目列表刷屏（每仓一条 ⇒ 数百条噪声）。
2. **缺根不静默**：根不存在必须进 errors（「查不了」≠「没有」四态口径，
   与 cloudcli._list_projects 同型）。
3. **双源合并**：git 与 cloudcli 各有半张图（cloudcli 有精确名/会话数，git 覆盖
   从没在 cloudcli 开过会话的目录）——去重 + cloudcli 精确名胜出是本页的名称口径。
4. **降级解耦**：cloudcli 库读不到 ⇒ git 部分照常 + errors 点名，不许整页塌掉。
5. **前端四件套**：section / 侧栏按钮（必须位于 #navTree 之上——用户点名
   「在 agents 菜单上面」）/ PAGE_LABELS / go() 懒加载钩子，缺一即静默退回总览
   或页面空白（test_kb_frontend_pages 同型判据）。
"""
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))
import tiers                       # noqa: E402

import cloudcli                     # noqa: E402
import localprojects as lp          # noqa: E402

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")


def _mkrepo(root: pathlib.Path, name: str, as_file: bool = False) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    g = d / ".git"
    if as_file:
        g.write_text("gitdir: /somewhere/else\n", encoding="utf-8")   # git worktree 形态
    else:
        g.mkdir()
        (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return d


#: v0.13.31 P1 把 /tmp 整前缀排除、P4 又剪 .cache ⇒ 夹具根两者都不能住
#: （tempfile 默认 /tmp、.cache 在 EXCLUDE_DIR_NAMES）——会被精度闸先杀光。
#: 用 ~ 下的专用可见名（不匹配任何排除规则），HUB_L0_TMP 可改。
_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))


def _mktmp(prefix: str) -> pathlib.Path:
    _L0_TMP.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        d = _L0_TMP / (prefix + str(os.getpid()) + "-" + os.urandom(4).hex())
        try:
            d.mkdir()
            # v0.13.58：登记到进程级清单，退出时统一删。此前造完从不删，
            # 实测累积 230MB/2402 个目录 ⇒ 被项目扫描当真实项目（176 vs 应 ≈44）。
            return tiers.l0_fixture_register(d)
        except FileExistsError:
            continue
    raise RuntimeError("造夹具目录失败")


def _mk_cc_db(path: pathlib.Path, rows) -> None:
    """最小 cloudcli auth.db（字段与真实库实测 schema 对齐，test_cloudcli 同型）。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY, project_path TEXT,"
                " custom_project_name TEXT, isStarred INTEGER, isArchived INTEGER)")
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, provider TEXT,"
                " project_path TEXT, isArchived INTEGER, created_at TEXT, updated_at TEXT)")
    for pid, ppath, cname, star in rows:
        con.execute("INSERT INTO projects VALUES (?,?,?,?,0)", (pid, ppath, cname, star))
    con.commit()
    con.close()


class TestScan(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("l0lp-")
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_plain_repo_collected_with_basename(self):
        d = _mkrepo(self.root, "projA")
        projects, errors, _stats = lp._scan_roots([str(self.root)])
        self.assertEqual(errors, [])
        hits = [p for p in projects if p["path"] == str(d)]
        self.assertEqual(len(hits), 1, "普通 git 仓必须被收录")
        self.assertEqual(hits[0]["name"], "projA")
        self.assertTrue(hits[0]["git"])
        self.assertFalse(hits[0]["worktree"])

    def test_derived_worktree_dropped(self):
        """P3（v0.13.31 精度收紧）：.git 文件指向已收录仓库 ⇒ 派生检出，剔除。
        用户点名「本机没那么多项目」——agent-hub-wt-* 与主仓成对出现即重复。"""
        parent = _mkrepo(self.root, "parent")
        wt = self.root / "parent-wt"
        wt.mkdir()
        (wt / ".git").write_text(
            f"gitdir: {parent}/.git/worktrees/parent-wt\n", encoding="utf-8")
        projects, _e, stats = lp._scan_roots([str(self.root)])
        paths = [p["path"] for p in projects]
        self.assertIn(str(parent), paths, "主仓必须收录")
        self.assertNotIn(str(wt), paths, "派生 worktree 必须剔除（与主仓重复）")
        self.assertGreaterEqual(stats["worktrees"], 1, "worktree 剔除量必须进 stats")

    def test_foreign_worktree_kept_flagged(self):
        """gitdir 指向收录集之外（真独立检出）⇒ 保留并标 worktree:True——
        结构判定不按目录名猜，证据说了算。"""
        other = self.tmp / "elsewhere" / "main"
        other.mkdir(parents=True)
        (other / ".git").mkdir()
        wt = _mkrepo(self.root, "wtx", as_file=True)
        (wt / ".git").write_text(f"gitdir: {other}\n", encoding="utf-8")
        projects, _e, _s = lp._scan_roots([str(self.root)])
        hits = [p for p in projects if p["path"] == str(wt)]
        self.assertEqual(len(hits), 1, "非派生 worktree 必须保留")
        self.assertTrue(hits[0]["worktree"], "必须标 worktree:True（如实呈现形态）")

    def test_dead_gitdir_dropped(self):
        """v0.13.93：gitdir 指向的路径整条不存在 ⇒ 死残留，剔除。

        2026-10-08 目录改名（agent-hub → agenthub）后家目录 14 个
        `agent-hub-wt-*` 全部命中：git 进去直接 `fatal: not a git repository`，
        却因「不在已收录仓库的 .git/ 内」被放行混进列表，把 L1 精度闸顶到 60>50。
        判据是**路径不存在**这条证据，不按目录名猜。
        """
        wt = _mkrepo(self.root, "deadwt", as_file=True)
        gone = self.root / "vanished" / ".git" / "worktrees" / "deadwt"
        (wt / ".git").write_text(f"gitdir: {gone}\n", encoding="utf-8")
        projects, _e, stats = lp._scan_roots([str(self.root)])
        paths = [p["path"] for p in projects]
        self.assertNotIn(str(wt), paths, "死 gitdir（目标不存在）必须剔除")
        self.assertGreaterEqual(stats["worktrees"], 1, "死 gitdir 剔除量必须进 stats")

    def test_hidden_and_dep_dirs_pruned(self):
        _mkrepo(self.root, ".hermes/internal")          # 点开头目录：工具内部仓，不是项目
        _mkrepo(self.root, "web/node_modules/pkg")      # 依赖目录
        _mkrepo(self.root, "web/venv/x")                # venv
        _mkrepo(self.root, "web/projC")                 # 正常两深项目
        projects, _e, _s = lp._scan_roots([str(self.root)])
        paths = [p["path"] for p in projects]
        self.assertIn(str(self.root / "web" / "projC"), paths)
        self.assertNotIn(str(self.root / ".hermes" / "internal"), paths, "点目录必须剪掉")
        self.assertNotIn(str(self.root / "web" / "node_modules" / "pkg"), paths, "node_modules 必须剪掉")
        self.assertNotIn(str(self.root / "web" / "venv" / "x"), paths, "venv 必须剪掉")

    def test_backup_and_archive_dirs_pruned(self):
        """P4（v0.13.31）：snapshots 整仓拷贝 / marketpace 缓存 / ARCHIVED- 归档
        都不是项目——扫描面直接剪（深度剪枝在父目录就收口，excluded 计数不必>0）。"""
        for rel in ("snapshots/20260718/proj", "Grok/marketplace-cache/abc",
                    "Hermes-backup/current/scripts", "git-backups/proj"):
            _mkrepo(self.root, rel)
        _mkrepo(self.root, "ARCHIVED-勿用-xxx")
        _mkrepo(self.root, "ARCHIVED-勿用-xxx/inner")   # 归档目录本身已够深度剪枝
        _mkrepo(self.root, "real")
        projects, _e, _stats = lp._scan_roots([str(self.root)])
        paths = [p["path"] for p in projects]
        self.assertIn(str(self.root / "real"), paths)
        for bad in ("snapshots", "marketplace-cache", "Hermes-backup",
                    "git-backups", "ARCHIVED-勿用-xxx"):
            self.assertFalse(any(bad in p for p in paths),
                             f"备份/归档路径必须剪掉：{bad}")

    def test_depth_beyond_max_not_collected(self):
        # 语义：根本身算第 0 层；深度 ≤ MAX_DEPTH 的仓库收录，更深不剪不收。
        # a/b/proj = 深度 3（收）；a/b/c/deep = 深度 4（不收）。
        _mkrepo(self.root, "a/b/c/deep")                # 深度 4 > MAX_DEPTH
        _mkrepo(self.root, "a/b/proj")                  # 深度 3：应收录
        projects, _e, _s = lp._scan_roots([str(self.root)])
        paths = [p["path"] for p in projects]
        self.assertIn(str(self.root / "a" / "b" / "proj"), paths)
        self.assertNotIn(str(self.root / "a" / "b" / "c" / "deep"), paths,
                         f"深度 > {lp.MAX_DEPTH} 必须剪掉")

    def test_missing_root_reported_not_silent(self):
        projects, errors, _s = lp._scan_roots([str(self.tmp / "ghost")])
        self.assertEqual(projects, [])
        self.assertTrue(errors and "不存在" in errors[0], "缺根必须点名，不许静默")


class TestMerge(unittest.TestCase):
    """git + cloudcli 合并：去重、精确名胜出、sessions/last_activity 透传、降级。
    v0.13.31 P2：cloudcli 降级为纯富化——cloudcli-only 行（无 .git 对应）只有
    「真实存在的独立 git 仓」才收，其余全剔除且逐条记 dropped。"""

    def setUp(self):
        self.tmp = _mktmp("l0lpm-")
        self.root = self.tmp / "root"
        self.root.mkdir()
        _mkrepo(self.root, "shared")                    # 双源命中
        _mkrepo(self.root, "gitonly")
        self._saved_db = cloudcli.AUTH_DB
        cc_db = self.tmp / "auth.db"
        _mk_cc_db(cc_db, [
            ("p1", str(self.root / "shared"), "共享项目自定义名", 0),
            ("p2", str(self.root / "cconly"), None, 1),        # 纯目录（无 .git）
            ("p3", str(self.root / "ghost"), "幽灵", 0),       # 盘上不存在
            ("p4", "/tmp/xyz-probe", None, 0),                 # /tmp 排除路径
        ])
        (self.root / "cconly").mkdir(exist_ok=True)             # 盘上有目录但非 git
        cloudcli.AUTH_DB = cc_db
        self._saved_roots = lp.ROOTS
        lp.ROOTS = [str(self.root)]
        self.addCleanup(self._restore)

    def _restore(self):
        cloudcli.AUTH_DB = self._saved_db
        lp.ROOTS = self._saved_roots
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _merged(self):
        git_projects, _e, _s = lp._scan_roots(lp.ROOTS)
        errors: list = []
        return lp._merge_cloudcli(git_projects, errors), errors

    def test_merge_dedup_and_precise_name_wins(self):
        (merged, dropped), errors = self._merged()
        self.assertEqual(errors, [])
        by_path = {p["path"]: p for p in merged}
        shared = by_path[str(self.root / "shared")]
        self.assertEqual(shared["name"], "共享项目自定义名", "cloudcli 自定义名必须胜出")
        self.assertTrue(shared["git"] and shared["cloudcli"])
        self.assertIn(str(self.root / "gitonly"), by_path)

    def test_cc_only_rows_dropped_with_reasons(self):
        """P2 主体：cconly（非 git 目录）/ghost（盘上无）/tmp（排除路径）
        一律剔除，且 dropped 逐条带原因。"""
        (merged, dropped), _e = self._merged()
        paths = [p["path"] for p in merged]
        self.assertNotIn(str(self.root / "cconly"), paths,
                         "无 .git 的 cloudcli 行只是「开过会话的目录」，不是项目")
        self.assertNotIn(str(self.root / "ghost"), paths, "盘上不存在的存账幽灵必须剔除")
        self.assertNotIn("/tmp/xyz-probe", paths, "/tmp 前缀必须剔除")
        joined = "\n".join(dropped)
        self.assertIn("非 git 目录", joined)
        self.assertIn("盘上已不存在", joined)
        self.assertIn("排除路径", joined)

    def test_cc_only_real_repo_outside_roots_kept(self):
        """cloudcli-only 但它是**真 git 仓**（扫描根之外的独立检出）⇒ 保留——
        P2 不是一刀切杀 cloudcli，是「有 .git 证据才收」。"""
        ext = self.tmp / "outside" / "extrepo"
        _mkrepo(self.tmp / "outside", "extrepo")
        con = sqlite3.connect(cloudcli.AUTH_DB)
        con.execute("INSERT INTO projects VALUES ('p9',?,?,?,0)",
                    (str(ext), "外部独立仓", 0))
        con.commit()
        con.close()
        (merged, _d), _e = self._merged()
        hits = [p for p in merged if p["path"] == str(ext)]
        self.assertEqual(len(hits), 1, "根外真 git 仓的 cloudcli 行必须保留")
        self.assertTrue(hits[0]["cloudcli"] and hits[0]["git"])

    def test_root_itself_cloudcli_row_dropped(self):
        """根路径自身（/home/gztxt 一类）的 cloudcli 行必须剔除。"""
        con = sqlite3.connect(cloudcli.AUTH_DB)
        con.execute("INSERT INTO projects VALUES ('p10',?,NULL,0,0)",
                    (str(self.root),))
        con.commit()
        con.close()
        (merged, dropped), _e = self._merged()
        hits = [p for p in merged if p["path"] == str(self.root)]
        self.assertEqual(hits, [], "根目录自身不许以 cloudcli 行出现")
        self.assertTrue(any("根目录自身" in x for x in dropped))

    def test_cc_sessions_and_activity_passthrough(self):
        con = sqlite3.connect(cloudcli.AUTH_DB)
        con.execute("INSERT INTO sessions VALUES ('s1','claude',?,0,?,?)",
                    (str(self.root / "shared"), "2026-09-26T10:00:00", "2026-09-26T10:00:00"))
        con.commit()
        con.close()
        (merged, _d), _ = self._merged()
        shared = next(p for p in merged if p["path"] == str(self.root / "shared"))
        self.assertEqual(shared["sessions"], 1)
        self.assertEqual(shared["last_activity"], "2026-09-26T10:00:00")

    def test_cc_down_git_survives_with_named_error(self):
        cloudcli.AUTH_DB = self.tmp / "ghost.db"        # 读不到
        (merged, _d), errors = self._merged()
        self.assertIn(str(self.root / "gitonly"), [p["path"] for p in merged],
                      "cloudcli 挂了 git 部分必须照常")
        self.assertTrue(any("CloudCLI" in e for e in errors), "降级必须点名，不许静默")


class TestEnvelope(unittest.TestCase):
    def test_limits_are_sane_constants(self):
        self.assertGreaterEqual(lp.LIMIT, 64)
        self.assertGreaterEqual(lp.MAX_DEPTH, 2)
        self.assertIn("node_modules", lp.EXCLUDE_DIR_NAMES)
        # v0.13.31 精度收紧 P1/P4：三类新闸必须就位
        self.assertIn("snapshots", lp.EXCLUDE_DIR_NAMES)
        self.assertIn("marketplace-cache", lp.EXCLUDE_DIR_NAMES)
        self.assertIn("/tmp", lp.EXCLUDE_PATH_PREFIXES)
        self.assertIn("ARCHIVED", lp.EXCLUDE_NAME_PREFIXES)

    def test_list_projects_shape(self):
        d = lp._list_projects()
        for k in ("ok", "count", "projects", "roots", "errors", "took_ms",
                  "dropped_count", "dropped"):
            self.assertIn(k, d)
        for p in d["projects"][:5]:
            for k in ("name", "path", "git", "cloudcli", "sessions",
                      "last_activity", "worktree"):
                self.assertIn(k, p)

    def test_under_excluded_rules(self):
        """P1/P4 判定函数：/tmp 前缀、备份段、ARCHIVED 前缀各案。"""
        self.assertTrue(lp._under_excluded("/tmp"))
        self.assertTrue(lp._under_excluded("/tmp/anything/deep"))
        self.assertFalse(lp._under_excluded("/home/gztxt/tmp-proj"))    # 名字像不算
        self.assertTrue(lp._under_excluded("/fs/x/snapshots/20260718/p"))
        self.assertTrue(lp._under_excluded("/fs/x/Grok/marketplace-cache/hash"))
        self.assertTrue(lp._under_excluded("/fs/x/ARCHIVED-勿用-any/p"))
        self.assertFalse(lp._under_excluded("/home/gztxt/realproj"))


class TestFrontendQuartet(unittest.TestCase):
    """静态断言：section / 侧栏按钮在 #navTree 之上 / PAGE_LABELS / 懒加载钩子成对。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_section_exists(self):
        self.assertIn('id="page-localprojects"', self.html,
                      "page-localprojects section 缺失 ⇒ go() 会静默退回总览")

    def test_sidebar_button_above_navtree(self):
        """用户点名「在 agents 菜单上面」——按钮必须在 #navTree 之前、带 data-sys。"""
        btn = self.html.find('data-sys="localprojects"')
        navtree = self.html.find('id="navTree"')
        home = self.html.find('data-sys="classroom"')
        self.assertGreater(btn, 0, "侧栏「本机项目」按钮缺失")
        self.assertGreater(navtree, 0)
        self.assertLess(btn, navtree, "「本机项目」必须位于 AGENTS 手风琴（#navTree）之上")
        self.assertGreater(btn, home, "顺序：总览 → 本机项目 → AGENTS")

    def test_page_label(self):
        self.assertIn("localprojects: '本机项目'", self.js)

    def test_lazy_hook_paired(self):
        self.assertIn("page === 'localprojects'", self.js)

    def test_loader_functions_defined(self):
        for fn in ("loadLocalProjects", "lpRenderList", "lpSelect", "lpStart",
                   "lpRenderAgents", "lpFilter" if "function lpFilter" in self.js else "lpTime"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 面板点了没反应")

    def test_dom_ids_used_by_js_exist_in_html(self):
        for dom_id in ("lpQ", "lpHint", "lpList", "lpSelName", "lpAgent",
                       "lpStartBtn", "lpMeta", "lpHidden"):
            self.assertIn(f'id="{dom_id}"', self.html,
                          f"#{dom_id} 在 JS 里被引用但 HTML 缺失 ⇒ null 崩溃")

    def test_inline_handlers_reference_real_functions(self):
        import re
        for m in re.finditer(r'onclick="(?!if\()([a-zA-Z_$][\w$]*)\(', self.html):
            fn = m.group(1)
            if fn in ("chatSend",):     # 既有页面的函数不归本测管（test_kb_frontend_pages 已覆盖）
                continue
            self.assertIn(f"function {fn}(", self.js,
                          f"HTML 引用 {fn}() 但 hub.js 未定义（点击即报错）")

    def test_agents_arrival_repaints_project_selects(self):
        """AGENTS 异步到、项目页 agent 下拉同步画 ⇒ 落地页=本机项目时下拉曾恒空
        （2026-10-11 报障：右边 agent 框空白，点 GitHub 菜单后才有）。
        loadAgents 必须在清单到位后补画两页的 agent 下拉，否则竞速一输就永久空白。"""
        i = self.js.find("async function loadAgents(")
        self.assertGreater(i, 0)
        seg = self.js[i:i + 2500]
        self.assertIn("lpRenderAgents()", seg, "loadAgents 未补画本机项目 agent 下拉")
        self.assertIn("ghRenderAgents()", seg, "loadAgents 未补画 GitHub 项目 agent 下拉")

    def test_agent_select_not_emptied_before_agents(self):
        """AGENTS 未到时不得把 select 清成 0 个 option（会塌成 46px 空白）。"""
        for fn, sel in (("lpRenderAgents", "lpAgent"), ("ghRenderAgents", "ghAgent")):
            i = self.js.find(f"function {fn}(")
            self.assertGreater(i, 0, f"{fn} 缺失")
            seg = self.js[i:i + 1200]
            self.assertIn("if (!items.length) return;", seg,
                          f"{fn} 在 agent 清单为空时会清空 #{sel} 的 option")

    def test_loaded_flags_declared_once(self):
        """懒加载标志只允许在 01 声明一处。

        v0.13.32 起 09/10 分片顶层各又写了一份 `var lpLoaded/ghLoaded = false`，
        注释声称「同名 var 是刻意冗余、赋值不会再覆盖」—— **断言是错的**：
        `var` 的初始化在 09/10 顶层执行，晚于 06 顶层的 go(lsGet('hub.page'))，
        会把 go() 刚置上的 true 重置回 false ⇒ 每次导航重跑多根扫描，
        并掩盖「落地页=本机项目时 agent 下拉恒空」的竞速故障（2026-10-11 报障）。"""
        import re
        for name in ("lpLoaded", "ghLoaded"):
            hits = re.findall(r"(?m)^\s*var %s = false\b" % name, self.js)
            self.assertEqual(len(hits), 1,
                             f"var {name} 被声明 {len(hits)} 次 ⇒ 后一处会把懒加载标志重置回 false")

    def test_every_agent_select_renderer_is_repainted_by_loadAgents(self):
        """系统不变量：任何从 AGENTS 渲染 agent 下拉的函数，都必须在 loadAgents
        拉到清单后补画一次。

        为什么做成「遍历全部分片」而不是只钉 lp/gh 两个名字：这类缺陷的形态是
        「同步渲染 + 异步数据源」，新增第三个项目页时会一模一样地再犯。
        2026-10-11 的故障就是两页都缺这个补画，靠用户报障才发现。"""
        import re
        renderers = sorted(set(re.findall(r"function (\w*RenderAgents)\(", self.js)))
        self.assertGreaterEqual(len(renderers), 2, f"预期至少两页 agent 渲染器，实得 {renderers}")
        i = self.js.find("async function loadAgents(")
        self.assertGreater(i, 0)
        seg = self.js[i:i + 2500]
        for fn in renderers:
            self.assertIn(f"{fn}()", seg, f"{fn} 未在 loadAgents 的「清单到位」处补画")

    def test_start_posts_cwd(self):
        """lpStart 必须把选中项目路径作为 cwd 传给 /api/term/sessions——本页的存在意义。"""
        i = self.js.find("async function lpStart(")
        self.assertGreater(i, 0)
        body = self.js[i:i + 800]
        self.assertIn("/api/term/sessions", body)
        self.assertIn("cwd: lpSel", body)


class TestStarHideQuartet(unittest.TestCase):
    """v0.13.32 收藏/隐藏（用户需求：每个项目后面加收藏/隐藏图标，收藏置顶、
    隐藏不显示除非勾选顶部显示框；同时去掉面板大标题）。

    静态钉四件：① 行内动作函数与图标存在；② 状态持久走 lsSet 守卫键；
    ③ 渲染排序「收藏在前」+ 隐藏过滤受 #lpHidden 控制；④ 面板不再有 <h3>。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def _panel(self):
        i = self.html.find('id="page-localprojects"')
        j = self.html.find('id="page-github"')
        return self.html[i:j if j > i else len(self.html)]

    def test_row_action_functions_defined(self):
        for fn in ("lpToggleStar", "lpToggleHide"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 图标点了没反应")

    def test_state_persisted_via_guarded_ls(self):
        """持久化必须走 lsSet 守卫键（test_ls_guard R1 禁裸 localStorage）。"""
        self.assertIn("'hub.lp.stars'", self.js)     # 键名必须出现在源码里
        self.assertIn("'hub.lp.hidden'", self.js)
        self.assertIn("lsSet(key", self.js)          # _lpSave 保存路径
        self.assertNotIn("localStorage.setItem", self.js.replace(
            "window.localStorage.setItem", ""))       # 守卫内部那处除外

    def test_star_rows_sort_first_and_hidden_filtered(self):
        i = self.js.find("function lpRenderList(")
        body = self.js[i:i + 1200]
        self.assertIn("lpStars.has", body, "渲染必须按收藏分流")
        self.assertIn("concat", body, "收藏置顶用 concat 保持稳定序")
        self.assertIn("lpHiddenSet.has", body, "隐藏行必须被过滤")
        self.assertIn("lpHidden", body, "过滤必须受顶部「显示隐藏」开关控制")

    def test_star_and_eye_icons_rendered(self):
        self.assertIn("ico('star')", self.js, "收藏图标必须用 i-star sprite")
        self.assertIn("ico('eye')", self.js, "隐藏图标必须用 i-eye sprite")
        self.assertIn('id="i-star"', self.html, "i-star sprite 必须存在")

    def test_panel_title_removed(self):
        """用户 2026-09-26 裁定：两个项目页去掉面板大标题（面包屑已示页名）。"""
        lp_panel = self._panel()
        self.assertNotIn("<h3>", lp_panel, "本机项目面板不应再有 <h3> 大标题")

    def test_toolbar_checkbox_not_stretched(self):
        """v0.13.35 用户报「勾选框宽度太大、贴不上文字」：真因不是 gap，是
        `.toolbar input { flex:1; min-width:180px }`（搜索框拉伸规则）命中了
        checkbox ⇒ 勾选框被撑成 180px 宽块，文字被推到框外。两条拉伸规则
        （基础 180px + 窄屏 140px）都必须排除 :not([type=checkbox])。"""
        self.assertIn(".toolbar input:not([type=checkbox]) { flex: 1; min-width: 180px; }",
                      self.html, "基础拉伸规则必须排除勾选框")
        self.assertIn(".toolbar input:not([type=checkbox]) { min-width: 140px; }",
                      self.html, "窄屏 140px 拉伸同样要排除勾选框")


@tiers.host_only
class TestLivePrecisionOnHost(unittest.TestCase):
    """L1 host：真机上的精度收紧结果——用户点名「79 个肯定错」，这条就是验收线。
    count 必须 ≤ 50（真实项目约 42~45）；无 /tmp 前缀；无 .git 文件 worktree 路径；
    每条 path 盘上真实存在（杀幽灵）。"""

    def test_live_result_is_precise(self):
        d = lp._list_projects()
        self.assertTrue(d["ok"])
        self.assertLessEqual(d["count"], 50,
                             f"本机真实项目不该超过 50，实得 {d['count']}（精度又回退？）")
        for p in d["projects"]:
            path = p["path"]
            self.assertTrue(os.path.isdir(path), f"幽灵目录混入: {path}")
            self.assertFalse(path.startswith("/tmp"), f"/tmp 混入: {path}")
            g = pathlib.Path(path) / ".git"
            self.assertFalse(g.is_file(),
                             f"worktree（.git 文件）混入主列表: {path}")
        self.assertGreater(d["dropped_count"], 0, "剔除量必须如实上报（信封口径）")


class TestTermCwd(unittest.TestCase):
    """term.py 的 _cwd_or_none（创建端点的 cwd 校验）。"""

    def setUp(self):
        sys.path.insert(0, str(_REPO / "tests"))
        import term                    # noqa: E402
        self.term = term
        self.tmp = _mktmp("l0tc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_blank_is_none(self):
        for blank in (None, "", "   "):
            self.assertIsNone(self.term._cwd_or_none(blank))

    def test_valid_abs_dir_returned(self):
        self.assertEqual(self.term._cwd_or_none(str(self.tmp)), str(self.tmp))

    def test_missing_dir_rejected(self):
        with self.assertRaises(ValueError):
            self.term._cwd_or_none(str(self.tmp / "nope"))

    def test_relative_rejected(self):
        with self.assertRaises(ValueError):
            self.term._cwd_or_none("relative/path")

    def test_suspicious_chars_rejected(self):
        for bad in ("/a;b", "/a|b", "/a&b", "/a$b", "/a`b", "/a\nb", "/a\x00b"):
            with self.assertRaises(ValueError):
                self.term._cwd_or_none(bad)

    def test_createin_has_cwd_field(self):
        self.assertIn("cwd", self.term.CreateIn.model_fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)
