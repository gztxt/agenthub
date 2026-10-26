"""sessions_store 单测 —— 已分层（口径见 tests/tiers.py 与 tests/README.md）：
   L0 hermetic = 只依赖纯函数/tempfile，干净机器（含 CI runner）上结论必须一致；
   L1 host     = 断了本机真实仓库形态（~/.grok ~/.claude ~/.jcode ~/.qoder ~/.hermes
                 ~/.codex 与 /fs 真目录），换机显式 SKIP + 理由，绝不静默通过。
   跑法：
     bash scripts/run_tests.sh hermetic   # 只跑 L0，以零跳过为闸
     bash scripts/run_tests.sh all        # L0 + L1
     venv/bin/python -m unittest tests.test_sessions_store -v
   历史：本文件原口径是「真磁盘只读断言（无网络、无进程、无 mock）」—— 口径不变，
   只是把离不开本机的那部分显式标出来，不让它们冒充全绿。"""
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tiers.py
import tiers                                   # noqa: E402
from src import sessions_store as ss      # noqa: E402

CWD = "/fs/1000/ftp/技术文档"


def _first_live(items):
    """取第一条「记录里的目录现在还在盘上」的历史条目，没有则 None。

    本机历史里会混进指向临时目录的会话：子代理在 /tmp 影子树上起过真 pty，
    那些 claude/grok/qoder/jcode 会话被各自 app 落进了自己的 sessions 目录，
    而影子树随后被清理掉。此时 session_cwd() 的**正确**行为是回退 fallback，
    而不是等于记录里那个已死的目录 —— 原先三个用例无条件比等于，与同文件
    test_missing_recorded_dir_falls_back 断言的回退分支直接矛盾（09-23 实测
    被一次正常清理踩爆 6 例）。
    """
    return next((i for i in items if i.get("cwd") and Path(i["cwd"]).is_dir()), None)
# 【偏差 D3】计划这一例写错了 cwd：7491a32f… 实测只存在于
# ~/.qoder/projects/-home-gztxt-agent-hub/7491a32f-….jsonl（cwd=/home/gztxt/agent-hub），
# 而 CWD 目录下 qoder 实测 0 条 *.jsonl（仅 4 个无正文骨架）⇒ known_ids("qoder", CWD) 为空，
# 与同文件的 test_qoder_empty_state_has_note 在计划里直接互斥（双校验必拒 CWD 那份）。
# 处理：按真实 cwd 断言，不放宽 resume_argv「id_re + 实盘存在」这道安全闸。
QODER_CWD = "/home/gztxt/agent-hub"


class TestMask(unittest.TestCase):
    def test_kv_secret_masked(self):
        out = ss.mask_title("入口 ?ccr_web_token=ccr-web-fixed-token-gztxt-2026 打不开")
        self.assertIn("<masked>", out)
        self.assertNotIn("ccr-web-fixed-token-gztxt-2026", out)

    def test_uuid_title_not_mangled(self):
        t = "排查 session 01a0c91d-eb84-7130-9767-479821ef336c 无响应"
        self.assertNotIn("<masked>", ss.mask_title(t))      # 含 '-' 的 UUID 刻意不糊（避免误伤）

    def test_plain_chinese_untouched(self):
        t = "agent hub 宽屏 左侧菜单 1.不要有滚动条"
        self.assertEqual(ss.mask_title(t), t)

    def test_title_length_capped(self):
        self.assertLessEqual(len(ss.mask_title("长" * 400)), 120)


class TestTable(unittest.TestCase):
    def test_agent_set(self):
        """v0.13.93：codebuddy 接入（原为 8 个，测试名 test_only_seven_agents）。
           2026-10-24：dsh 接入（终端前端是外置 TUI 插件，见判例 254）。
           加一张卡要同时动后端这张表**与**前端 TERM_HIST_AGENTS —— 漏一边的表现是
           「菜单里有这张卡、点开却没有历史下拉」（cursor 就是这个症状，
           因为前端白名单不含它 ⇒ 根本不发历史请求）。"""
        self.assertEqual(set(ss.SESSION_STORES),
                         {"grok", "claude", "qoder", "jcode", "hermes", "codex", "opencode",
                          "cursor", "codebuddy", "dsh", "omp"})

    def test_hist_agents_front_back_same_set(self):
        """hub.js 的 TERM_HIST_AGENTS 与后端 SESSION_STORES 必须同集合——
           注释里写着"同集合"却无人核对，opencode 就是漏这刀漏出来的。"""
        hub = (tiers.repo_root() / "static" / "hub.js").read_text(encoding="utf-8")
        m = re.search(r"const TERM_HIST_AGENTS = \[([^\]]*)\]", hub)
        self.assertIsNotNone(m, "hub.js 里找不到 TERM_HIST_AGENTS")
        front = {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}
        self.assertEqual(front, set(ss.SESSION_STORES), "前后端历史白名单漂移")

    def test_supports_negative(self):
        for a in ("pi", "shell", "qwenpaw", "ccr"):
            self.assertFalse(ss.supports(a))

    def test_id_regexes(self):
        st = ss.SESSION_STORES
        self.assertTrue(st["jcode"]["id_re"].match("session_seedling_1790072132294_70768ccd3fd9f542"))
        self.assertFalse(st["jcode"]["id_re"].match("sheep"))            # 动物名不唯一，禁用
        self.assertTrue(st["hermes"]["id_re"].match("20260921_221812_fa76f1"))
        self.assertTrue(st["grok"]["id_re"].match("01a0c91d-eb84-7130-9767-479821ef336c"))
        self.assertFalse(st["grok"]["id_re"].match("01a0c91d-eb84-7130-9767-479821ef336c; rm -rf /"))
        # v0.13.83 cursor：**实测证伪**留在这里当回归——我一度按「32 位无连字符 hex」
        # 写形状门，真机 61 条**全被拒**（提交时报 400「session_id 形状非法」）。
        # 真值是标准带连字符 UUID，故它复用 UUID_RE（与 grok/claude/codex 同一支）。
        self.assertTrue(st["cursor"]["id_re"].match("4a0d0eb5-8712-4d5a-bf0d-83215584ce28"))
        self.assertFalse(st["cursor"]["id_re"].match("4a0d0eb587124d5abf0d83215584ce28"))   # 无连字符 ⇒ 拒
        self.assertFalse(st["cursor"]["id_re"].match("4a0d0eb5-8712-4d5a-bf0d-83215584ce28; rm -rf /"))
        # v0.13.93 codebuddy：73/73 全是标准带连字符 UUID ⇒ 同样复用 UUID_RE。
        # 同时钉住 resume argv 的**首元素是绝对路径**：该 CLI 只在 WorkBuddy 包内、不在 PATH，
        # 写 "codebuddy" 会被 term.py 的 which() 判失败、会话拉不起来（profiles.py 同款理由）。
        self.assertTrue(st["codebuddy"]["id_re"].match("373e4969-47e2-4aa4-bf59-a0ad09279726"))
        self.assertFalse(st["codebuddy"]["id_re"].match("373e496947e24aa4bf59a0ad09279726"))
        self.assertEqual(st["codebuddy"]["resume"][1:], ["--resume", "{id}"])
        self.assertTrue(st["codebuddy"]["resume"][0].startswith("/"),
                        "codebuddy CLI 必须在 PATH 之外 ⇒ resume argv 必须写绝对路径")
        # 2026-10-24 dsh：id 是**裸 uuid**（TUI 退出提示 `--resume <uuid>`，不带 session- 前缀）。
        # 命令必须带 `--profile tui`：裸 dsh 会变成 `dsh web`（判例 252 §四.2 / 254）。
        self.assertTrue(st["dsh"]["id_re"].match("d76224bf-cad5-44ff-8a9d-4a69e563feb3"))
        self.assertFalse(st["dsh"]["id_re"].match("session-d76224bf-cad5-44ff-8a9d-4a69e563feb3"),
                         "带 session- 前缀的是仓库文件名，不是 resume id")
        self.assertEqual(st["dsh"]["resume"], ["dsh", "--profile", "tui", "--resume", "{id}"])

    def test_compress_path_matches_codebuddy_bucket(self):
        """`_compress_path` 是 dist 里 PathUtils.compressPath 的 Python 等价：
           `/ \\ :` → `-`、去首尾 `-`、合并连续 `-`。桶名对不上 ⇒ resume 定位不到会话，
           所以这层等价必须被钉死（改错一位 = 全库条目静默不可续）。"""
        self.assertEqual(ss._compress_path("/fs/1000/ftp/技术文档"), "fs-1000-ftp-技术文档")
        self.assertEqual(ss._compress_path("/home/gztxt"), "home-gztxt")
        self.assertEqual(ss._compress_path("/tmp//a///b/"), "tmp-a-b")
        self.assertEqual(ss._compress_path("/mnt/vol2/项目 目录"), "mnt-vol2-项目 目录")
        # ⚠️ 只替换 `/ \ :` 三种分隔符，**空格不动**（实测 dist 源码的正则就是
        # /[/\\:]/g）。我一度按「非字母数字全换」写期望值，被这一例当场证伪。
        self.assertEqual(ss._compress_path("C:\\Users\\gztxt\\x"), "C-Users-gztxt-x")
        self.assertEqual(ss._compress_path(""), "")

    def test_codebuddy_first_user_shape(self):
        """行形状是 `type=='message' and role=='user'`（**不是** claude 的 `type=='user'`）。
           这条钉死「为什么不能复用 `_first_user_text`」：喂它 claude 形状能出标题，
           喂 codebuddy 形状恒空 —— 后者正是必须单设 role 版抽取器的理由。"""
        cb = [{"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "修一下 hub 的历史下拉"}]},
              {"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "好"}]}]
        self.assertEqual(ss._codebuddy_first_user(cb), "修一下 hub 的历史下拉")
        self.assertEqual(ss._first_user_text(cb), "",
                         "通用抽取器对 codebuddy 形状恒空（这正是要单设 role 版的原因）")
        # 注入块要跳过，不能拿 system-reminder / 命令回显当标题
        self.assertEqual(ss._codebuddy_first_user(
            [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "<system-reminder>噪音</system-reminder>"}]},
             {"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "<user_query>真问题</user_query>"}]}]),
            "真问题")


class TestDshStore(unittest.TestCase):
    """DSH 历史（2026-10-24，判例 254）：投影缓存格式 / 空会话过滤 / 裸 uuid / resume 模板。

    全部 hermetic：把 ss.DSH_SESSIONS 指到临时目录，不碰本机 ~/.dsh。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._orig = ss.DSH_SESSIONS
        ss.DSH_SESSIONS = self.root
        ss._CACHE.clear()

    def tearDown(self):
        ss.DSH_SESSIONS = self._orig
        ss._CACHE.clear()
        self._tmp.cleanup()

    def _write(self, sid, *, title=None, first=None, cwd="/fs/1000/ftp/技术文档", age=0.0):
        import json as _json
        import os
        import time as _time
        rows = {"title": {"val": title}}
        if first is not None:
            rows["titleInput"] = {"val": {"first": {"text": first}}}
        doc = {"version": 7, "record": {"identity": {"createdAt": 1, "cwd": cwd}, "rows": rows}}
        p = self.root / f"session-{sid}.json"
        p.write_text(_json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        if age:
            t = _time.time() - age
            os.utime(p, (t, t))
        return p

    def test_items_bare_uuid_title_and_cwd(self):
        a = "11111111-1111-1111-1111-111111111111"
        b = "22222222-2222-2222-2222-222222222222"
        self._write(a, title="正式标题", cwd="/fs/1000/ftp/技术文档")
        self._write(b, title=None, first="第一条用户输入", cwd="/fs/1000/ftp/技术文档/dsh-src")
        d = ss.list_history("dsh", "/fallback", 8)
        self.assertEqual([i["id"] for i in d["items"]], [b, a], "裸 uuid + 按缓存 mtime 倒序")
        self.assertEqual(d["items"][0]["title"], "第一条用户输入", "无 title 时退首条用户输入")
        self.assertEqual(d["items"][0]["cwd"], "/fs/1000/ftp/技术文档/dsh-src")

    def test_empty_session_is_skipped(self):
        a = "11111111-1111-1111-1111-111111111111"
        self._write(a, title=None, first=None)      # 只起界面没提问
        d = ss.list_history("dsh", "/fallback", 8)
        self.assertEqual(d["items"], [])
        self.assertEqual(d["note"], "")

    def test_limit_and_resume_argv(self):
        sid = "33333333-3333-3333-3333-333333333333"
        for n in range(3):
            self._write(f"4444444{n}-4444-4444-4444-444444444444", title=f"会话{n}", age=10 - n)
        self._write(sid, title="要续的那条", age=1)
        d = ss.list_history("dsh", "/fallback", 2)
        self.assertEqual(len(d["items"]), 2)
        argv = ss.resume_argv("dsh", sid, "/fallback")
        self.assertEqual(argv, ["dsh", "--profile", "tui", "--resume", sid])
        self.assertEqual(ss.title_for("dsh", sid), "要续的那条")

    def test_unknown_id_rejected(self):
        with self.assertRaises(ValueError):
            ss.resume_argv("dsh", "session-33333333-3333-3333-3333-333333333333", "/fallback")


class TestResumeArgv(unittest.TestCase):
    @tiers.host_only          # 需要盘上真 id 才能断言（L1）
    def test_good_ids(self):
        self.assertEqual(ss.resume_argv("grok", "01a0c91d-eb84-7130-9767-479821ef336c", CWD),
                         ["grok", "--resume", "01a0c91d-eb84-7130-9767-479821ef336c"])
        self.assertEqual(ss.resume_argv("qoder", "7491a32f-ccfb-4602-bd84-22c521fd45ee", QODER_CWD),
                         ["qodercli", "-w", QODER_CWD, "-r", "7491a32f-ccfb-4602-bd84-22c521fd45ee"])
        # v0.13.82：codex 必须带 --no-alt-screen（否则续聊进来的 TUI 进备用屏，
        # 而 xterm.js 的备用屏没有 scrollback ⇒ 它的窗口永远没法上翻看历史）。
        # --no-alt-screen 是顶层选项，放子命令之前；实测两种位置都被 codex 接受。
        self.assertEqual(ss.resume_argv("codex", "01a0bffd-7df4-7692-953d-210230d73610", CWD),
                         ["codex", "--no-alt-screen", "resume", "01a0bffd-7df4-7692-953d-210230d73610"])

    def test_injection_rejected(self):
        for bad in ["x; rm -rf /", "--resume=evil", "$(id)", "../etc/passwd", "a" * 300, ""]:
            with self.assertRaises(ValueError):
                ss.resume_argv("grok", bad, CWD)

    def test_unsupported_agent(self):
        with self.assertRaises(ValueError):
            ss.resume_argv("pi", "01a0c91d-eb84-7130-9767-479821ef336c", CWD)


class TestCursorTitleSource(unittest.TestCase):
    """v0.13.96：cursor 标题取哪一源（L0 hermetic —— 只用 tempfile + mock，不读真盘）。

    事故（2026-10-08 实测）：`_t_cursor` 与 `_title_of_session` 都写「meta.title 优先」，
    而本机 99 条 meta 里 5 条有 title、**无一例外全是 Cursor 服务端生成的英文**
    （`Cursor Startup Garbled Characters` / `Top Image Bar Sticky Issue` / `Initial Greeting` …），
    对应会话的用户原话却是中文（`cursor启动会带入乱码字符`）⇒ 侧栏历史里那几条**整排显示英文**。
    meta.title 来自服务端会话列表、离线读不到也不可控，不能当主口径（D2：问题原文优先）。

    放在 L0 而不是 TestRealStores：判据是「同一个函数在两种源下各给什么」，
    与本机仓库形态无关 —— 放进 L1 会让干净机器上这条闸门静默跳过，等于没闸门。"""

    def test_cursor_title_prefers_user_text_over_english_meta_title(self):
        import tempfile
        from unittest import mock

        sid = "11111111-2222-4333-8444-555555555555"
        with tempfile.TemporaryDirectory() as td:
            # 转录：首句用户提问（中文）。行形状 = cursor 实测的 role 键 + message.content
            tr = Path(td) / "tr" / f"{sid}.jsonl"
            tr.parent.mkdir(parents=True)
            tr.write_text(json.dumps({"role": "user", "message": {"content": [
                {"type": "text", "text": "<user_query>\ncursor启动会带入乱码字符\n</user_query>"}]}},
                ensure_ascii=False) + "\n", encoding="utf-8")
            transcripts = {sid: tr}
            en_meta = {"title": "Cursor Startup Garbled Characters"}      # 服务端英文标题

            # 正对照：两条源都在 ⇒ 取中文原文（**修复前正是这条会红**）
            self.assertEqual(ss._cursor_title(sid, en_meta, transcripts), "cursor启动会带入乱码字符",
                             "有中文原文时必须用原文，不得被服务端英文标题盖掉（D2）")
            # 负对照：转录缺失 ⇒ 回落 meta.title（否则标题开天窗）
            self.assertEqual(ss._cursor_title(sid, en_meta, {}), "Cursor Startup Garbled Characters",
                             "转录不在时必须回落 meta.title")
            # 负对照：两源皆空 ⇒ 空串（「未命名会话」由调用方兜底，此处不编造）
            self.assertEqual(ss._cursor_title(sid, {"title": ""}, {}), "")
            # 本例管的是「优先哪一源」，不是「把英文翻成中文」：英文原文照原样透传
            tr.write_text(json.dumps({"role": "user", "message": {"content": [
                {"type": "text", "text": "hello in english"}]}}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            self.assertEqual(ss._cursor_title(sid, en_meta, transcripts), "hello in english")

    def test_cursor_title_falls_back_when_transcripts_absent(self):
        """不传 transcripts 字典时走按 sid 通配那条路；仓库根不存在时不得抛异常。"""
        from unittest import mock
        with mock.patch.object(ss, "CURSOR_PROJECTS", Path("/nonexistent-cursor-projects")):
            self.assertEqual(
                ss._cursor_title("11111111-2222-4333-8444-555555555555",
                                 {"title": "Server Side Title"}),
                "Server Side Title")


@tiers.host_only              # 断言本机六家仓库的真实形态 ⇒ 换机不可判定（L1）
class TestRealStores(unittest.TestCase):
    """把 spec §2 取证矩阵的数字当断言：磁盘形态变了就 FAIL，这正是我们要的信号。"""

    def test_grok_history_has_cn_title(self):
        d = ss.list_history("grok", CWD, 3)
        self.assertTrue(d["items"], "grok 在 技术文档 实测 88 条，不应为空")
        it = d["items"][0]
        self.assertLessEqual(len(d["items"]), 3)
        self.assertTrue(re.search(r"[\u4e00-\u9fff]", it["title"]), f"标题应含中文：{it}")
        self.assertNotRegex(it["title"], r"\A[0-9a-f]{4}", "标题不得是字母编号")
        self.assertIsInstance(it["ts"], int)
        self.assertTrue(it["id"])

    def test_claude_jcode_hermes_nonempty(self):
        for agent, cwd in (("claude", CWD), ("jcode", CWD), ("hermes", "/home/gztxt")):
            with self.subTest(agent=agent):
                self.assertTrue(ss.list_history(agent, cwd, 3)["items"])

    def test_sorted_desc_and_capped(self):
        items = ss.list_history("grok", CWD, 3)["items"]
        ts = [i["ts"] for i in items]
        self.assertEqual(ts, sorted(ts, reverse=True))

    def test_codex_excludes_exec_noise(self):
        """v0.13.61：`codex exec` 探针不得进历史（D5 的原意），但**不能用 source 白名单实现**。

        09-30 事故：老实现 `where source='cli'`，而 09-29 起用户实际在 IDE 扩展里开会话，
        source 记的是 `vscode` ⇒ 最新会话被整体过滤，侧栏历史停在 09-28。
        故判据锁成「排除噪音」而非「只认 cli」。"""
        import sqlite3
        db = Path.home() / ".codex" / "state_5.sqlite"
        if not db.exists():
            self.skipTest("无 ~/.codex/state_5.sqlite（非本机形态）")
        # ⚠ 判别力说明：exec 探针的 title/first_user_message **就是真实用户提问**
        # （本仓探针统一发「只回复一个字：好」，09-30 实测 74 条同款）。
        # ⇒ 内容层无法区分探针与人开的会话，唯一可靠判据是 threads.source。
        #    所以这里直接对账「结果集里一条 exec 都没有」，而不是猜标题长相。
        d = ss.list_history("codex", CWD, 20)
        self.assertLessEqual(len(d["items"]), 20)
        if not d["items"]:
            self.assertTrue(d["note"])   # 0 条必须给 note，不许静默空白
            return
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            noise = {r[0] for r in c.execute(
                "select id from threads where archived=0 and ("
                "source like 'exec%' or source like '{\"subagent\"%')")}
        leaked = [i["id"] for i in d["items"] if i["id"] in noise]
        self.assertEqual(leaked, [], f"exec 探针/子代理线程混进历史：{leaked}")
        for it in d["items"]:
            self.assertRegex(it["id"], r"\A[0-9a-fA-F-]{36}\Z", "非 UUID 会话不得进历史")

    def test_codex_shows_recent_sessions_not_only_cli(self):
        """回归锁：历史必须能取到**最近的**真实会话，不能停在某个旧日期。

        取本机 threads 库里 updated_at 最大的非 exec 线程（跳过子代理），
        断言它出现在 list_history 结果里 —— 白名单实现下此例会红。"""
        import sqlite3
        db = Path.home() / ".codex" / "state_5.sqlite"
        if not db.exists():
            self.skipTest("无 ~/.codex/state_5.sqlite（非本机形态）")
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "select id, cwd from threads where archived=0 "
                "and source not like 'exec%' and source not like '{\"subagent\"%' "
                "order by updated_at desc limit 1").fetchone()
        if not row:
            self.skipTest("本机无真实 codex 会话")
        d = ss.list_history("codex", row["cwd"] or CWD, 20)
        self.assertIn(row["id"], [i["id"] for i in d["items"]],
                      "最新真实会话必须出现在历史里（白名单 source 会漏）")

    def test_listed_codex_session_is_resumable(self):
        """v0.13.62 不变量：**列表能列出来的历史，必须点得进去**（同源闸门）。

        09-30 事故：v0.13.61 只把 _t_codex 的 source 白名单换成排除集，列表侧放行了
        IDE 扩展（source='vscode'）开的会话，但 _exists_on_disk 仍写死 `source='cli'`
        ⇒ 点「续聊」必 404 `session_id 不在实盘清单内`。列表与校验各写一套过滤口径，
        就是这个半边修复的由来。故本例拿列表自己的结果去问 resume_argv。"""
        d = ss.list_history("codex", CWD, 20)
        if not d["items"]:
            self.skipTest("本机无真实 codex 会话")
        for it in d["items"][:5]:
            with self.subTest(sid=it["id"]):
                argv = ss.resume_argv("codex", it["id"], it["cwd"] or CWD)
                # v0.13.82：期望值跟着 SESSION_STORES 的模板走（现含 --no-alt-screen），
                # 但**仍是逐字显式断言**——这条闸门护的是「真正会被 exec 的那串 argv」，
                # 不是「argv 等于模板」（后者是同义反复，等于没闸门）。
                self.assertEqual(argv, ["codex", "--no-alt-screen", "resume", it["id"]])

    def test_codex_noise_source_still_rejected(self):
        """反向闸：排除集不能被顺手放宽成「什么都收」—— exec 探针/子代理仍须 404。"""
        import sqlite3
        db = Path.home() / ".codex" / "state_5.sqlite"
        if not db.exists():
            self.skipTest("无 ~/.codex/state_5.sqlite（非本机形态）")
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            row = c.execute("select id from threads where archived=0 and ("
                            "source like 'exec%' or source like '{\"subagent\"%') limit 1").fetchone()
        if not row:
            self.skipTest("本机无 exec 探针/子代理线程")
        with self.assertRaises(ValueError):
            ss.resume_argv("codex", row[0], CWD)

    def test_hermes_note_declares_scope(self):
        d = ss.list_history("hermes", "/home/gztxt", 3)
        self.assertTrue(d["note"], "hermes 不按 cwd 过滤，note 必须写明口径")

    def test_opencode_history_from_sqlite(self):
        d = ss.list_history("opencode", CWD, 3)
        self.assertTrue(d["items"], "opencode 实测有可续会话（1.18.32 opencode.db），不应为空")
        it = d["items"][0]
        self.assertRegex(it["id"], r"\Ases_")
        self.assertNotRegex(it["title"], r"\ANew session - ", "零消息占位会话不该进历史")
        argv = ss.resume_argv("opencode", it["id"], CWD)
        self.assertEqual(argv, ["opencode", "--session", it["id"]])
        self.assertTrue(ss.title_for("opencode", it["id"]), "title_for 必须能按 id 直查")

    def test_items_carry_their_own_cwd(self):
        """跳目录之后，条目若不带自己的 cwd，前端就无从标出「这条来自哪个工程」"""
        seen_other = False
        for a in ("grok", "claude", "jcode", "qoder", "hermes", "codex", "cursor"):
            for i in ss.list_history(a, CWD, 5)["items"]:
                with self.subTest(agent=a, sid=i["id"][:10]):
                    self.assertIsInstance(i["cwd"], str)
                if i["cwd"] and i["cwd"] != CWD:
                    seen_other = True
        self.assertTrue(seen_other, "跨目录必须真命中别的目录的条目，否则本次裁定没落地")

    @tiers.host_only
    def test_cursor_history_and_resume(self):
        """v0.13.83 cursor 全链：列表 → 标题 → 续聊 argv → 存在性校验。
           cursor 的会话分散在两处（chats 主表 + projects 转录供标题），
           且行形状用 role 键而非各家的 type 键 —— 这条用例把「两处都对上」钉死。"""
        items = ss.list_history("cursor", CWD, 5)["items"]
        if not items:
            self.skipTest("本机 cursor 无历史")
        it = items[0]
        self.assertRegex(it["id"], r"\A[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z",
                         "cursor 会话 id 实测是标准带连字符 UUID（无连字符那条路已被证伪）")
        self.assertGreater(it["ts"], 0, "updatedAtMs 是毫秒，转秒后不该为 0")
        self.assertNotEqual(it["title"], "未命名会话", "标题应由转录首句用户提问抽出")
        self.assertNotEqual(it["title"], it["id"][:8], "标题不许退化成 sid 前缀（D2）")
        self.assertTrue(ss.title_for("cursor", it["id"]), "title_for 必须能按 id 直查")
        self.assertEqual(ss.resume_argv("cursor", it["id"], CWD),
                         ["cursor-agent", "--resume", it["id"]])

    @tiers.host_only
    def test_cursor_listed_implies_resumable(self):
        """**列得出来就必须点得动**（v0.13.62 codex 那条教训的同构）。

        cursor 的 `--resume <chatId>` 是按 **md5(当前 cwd)** 定位会话的；记录的 cwd 已删
        （本机 4 条 /tmp/cursorfwd）或与画像目录不同桶时，cursor **不报错**，而是静默开一条
        新会话 —— 用户点了历史条目却落进别的会话，比"没有历史下拉"更糟。
        故 _t_cursor 与 _exists_on_disk 必须用**同一套**判据（hasConversation + 桶名一致）。

        这条闸门的价值：它把「列表」与「续聊白名单」的**一致性**变成可断言的量，
        而不是两处各写一遍、各自漂移（本仓反复吃亏的正是这种半边修复）。"""
        items = ss.list_history("cursor", CWD, 20)["items"]
        if not items:
            self.skipTest("本机 cursor 无历史")
        for i in items:
            with self.subTest(sid=i["id"][:8]):
                self.assertTrue(ss._exists_on_disk("cursor", i["id"], CWD),
                                "列出来了却过不了续聊白名单 ⇒ 点了会静默开新会话")
                # argv 真拼得出来（形状门 + 实盘门都过）
                argv = ss.resume_argv("cursor", i["id"], CWD)
                self.assertEqual(argv[:2], ["cursor-agent", "--resume"])

    @tiers.host_only
    def test_cursor_session_cwd_matches_bucket(self):
        """resume 时要落到「能解析到该会话桶」的目录：session_cwd 的值必须与桶名自洽。
           记录目录还在 ⇒ 用它；已删/为空 ⇒ 退回画像目录（只有桶名恰好相符时才允许列出）。"""
        items = ss.list_history("cursor", CWD, 5)["items"]
        if not items:
            self.skipTest("本机 cursor 无历史")
        for i in items:
            with self.subTest(sid=i["id"][:8]):
                got = ss.session_cwd("cursor", i["id"], CWD)
                self.assertTrue(Path(got).is_dir(), f"续聊目录必须是真实存在的目录：{got!r}")
                bucket = next(iter(ss.CURSOR_CHATS.glob(f"*/{i['id']}/meta.json"))).parent.parent.name
                self.assertEqual(hashlib.md5(got.encode()).hexdigest(), bucket,
                                 "session_cwd 与 cursor 的桶名不自洽 ⇒ --resume 找不到该会话")

    def test_codebuddy_history_and_resume(self):
        """v0.13.93 codebuddy 全链：列表 → 标题 → 续聊 argv → 存在性校验。
           它的行形状用 `role` 键（不是各家的 `type=='user'`），桶名是 compressPath(realpath(cwd))
           而不是 slug —— 两处都与 claude 不同，故不能靠 claude 的用例间接覆盖。"""
        items = ss.list_history("codebuddy", CWD, 5)["items"]
        if not items:
            self.skipTest("本机 codebuddy 无历史")
        it = items[0]
        self.assertRegex(it["id"], r"\A[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
        self.assertGreater(it["ts"], 0)
        self.assertNotEqual(it["title"], "未命名会话", "标题应由首句用户提问抽出")
        self.assertNotEqual(it["title"], it["id"][:8], "标题不许退化成 sid 前缀（D2）")
        self.assertTrue(ss.title_for("codebuddy", it["id"]), "title_for 必须能按 id 直查")
        argv = ss.resume_argv("codebuddy", it["id"], CWD)
        self.assertEqual(argv[1:], ["--resume", it["id"]])
        self.assertTrue(Path(argv[0]).is_file(), f"resume argv[0] 必须是真实存在的 CLI：{argv[0]!r}")

    @tiers.host_only
    def test_codebuddy_listed_implies_resumable(self):
        """**列得出来就必须点得动**（v0.13.62 codex / v0.13.83 cursor 两次教训的第三次同构）。

        codebuddy 的 resume 是**按起 pty 的工作目录定位会话**的：dist 里
        `findExistingSession()` 调 `sessionManager.get(id)` 不传第二参数 ⇒
        `getPreferredProjectDir(undefined)` 落空 ⇒ 只剩 `getSessionFilePath()` = 当前 cwd 那个桶。
        桶名对不上时它抛 `SessionNotFoundError`（"No conversation found with session ID"），
        比 cursor 的静默开新会话好一点，但一样是「点了就是坏体验」。

        这条把「列表」与「续聊白名单」的一致性变成可断言的量——两处各写一遍必然漂移。"""
        items = ss.list_history("codebuddy", CWD, 20)["items"]
        if not items:
            self.skipTest("本机 codebuddy 无历史")
        for i in items:
            with self.subTest(sid=i["id"][:8]):
                self.assertTrue(ss._exists_on_disk("codebuddy", i["id"], CWD),
                                "列出来了却过不了续聊白名单 ⇒ 点了必然 resume 不到")
                ss.resume_argv("codebuddy", i["id"], CWD)      # 形状门 + 实盘门都过

    @tiers.host_only
    def test_codebuddy_session_cwd_matches_bucket(self):
        """resume 要落到「能解析到该会话桶」的目录：session_cwd 的值与桶名必须自洽。
           记录目录还在 ⇒ 用它；已删/为空 ⇒ 退回画像目录（此时只有桶名恰好相符才允许列出）。"""
        items = ss.list_history("codebuddy", CWD, 20)["items"]
        if not items:
            self.skipTest("本机 codebuddy 无历史")
        for i in items:
            with self.subTest(sid=i["id"][:8]):
                got = ss.session_cwd("codebuddy", i["id"], CWD)
                self.assertTrue(Path(got).is_dir(), f"续聊目录必须真实存在：{got!r}")
                f = ss._store_path("codebuddy", i["id"])
                self.assertIsNotNone(f)
                self.assertEqual(ss._cb_bucket_of(got), f.parent.name,
                                 "session_cwd 与 codebuddy 的桶名不自洽 ⇒ --resume 找不到该会话")

    @tiers.host_only
    def test_codebuddy_empty_files_not_listed(self):
        """零字节 jsonl（建会话时 writeFile("") 占位、没发言就退出）不得进列表：
           resume 它得到的是一条**空会话**，与 opencode exists(message) / cursor
           hasConversation 同口径。实测本机 73 个 jsonl 里 11 个是空的。"""
        empties = [p for p in ss.CODEBUDDY_PROJECTS.glob("*/*.jsonl") if p.stat().st_size == 0]
        if not empties:
            self.skipTest("本机暂无零字节 codebuddy 会话文件")
        listed = {i["id"] for i in ss.list_history("codebuddy", CWD, 20)["items"]}
        for p in empties:
            self.assertNotIn(p.stem, listed, f"零字节会话不该被列出：{p.name}")
            self.assertFalse(ss._exists_on_disk("codebuddy", p.stem, CWD))

    def test_missing_dir_degrades_not_raises(self):
        # 跳目录口径（用户 09-22 裁定）之后，"画像目录不存在"不再等于"无历史"：
        # 全局最近条目照给，传入的 cwd 只用于画像目录标注。此例断言的是「不许抛」。
        d = ss.list_history("grok", "/no/such/dir", 3)
        self.assertIsInstance(d["items"], list)
        for i in d["items"]:
            self.assertEqual({"agent", "id", "title", "ts", "cwd"} - set(i), set(), "条目字段不齐")

    def test_unknown_agent_degrades(self):
        self.assertEqual(ss.list_history("pi", CWD, 3)["items"], [])

    def test_sqlite_readonly_mtime_unchanged(self):
        for agent, p in (("hermes", Path.home() / ".hermes/state.db"),
                         ("codex", Path.home() / ".codex/state_5.sqlite"),
                         ("opencode", Path.home() / ".local/share/opencode/opencode.db")):
            if not p.exists():
                self.skipTest(f"{p} 不存在")
            before = p.stat().st_mtime_ns
            ss.list_history(agent, CWD, 3)
            self.assertEqual(before, p.stat().st_mtime_ns)

    def test_known_ids_covers_history(self):
        ids = ss.known_ids("grok", CWD)
        for it in ss.list_history("grok", CWD, 3)["items"]:
            self.assertIn(it["id"], ids)

    def test_cache_returns_same_object(self):
        self.assertIs(ss.list_history("grok", CWD, 3), ss.list_history("grok", CWD, 3))


class TestLiveTitles(unittest.TestCase):
    def test_shape(self):
        for agent in ("grok", "claude", "hermes", "jcode", "codex", "qoder", "pi"):
            with self.subTest(agent=agent):
                m = ss.live_titles(agent)
                self.assertIsInstance(m, dict)
                for k, v in m.items():
                    self.assertIsInstance(k, int)
                    self.assertIsInstance(v, str)


@tiers.host_only              # 三个用例里两个要真盘样本 ⇒ 整类标 L1，宁可少跑不谎报
class TestTitleFor(unittest.TestCase):
    """续聊会话的顶栏标题兜底：live_titles() 靠 pid 反查，实测 jcode 只在退出时写 last_pid、
       codex/qoder 压根没有 pid→会话 映射 ⇒ 只靠它会回退成 sid 前缀（正是本次要消除的东西）。"""

    def test_all_six_resolve_by_id(self):
        for agent, cwd in (("grok", CWD), ("claude", CWD), ("jcode", CWD),
                           ("hermes", str(Path.home())), ("codex", CWD), ("qoder", QODER_CWD),
                           ("cursor", CWD), ("codebuddy", CWD)):
            with self.subTest(agent=agent):
                # 只取**有内容**的一条：2026-10-24 22:36 grok 的 build-plan 探针在本机
                # 留下一条骨架会话（num_messages=0 / session_summary=""），它合法地没有
                # 标题（title_for 返 ""是正确行为），旧的 items[0] 写法把它当解析失败
                # ⇒ 闸门 ⑥ 在 10-25 被这条数据卡住。判据用**独立于被测代码**的 msgs
                # 字段筛，不拿「title 是否为空」筛（那会把真解析坏静默跳过，同 _first_live 的教训）。
                # 注意只有 grok 侧会给出 msgs（其余 agent 为 None）⇒ 只排 ==0，不排 None。
                it = next((x for x in ss.list_history(agent, cwd, 20)["items"]
                           if x.get("msgs") != 0), None)
                if it is None:
                    self.skipTest(f"{agent} 该目录无有内容的历史")
                t = ss.title_for(agent, it["id"], cwd)
                self.assertTrue(t, f"{agent} title_for 返回空")
                self.assertTrue(re.search(r"[\u4e00-\u9fffA-Za-z]", t), f"{agent} 标题无实义：{t!r}")
                self.assertNotEqual(t, it["id"][:8], "标题退化成 sid 前缀")

    def test_bad_shape_id_rejected(self):
        for bad in ("../../etc/passwd", "a;rm -rf", "", "x" * 300):
            with self.subTest(bad=bad):
                self.assertEqual(ss.title_for("grok", bad), "")

    def test_unknown_agent_returns_empty(self):
        self.assertEqual(ss.title_for("pi", "01a0c914-8f95-78e2-8b0f-123456789abc"), "")


@tiers.host_only              # 依赖本机 jcode 平铺目录的真实条数（L1）
class TestJcodeWindow(unittest.TestCase):
    """09-22 实测缺陷回归：jcode 仓库是平铺目录、混着所有 cwd。早期实现只扫「最新 20 个
       文件」再按 cwd 过滤 —— 当日 10 条探针（cwd=/tmp、/home/gztxt/agent-hub）把窗口占满，
       技术文档目录下实有 89 条却只剩 1 条可见，前端看着像漏读。"""

    def test_limit_is_actually_filled(self):
        """产品默认 5 条（HIST_LIMIT）：不得再出现"只要 5 条却给 1 条"的静默缺量"""
        d = ss.list_history("jcode", CWD, 5)
        self.assertEqual(len(d["items"]), 5, "limit=5 必须填满")
        d8 = ss.list_history("jcode", CWD, 8)
        self.assertGreaterEqual(len(d8["items"]), 8, "放宽 limit 应真拿到更多，而不是被窗口卡住")
        ids = [i["id"] for i in d8["items"]]
        self.assertEqual(len(ids), len(set(ids)), "不得出现重复条目")
        ts = [i["ts"] for i in d8["items"]]
        self.assertEqual(ts, sorted(ts, reverse=True), "必须按时间倒序（跨目录之后仍要单调）")

    def test_session_cwd_matches_record(self):
        """session_cwd() 是 pty 起目录与 qoder -w 的唯一来源，必须与条目自带 cwd 一致，
           且绝不返回一个不存在的目录（否则 pty 直接起不来）"""
        for a in ("grok", "claude", "jcode", "qoder"):
            items = ss.list_history(a, CWD, 5)["items"]
            if not items:
                continue
            i = _first_live(items) or items[0]
            with self.subTest(agent=a):
                c = ss.session_cwd(a, i["id"], "FALLBACK")
                if Path(i["cwd"]).is_dir():
                    self.assertEqual(c, i["cwd"], "反查与会话记录不一致")
                    self.assertTrue(Path(c).is_dir(), f"返回了不存在的目录：{c}")
                else:
                    self.assertEqual(c, "FALLBACK",
                                     f"记录目录 {i['cwd']} 已消失时必须回退，不得吐死路径")
                    # 回退分支的"绝不返回不存在目录"必须拿**真存在的 fallback** 验，
                    # 拿字面量 "FALLBACK" 去 is_dir() 是在断一个无意义的命题
                    real = ss.session_cwd(a, i["id"], CWD)
                    self.assertEqual(real, CWD, "回退时必须给调用方给的 fallback")
                    self.assertTrue(Path(real).is_dir(), f"回退结果不在盘上：{real}")


@tiers.host_only              # 依赖真盘；同一条「形状合法但盘上没有必须拒」在
                              # tests/test_term_launch_guard.py::TestResumeArgvHostile 已有 L0 版
class TestResumeExists(unittest.TestCase):
    """续聊存在性校验必须按仓库结构直查，不能走被 limit 截断的展示清单"""

    def test_old_session_beyond_top20_resumable(self):
        root = Path.home() / ".jcode" / "sessions"

        def wd(p):
            try:
                return json.load(open(p, errors="ignore")).get("working_dir")
            except Exception:  # noqa: BLE001
                return None
        cands = [p for p in sorted(root.glob("session_*.json"),
                                   key=lambda x: x.stat().st_mtime, reverse=True) if wd(p) == CWD]
        if len(cands) <= 25:
            self.skipTest(f"该目录仅 {len(cands)} 条，样本不足")
        old = cands[-5].stem
        self.assertNotIn(old, ss.known_ids("jcode", CWD), "前提：它确实不在 20 条展示清单里")
        self.assertEqual(ss.resume_argv("jcode", old, CWD), ["jcode", "--resume", old])

    def test_qoder_argv_uses_session_cwd(self):
        """qoder 是唯一把目录写进 argv 的（-w）：跳目录后必须给会话自己的目录，
           拿画像 cwd 硬套等于「在技术文档里打开 agent-hub 的工程」"""
        items = ss.list_history("qoder", CWD, 5)["items"]
        if not items:
            self.skipTest("qoder 无可续条目")
        it = _first_live(items) or items[0]
        argv = ss.resume_argv("qoder", it["id"], CWD)
        self.assertEqual(argv[0], "qodercli")
        self.assertEqual(argv[argv.index("-w") + 1],
                         it["cwd"] if Path(it["cwd"]).is_dir() else CWD,
                         "-w 必须是会话自己的目录（其目录已消失时给 fallback，即调用方 CWD）")
        self.assertEqual(argv[-2:], ["-r", it["id"]])

    def test_missing_recorded_dir_falls_back(self):
        """记录目录已被删时必须退回 fallback——否则 pty 起在不存在的目录里直接死，
           而这条分支在真仓库里没有自然样本（探针只能 SKIP），故用 mock 钉住。"""
        from unittest import mock
        items = ss.list_history("jcode", CWD, 5)["items"]
        if not items:
            self.skipTest("jcode 无可续条目")
        live = _first_live(items)
        it = live or items[0]
        sid, want = it["id"], it["cwd"]
        self.assertTrue(want, "样本需自带目录")
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.assertEqual(ss.session_cwd("jcode", sid, "FALLBACK"), "FALLBACK")
        if live:
            self.assertEqual(ss.session_cwd("jcode", sid, "FALLBACK"), want, "未打桩时必须给真目录")
        else:
            # 盘上没有活目录样本时，本例仍钉住硬约束：绝不返回不存在的路径
            self.assertEqual(ss.session_cwd("jcode", sid, "FALLBACK"), "FALLBACK",
                             f"记录目录 {want} 已消失时必须回退")

    def test_absent_id_still_rejected(self):
        with self.assertRaises(ValueError):
            ss.resume_argv("jcode", "session_zzzz_1790000000000_0000000000000000", CWD)
        with self.assertRaises(ValueError):
            ss.resume_argv("grok", "00000000-0000-4000-8000-000000000000", CWD)
        with self.assertRaises(ValueError):
            ss.resume_argv("codex", "00000000-0000-4000-8000-000000000000", CWD)
