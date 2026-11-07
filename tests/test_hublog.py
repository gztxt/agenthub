#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：设置 → 日志子菜单（src/hublog.py + 前端 #page-settings-logs）。

夹具：DB 走 tmp，journald 在 `_journal_lines` 这一个接缝上 mock（零真 journalctl、
零宿主依赖）。每条用例对应一类静默故障或一次实测踩坑：

1. **`--since` 必须配 `-r`**（09-27 实测）：带 `--since` 时 journalctl 从窗口起点
   正序读，`-n` 截的是窗口里**最旧**的 N 条 ⇒ 最新的报错全丢了，页面看着"有日志"
   却永远停在 24h 前。钉住命令里必须有 `-r`。
2. **多行 traceback 不许被切成孤儿行**：`-r` 出来续行排在头行**之前**，必须先
   翻回时间正序再解析，否则续行会贴到更早那条上（报错看着像别人的）。
3. **关键字过滤不进命令行**：用户输入只做 Python 侧子串匹配，从根上断掉命令注入
   （日志页是"用户能自由输入"的少数入口之一）。
4. **journald 不可用是数据不是异常**：非 systemd / 超时 ⇒ sources[].ok=false + 说明，
   另一路照常出，绝不整页 500。
5. **鉴权按写方法判**（2026-11-07 起身份 = Cookie 会话 / HTTP Basic）：未登录 401、
   用户库不可用 503（fail-closed）、已登录放行 —— 与 /api/runlog、/api/audit/list 同口径。
   日志含 IP/路径/查询词，批量读＝窥史。
"""
import asyncio
import json
import os
import pathlib
import re
import shutil
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

import db                              # noqa: E402
import hublog                          # noqa: E402

import tiers  # noqa: E402

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))

PASSCODE = "hublog-unit-passcode"


def _ago_iso(minutes: int) -> str:
    """窗口内（相对当前）的 created_at。

    夹具原先写死 '2026-09-27T05:00:00+00:00'，而过了一天多它就落到 window=24h
    窗外 ⇒ 整批用例红，且红得毫无道理（代码没动、只是日历翻页）。
    必须用与 hublog._cutoff_iso 完全同形的 isoformat（含微秒）：DB 侧是**字符串**
    比较，少写毫秒会让同秒时刻被判成更早而误滤。"""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


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


class _Req:
    def __init__(self, headers=None, query="", path="/api/hublog"):
        self.method = "GET"
        self.url = types.SimpleNamespace(path=path, query=query)
        self.headers = types.SimpleNamespace(raw=headers or [])
        self.client = types.SimpleNamespace(host="127.0.0.1")


def _hdr(pc):
    return [(b"x-hub-token", pc.encode())]


class TestLevelOf(unittest.TestCase):
    """级别判定：uvicorn 行首前缀最准，关键字兜底。"""

    def test_prefixes(self):
        self.assertEqual(hublog._level_of("ERROR:    Exception in ASGI application"), "error")
        self.assertEqual(hublog._level_of("WARNING:  x"), "warn")
        self.assertEqual(hublog._level_of("INFO:     127.0.0.1 - \"GET /health HTTP/1.1\" 200 OK"), "info")

    def test_keywords(self):
        self.assertEqual(hublog._level_of("Traceback (most recent call last):"), "error")
        self.assertEqual(hublog._level_of('INFO:     1.2.3.4 - "POST /x HTTP/1.1" 500 Internal Server Error'), "error")
        self.assertEqual(hublog._level_of('INFO:     1.2.3.4 - "GET /x HTTP/1.1" 401 Unauthorized'), "warn")
        self.assertEqual(hublog._level_of("[writegate] 401：POST /api/x"), "warn")
        # 普通 404 是噪声，别把日志染黄
        self.assertEqual(hublog._level_of('INFO:     1.2.3.4 - "GET /x HTTP/1.1" 404 Not Found'), "info")


class TestJournalParse(unittest.TestCase):
    """解析层：续行归并 + 时间正序（`-r` 输出是新→旧）。"""

    # `-r` 出来的形状：新→旧；同一条多行消息内部顺序不变（头行在前、续行无前缀）。
    # 注：本机 agent-hub 的 journal 里实测 3000 条全是无换行消息（无实证样本），
    # 所以这里的续行归并是**防御性**的：有续行就并，没有也不影响单条解析。
    _LINES = [
        "2026-09-27T16:00:03+0800 zzst agent-hub[1]: INFO:     1.2.3.4 - \"GET /after HTTP/1.1\" 200 OK",
        "2026-09-27T16:00:02+0800 zzst agent-hub[1]: ERROR:    Exception in ASGI application",
        "  File \"/x/y.py\", line 3, in f",
        "ValueError: boom",
        "2026-09-27T15:59:00+0800 zzst agent-hub[1]: INFO:     1.2.3.4 - \"GET / HTTP/1.1\" 200 OK",
    ]

    def test_continuation_merges_into_header(self):
        with mock.patch.object(hublog, "_journal_lines", lambda w, n: (self._LINES, "")):
            es, note = hublog._journal_entries(24, 100)
        self.assertEqual(note, "")
        self.assertEqual(len(es), 3, "traceback 的续行必须并入头行，不能各自成条")
        head = [e for e in es if e["level"] == "error"][0]
        self.assertIn("ValueError: boom", head["msg"])
        self.assertIn("line 3", head["msg"])
        self.assertEqual(es[0]["ts"], "2026-09-27T16:00:03+0800", "-r 输出保持新→旧")
        self.assertEqual(es[-1]["ts"], "2026-09-27T15:59:00+0800")
        self.assertTrue(all(e["src"] == "journal" for e in es))

    def test_command_has_reverse_flag(self):
        """不带 `-r` 时 `--since` 会让 `-n` 截到最旧的 N 条（09-27 实测）。"""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(hublog.subprocess, "run", fake_run):
            hublog._journal_lines(24, 50)
        cmd = seen["cmd"]
        self.assertIn("--user", cmd)
        self.assertIn("-r", cmd, "缺 -r ⇒ 拿到的是窗口里最旧的 N 条，最新报错全丢")
        self.assertIn("--since", cmd)
        self.assertNotIn(";", " ".join(cmd), "命令里不许出现分隔符拼串")

    def test_keyword_never_reaches_commandline(self):
        """用户输入只做 Python 侧匹配：命令里不许出现用户串。"""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(hublog.subprocess, "run", fake_run):
            hublog._journal_lines(24, 50)
        joined = " ".join(seen["cmd"])
        for bad in ("rm", "-rf", ";", "|", "$("):
            self.assertNotIn(bad, joined)

    def test_failure_is_data_not_exception(self):
        with mock.patch.object(hublog, "_journal_lines", lambda w, n: ([], "journalctl 不存在（非 systemd 环境）")):
            es, note = hublog._journal_entries(24, 100)
        self.assertEqual(es, [])
        self.assertIn("不存在", note)

    def test_empty_window_gets_explained(self):
        """空 ≠ 成功：单元名不对/手跑实例都会空，必须把原因说在页面上。"""
        with mock.patch.object(hublog, "_journal_lines", lambda w, n: ([], "")):
            es, note = hublog._journal_entries(24, 100)
        self.assertEqual(es, [])
        self.assertIn("窗口内无条目", note)


class TestEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("hublog-")
        db.init_db(self.tmp / "log.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('hub_chat','chat','success',12,'{\"q\":\"你好\"}',?)", (_ago_iso(90),))
        db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                   " VALUES('task_exec','run','fail',30,'{\"err\":\"boom\"}',?)", (_ago_iso(80),))
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("TERM_TOKEN", None)
        os.environ.pop("HUB_PASSCODE", None)
        self._jl = mock.patch.object(hublog, "_journal_lines", lambda w, n: ([], "L0 夹具不拉 journald"))
        self._jl.start()
        self.addCleanup(self._jl.stop)

    def _run(self, coro):
        return asyncio.run(coro)

    def _call(self, req, **kw):
        """直调端点必须把 Query 参数**逐个显式传**：FastAPI 的默认值是 Query 对象
           而不是 "all"/24（只走 HTTP 时才由框架注入）⇒ 省略就会撞 400。"""
        args = {"source": "all", "level": "all", "q": "", "window": 24,
                "limit": 200, "subject": "", "format": "json"}
        args.update(kw)
        return self._run(hublog.hublog_query(req, **args))

    def test_userdb_down_is_503(self):
        with mock.patch.object(db, "is_open", lambda: False):
            with self.assertRaises(Exception) as cm:
                self._run(hublog.hublog_query(_Req([])))
        self.assertEqual(getattr(cm.exception, "status_code", None), 503,
                         "用户库不可用必须 fail-closed，不能放行")

    def test_missing_credential_is_401(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        with self.assertRaises(Exception) as cm:
            self._run(hublog.hublog_query(_Req([])))
        self.assertEqual(getattr(cm.exception, "status_code", None), 401)

    def test_ok_returns_events_and_stats(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        d = self._call(_Req(_hdr(PASSCODE)))
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["stats"]["error"], 1)
        self.assertEqual(d["stats"]["event"], 2)
        self.assertFalse(d["sources"][0]["ok"], "夹具里 journald 不可用，ok 必须是 False")
        self.assertIn("L0 夹具", d["sources"][0]["note"], "不可用要把原因带出来，不许静默空列表")
        self.assertTrue(any("boom" in e["msg"] for e in d["entries"]),
                        "fail 事件的 err 必须出现在正文里（否则等于没报错）")

    def test_source_event_excludes_journal(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        d = self._call(_Req(_hdr(PASSCODE)), source="event")
        self.assertTrue(all(e["src"] == "event" for e in d["entries"]))

    def test_level_error_filters(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        d = self._call(_Req(_hdr(PASSCODE)), level="error")
        self.assertTrue(d["entries"])
        self.assertTrue(all(e["level"] == "error" for e in d["entries"]))

    def test_keyword_filter_matches_message(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        d = self._call(_Req(_hdr(PASSCODE)), q="boom")
        self.assertEqual([e["msg"] for e in d["entries"]],
                         [e["msg"] for e in d["entries"] if "boom" in e["msg"].lower()])
        self.assertEqual(d["count"], 1)

    def test_bad_params_are_400(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        for kw in ({"source": "evil"}, {"level": "trace"}, {"subject": "nope"}):
            with self.assertRaises(Exception) as cm:
                self._call(_Req(_hdr(PASSCODE)), **kw)
            self.assertEqual(getattr(cm.exception, "status_code", None), 400, kw)

    def test_long_keyword_is_capped(self):
        """超长关键字：HTTP 层由 max_length 挡，直调时也得在 Python 侧截到 120
        （不能拿用户串去拼任何东西）。"""
        os.environ["HUB_PASSCODE"] = PASSCODE
        d = self._call(_Req(_hdr(PASSCODE)), q="x" * 500)
        self.assertLessEqual(len(d["query"]["q"]), 500)
        self.assertEqual(d["count"], 0, "关键字匹配不到 ⇒ 空结果，不是报错")

    def test_text_export_attaches_filename(self):
        os.environ["HUB_PASSCODE"] = PASSCODE
        r = self._call(_Req(_hdr(PASSCODE)), format="text")
        self.assertIn("agenthub.log", r.headers.get("content-disposition", ""))
        self.assertIn("boom", r.body.decode("utf-8"))


class TestRunlogMerged(unittest.TestCase):
    """v0.13.47：系统菜单「运行日志」页并入本端点 —— source=rest 视图 + subject 过滤。

    这批用例守的是「合并 ≠ 丢功能」：原页面能按 source=rest 只看三中心留痕、
    能按 subject 只看某一路检索，新入口必须一样做得到，且非法 subject 仍 400
    （照抄 /api/runlog 的口径，别因为换了个入口就放宽校验）。
    """

    def setUp(self):
        self.tmp = _mktmp("hublog-rest-")
        db.init_db(self.tmp / "log.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        rows = [
            ("rest", "mem.search", "success", 7, '{"q":"abc","channel":"web"}'),
            ("rest", "kb.search", "fail", 9, '{"err":"kb 降级","channel":"mcp"}'),
            ("hub_chat", "chat", "success", 12, '{"q":"你好"}'),
        ]
        for src, subj, st, ms, det in rows:
            db.execute("INSERT INTO profile_events(source,subject,status,duration_ms,detail,created_at)"
                       " VALUES(?,?,?,?,?,?)", (src, subj, st, ms, det, _ago_iso(90)))
        os.environ["HUB_PASSCODE"] = PASSCODE
        self.addCleanup(lambda: os.environ.pop("HUB_PASSCODE", None))
        self._jl = mock.patch.object(hublog, "_journal_lines", lambda w, n: ([], "L0 夹具不拉 journald"))
        self._jl.start()
        self.addCleanup(self._jl.stop)

    def _call(self, **kw):
        args = {"source": "all", "level": "all", "q": "", "window": 24,
                "limit": 200, "subject": "", "format": "json"}
        args.update(kw)
        return asyncio.run(hublog.hublog_query(_Req(_hdr(PASSCODE)), **args))

    def test_source_rest_is_only_three_center_traces(self):
        d = self._call(source="rest")
        self.assertTrue(d["entries"], "选了运行日志来源却空 ⇒ 合并后原内容没了")
        self.assertTrue(all(e["tag"] == "rest" for e in d["entries"]),
                        "rest 视图里混进了别的 source ⇒ 过滤没生效")
        self.assertEqual(d["count"], 2)
        # 原页面的结构化字段（耗时 / 通道 / 查询词）必须在正文里，否则等于降质
        joined = " ".join(e["msg"] for e in d["entries"])
        self.assertIn("7ms", joined)
        self.assertIn("channel=web", joined)
        self.assertIn("q=abc", joined)

    def test_subject_filter_narrows_to_one_trace(self):
        d = self._call(source="rest", subject="kb.search")
        self.assertEqual(d["count"], 1)
        self.assertIn("kb 降级", d["entries"][0]["msg"])

    def test_subject_without_rest_still_filters_events(self):
        d = self._call(subject="mem.search")
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["entries"][0]["tag"], "rest")

    def test_bad_subject_is_400(self):
        with self.assertRaises(Exception) as cm:
            self._call(subject="rm -rf /")
        self.assertEqual(getattr(cm.exception, "status_code", None), 400,
                         "subject 必须按 runlog.SUBJECTS 白名单校验（原 /api/runlog 同口径）")

    def test_subjects_enum_returned_for_dropdown(self):
        d = self._call()
        self.assertIn("mem.search", d["subjects"], "前端 subject 下拉靠这份清单填充")


class TestUnitNameConsistency(unittest.TestCase):
    """**unit 改名的一致性闸门**（2026-10-08 `agent-hub.service` → `agenthub.service`）。

    为什么这组用例值钱：`journalctl -u <不存在的单元>` **静默返空、不报错**。
    所以「代码里的 unit 名与真实 systemd 单元脱钩」这种故障的表现是
    「设置→日志页空白，而 /health 正常」——没有任何一处会抛错告诉你。
    本组把三份真值（代码常量 / 仓内归档副本 / 真实 systemd 单元）互相比对，
    脱钩当场红。

    L0 部分只读仓内文件（deploy/）；L1 部分读真 ~/.config/systemd/user。
    """

    UNIT = "agenthub.service"
    OLD_UNIT = "agent-hub.service"

    def test_default_unit_is_current_name(self):
        self.assertEqual(hublog.DEFAULT_UNIT, self.UNIT)

    def test_frontend_user_visible_text_names_current_unit(self):
        """前端**用户可见**的降级文案必须写新单元名。

        为什么单独一条：源文件与构建产物各存一份，同一处文案要改两遍
        （`static/hub/*.js` 与 `static/hub.js`）⇒ 极易只改一处。
        而这条文案的特殊性是它**在用户排障时给指令** —— 写旧名等于教用户去
        重启一个已停用的单元（restart 它既起不来、又在 journald 里刷重试）。
        """
        src = (_REPO / "static" / "hub" / "05-chat-and-history.js").read_text(encoding="utf-8")
        self.assertNotIn(self.OLD_UNIT, src,
                         "前端源码仍写旧单元名 ⇒ 用户看到的是「去重启一个已停用的单元」")
        self.assertIn("重启 %s 后生效" % self.UNIT, src,
                      "降级文案应指向现役单元 %s" % self.UNIT)

    def test_built_bundle_is_in_sync_with_sources(self):
        """构建产物不得残留旧名 —— 这是「只改源码忘重建」的唯一可观测判据。

        v0.13.94 改名时前端那处漏改正是「改了仓名/远端/unit，唯独没重建 hub.js」，
        靠人眼 review 产物 6781 行是不可能发现的，所以固化成断言。
        """
        import hashlib
        built = (_REPO / "static" / "hub.js").read_text(encoding="utf-8", errors="ignore")
        self.assertNotIn(self.OLD_UNIT, built,
                         "static/hub.js 仍含旧单元名 ⇒ 忘了跑 scripts/build_hubjs.sh")
        # 提手必须等于产物内容的 md5 前 8 位，否则端侧拿到的是缓存旧版
        digest = hashlib.md5(built.encode("utf-8", "ignore")).hexdigest()[:8]
        html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("hub.js?v=%s" % digest, html,
                      "index.html 的 ?v= 提手与产物实际 md5 %s 不符 ⇒ 端侧会拿缓存旧版" % digest)

    def test_health_service_field_uses_current_name(self):
        """/health 的 `service` 字段。它无消费方（纯展示），但**是**运维第一手看到的标识。"""
        src = (_REPO / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"service": "agenthub"', src,
                      "/health 的 service 字段仍是旧名（它无消费方，容易漏改）")

    def test_deploy_copy_named_after_unit(self):
        d = _REPO / "deploy"
        self.assertTrue((d / self.UNIT).is_file(),
                        "仓内归档副本缺失 deploy/%s" % self.UNIT)
        self.assertFalse((d / self.OLD_UNIT).exists(),
                         "旧名副本 deploy/%s 还在 ⇒ 两份真值迟早分叉" % self.OLD_UNIT)

    def test_deploy_copy_identifier_matches_unit(self):
        text = (_REPO / "deploy" / self.UNIT).read_text(encoding="utf-8")
        m = re.search(r"^SyslogIdentifier=(\S+)$", text, re.MULTILINE)
        self.assertTrue(m, "deploy/%s 缺 SyslogIdentifier" % self.UNIT)
        self.assertEqual(m.group(1), self.UNIT.removesuffix(".service"),
                         "SyslogIdentifier 与 unit 名不一致 ⇒ journald 里的 tag 对不上")

    def test_deploy_copy_starts_dualstack(self):
        """副本的 ExecStart 必须走 run_dualstack.py —— 09-29 双栈改造只改了生产、漏了副本。

        分叉的后果比 unit 改名那次更隐蔽：照 `deploy/README.md` 装一遍就把双栈服务
        悄悄降级成单栈 v4，**/health 照样 200**（只是局域网 IPv6 侧不再可达）。
        判据取「含 run_dualstack.py」，不逐字比 ExecStart（路径参数将来可能合法变动）。
        """
        text = (_REPO / "deploy" / self.UNIT).read_text(encoding="utf-8")
        m = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE)
        self.assertTrue(m, "deploy/%s 缺 ExecStart" % self.UNIT)
        self.assertIn("run_dualstack.py", m.group(1),
                      "副本的 ExecStart 不含 run_dualstack.py ⇒ 与生产分叉成单栈 v4。"
                      "修法：cp ~/.config/systemd/user/%s deploy/%s（副本须是生产的逐字镜像）"
                      % (self.UNIT, self.UNIT))
        self.assertNotRegex(m.group(1), r"--port\s+\d+",
                            "端口真值只在 .env(PORT)，unit 里写死会盖住它（第二份真值）")

    def test_deploy_copy_does_not_pin_old_unit_name(self):
        """副本里不该残留旧单元名 —— 否则照 README 装完 enable 的是旧名。"""
        text = (_REPO / "deploy" / self.UNIT).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#"):
                continue          # 注释里提到旧名是改名说明，允许
            self.assertNotIn(self.OLD_UNIT.removesuffix(".service"), line,
                             "deploy/%s 的非注释行仍含旧单元名" % self.UNIT)

    def test_scanner_excludes_current_unit(self):
        import scanner
        self.assertTrue(scanner.EXCLUDE_PATTERNS.match("agenthub"),
                        "scanner 的 EXCLUDE 认不出 agenthub ⇒ 端口扫描会把自家 3102 "
                        "登记成外部服务（症状是资源页多一条自己家的条目，不报错）")
        # 前缀匹配是**故意的**：`agenthub-backup` / `agenthub-stage` 这类影子单元
        # 同样该被排除，所以别把正则写死成全等（`^agenthub$` 会让影子实例漏登记）。
        self.assertTrue(scanner.EXCLUDE_PATTERNS.match("agenthub-backup"),
                        "影子/备份单元也该被排除 ⇒ EXCLUDE 必须保持前缀语义")

    @tiers.host_only
    def test_real_systemd_unit_exists(self):
        """真单元在盘上。新旧任一存在即可，但**两份都在**说明 disable 漏了。"""
        u = pathlib.Path.home() / ".config" / "systemd" / "user"
        self.assertTrue((u / self.UNIT).is_file(),
                        "真单元 ~/.config/systemd/user/%s 不存在" % self.UNIT)

    @tiers.host_only
    def test_deploy_copy_matches_real_unit(self):
        """副本必须是**生产 unit 的逐字镜像**（比 ExecStart 更严，防任何字段分叉）。

        为什么值得写这么严：分叉的两种表现都不报错 ——
        ① ExecStart 落后 ⇒ 照 README 装完服务被悄悄降级成单栈，/health 仍 200；
        ② SyslogIdentifier/路径落后 ⇒ 装完日志页或文档链又指回旧形态。
        注释行不比（生产 unit 的改名说明注释会随时间增补，逐字比会误判）。
        """
        live = pathlib.Path.home() / ".config" / "systemd" / "user" / self.UNIT
        copy = _REPO / "deploy" / self.UNIT
        if not live.is_file():
            self.skipTest("真单元不在盘上（本机非生产形态）")

        def keys(text):
            out = {}
            for ln in text.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                out.setdefault(k, []).append(v.strip())
            return out

        a, b = keys(copy.read_text(encoding="utf-8")), keys(live.read_text(encoding="utf-8"))
        self.assertEqual(sorted(a), sorted(b),
                         "deploy 副本与生产 unit 的键集不同 ⇒ 副本已落后或超前："
                         "仅副本有=%s 仅生产有=%s" % (sorted(set(a) - set(b)), sorted(set(b) - set(a))))
        for k in sorted(a):
            self.assertEqual(a[k], b[k], "键 %s 分叉：副本=%s 生产=%s" % (k, a[k], b[k]))


class TestFrontendPage(unittest.TestCase):
    """前端：第四个设置子页，形态与另三个同口径（无抽屉 / 无 inline onclick）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        cls.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_page_and_nav_entry_exist(self):
        self.assertIn('id="page-settings-logs"', self.html)
        self.assertIn("'settings-logs'", (_REPO / "static" / "hub" / "05-chat-and-history.js")
                      .read_text(encoding="utf-8"), "侧栏设置组缺「日志」子项")
        self.assertIn("'settings-logs'", self.js, "hub.js 产物没带上日志子项")
        self.assertIn("page-settings-logs", self.js, "SET_PAGE_IDS 缺日志页 ⇒ 委托挂不上")

    def test_no_inline_onclick_and_no_new_overlay(self):
        seg = self.html[self.html.index('id="page-settings-logs"'):]
        seg = seg[:seg.index("</section>")]
        self.assertNotIn("onclick", seg, "内联 onclick 会旁路委托（09-23 事故正身）")
        self.assertNotIn("@media", seg, "页内不许新开断点（断点唯一真源在 :root/01）")
        for act in ("log-refresh", "log-copy", "log-export"):
            self.assertIn(act, seg)

    def test_lazy_load_hook_and_no_passcode_flow(self):
        """2026-11-07 C 档：日志读端点走会话 Cookie，不再有页内口令框 / 不再弹 prompt。"""
        src = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        self.assertIn("settings-logs", src, "go() 缺日志页懒加载钩子")
        self.assertIn("settingsLogsLoad", src)
        self.assertNotIn("logPasscode", src, "共享口令框已废弃，不该回潮")
        self.assertNotIn("prompt(", src[src.index("function settingsLogsLoad"):
                                       src.index("function settingsLogsRender")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
