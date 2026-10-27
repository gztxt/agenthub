#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：Botboy 页（src/botboy.py + 前端四件套）。

数据源不是 HTTP 而是**文件投影**：`$DSH_HOME/bot/state.json`（ds-bot 的权威落盘面）。
所以这里全部用 tmp 目录造夹具，不起服务、不 import src.main、不 spawn dsh。

为什么这些用例必须存在：
1. **两件"没有"必须分得开**（本仓反复栽的坑）：`state.json` 不存在是「还没装/还没建」，
   文件在但坏是「不可判定」。把后者渲染成空团队，用户会以为团队被清空了。
2. **形状以对端源码为准**：字段取自 `ds-bot@0.2.2` 的 roster.js::buildBot / buildRoom
   与 delivery.js::deliver。对端删字段、改类型时这里判红，而不是页面上静默空白。
3. **排序稳定**：Main Bot 置顶 + 最近活跃倒序。没有这条，同一份数据两次刷新顺序不同
   （对象键序在 V8 里对非整数键是插入序，插入序又随写入历史漂移）。
4. **坏值不装成好值**：时间戳非数字/负数必须是 None，不能落成 1970-01-01。
5. **前端四件套**：SYS_PAGES / PAGE_LABELS / #page-botboy(.sp 骨架) / go() 进页钩子，
   缺一即菜单项点了退回总览或正文空白（test_kb_frontend_pages、test_localprojects 同型判据）。
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"));

import botboy                        # noqa: E402

TPL = _REPO / "templates" / "index.html"
SHARD05 = _REPO / "static" / "hub" / "05-chat-and-history.js"
SHARD14 = _REPO / "static" / "hub" / "14-botboy.js"
SHARD01 = _REPO / "static" / "hub" / "01-core-boot.js"
SERVED = _REPO / "static" / "hub.js"


def _state(**over) -> dict:
    """对端 store.js 的初始形状（缺的键也要能读：老版本文件没有 works/requests）。"""
    s = {
        "version": 1, "mainBotIds": [], "bots": {}, "rooms": {}, "agents": {},
        "capabilities": {"items": {}}, "exchanges": [], "exchangeTurns": {},
        "revision": 1,
    }
    s.update(over)
    return s


def _bot(bid, name, created=1761600000000, **over):
    b = {"id": bid, "name": name, "role": "", "instructions": "", "createdAt": created,
         "createdBy": None, "capabilities": []}
    b.update(over)
    return b


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        (self.home / "bot").mkdir(parents=True)
        self.path = self.home / "bot" / "state.json"
        self._old = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(self.home)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DSH_HOME", None)
        else:
            os.environ["DSH_HOME"] = self._old
        self.tmp.cleanup()

    def write(self, obj):
        self.path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class TestMissingAndBroken(_Base):
    """① 两件「没有」分得开；② 坏文件不许装成空团队。"""

    def test_missing_file_is_not_an_error(self):
        d = botboy.overview()
        self.assertTrue(d["ok"])
        self.assertFalse(d["found"])
        self.assertEqual(d["counts"], {"bots": 0, "mains": 0, "rooms": 0, "exchanges": 0, "works": 0})
        self.assertIn(str(self.path), d["hint"], "缺文件时必须指出找的是哪个路径")

    def test_broken_json_is_not_empty_team(self):
        self.path.write_text("{not json", encoding="utf-8")
        d = botboy.overview()
        self.assertFalse(d["ok"])
        self.assertTrue(d["found"])
        self.assertEqual(d["error"]["code"], "state-invalid-json")
        self.assertIn("JSON", d["error"]["message"])

    def test_top_level_not_object(self):
        self.path.write_text("[1,2,3]", encoding="utf-8")
        d = botboy.overview()
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], "state-invalid-shape")

    def test_unreadable_path_reports_not_raises(self):
        # 把 state.json 换成目录 ⇒ read_text 抛 IsADirectoryError（OSError 子类）
        self.path.unlink(missing_ok=True)
        self.path.mkdir()
        d = botboy.overview()
        self.assertFalse(d["ok"])
        self.assertEqual(d["error"]["code"], "state-unreadable")


class TestProjection(_Base):
    """③ 排序稳定；④ 坏值不装好值；⑤ 名字解析与截断。"""

    def test_counts_mains_and_ordering(self):
        self.write(_state(
            mainBotIds=["b1"],
            bots={"b1": _bot("b1", "Chief", 1761600000000),
                  "b2": _bot("b2", "Dev", 1761600100000),
                  "b3": _bot("b3", "Ops", 1761599000000)},
            exchanges=[
                {"id": "e1", "from": "b2", "to": "b3", "text": "旧", "role": "bot", "time": 1761600200000},
                {"id": "e2", "from": "b2", "to": "b1", "text": "新", "role": "bot", "time": 1761600900000},
            ],
        ))
        d = botboy.overview()
        self.assertEqual(d["counts"], {"bots": 3, "mains": 1, "rooms": 0, "exchanges": 2, "works": 0})
        self.assertEqual([b["name"] for b in d["bots"]], ["Chief", "Dev", "Ops"],
                         "Main 必须置顶；其余按最近活跃倒序（Dev 最新）")
        self.assertTrue(d["bots"][0]["main"])
        self.assertEqual(d["bots"][0]["lastActive"], 1761600900000)

    def test_exchanges_newest_first_and_limited(self):
        ex = [{"id": "e%d" % i, "from": "b1", "to": "b1", "text": "t%d" % i,
               "role": "bot", "time": 1761600000000 + i} for i in range(50)]
        self.write(_state(bots={"b1": _bot("b1", "A")}, exchanges=ex))
        d = botboy.overview(limit=5)
        self.assertEqual([e["text"] for e in d["exchanges"]], ["t49", "t48", "t47", "t46", "t45"])
        self.assertEqual(d["counts"]["exchanges"], 50, "counts 报全量，列表才按 limit 截")

    def test_in_flight_works_are_the_task_list(self):
        """state.works = 还没收工的委派（handoffs.js 关闭时 delete）⇒ 就是"任务列表"。

        形状取自 handoffs.js:101 —— {from?, to?, room?, parent?, open[], results[], at}。
        """
        self.write(_state(
            bots={"b1": _bot("b1", "Chief"), "b2": _bot("b2", "Dev")},
            rooms={"r1": {"id": "r1", "name": "Chief、Dev", "members": ["b1", "b2"],
                          "admin": "b1", "mode": "admin", "notice": "", "createdAt": 1}},
            works={
                "w0": {"from": "b1", "to": "b2", "at": 1761600100000, "open": [], "results": []},
                "w1": {"from": "b1", "to": "b2", "room": "r1", "parent": "w0",
                       "open": ["w2"], "results": [{"id": "x"}], "at": 1761600500000},
                "bad": "not-a-dict",
            },
        ))
        d = botboy.overview()
        self.assertEqual(d["counts"]["works"], 2, "畸形条目不计入，但整页不许塌")
        w = d["works"][0]
        self.assertEqual(w["fromName"], "Chief")
        self.assertEqual(w["toName"], "Dev")
        self.assertEqual(w["roomName"], "Chief、Dev")
        self.assertEqual(w["waiting"], 1)
        self.assertEqual(w["results"], 1)
        self.assertEqual(w["at"], 1761600500000, "按开始时间倒序：最新的在最前")

    def test_room_member_and_admin_names_resolved(self):
        self.write(_state(
            mainBotIds=["b1"],
            bots={"b1": _bot("b1", "Chief"), "b2": _bot("b2", "Dev")},
            rooms={"r1": {"id": "r1", "name": "Chief、Dev", "members": ["b1", "b2"],
                          "admin": "b1", "mode": "admin", "notice": "", "createdAt": 1761600200000}},
            exchanges=[{"id": "e1", "from": "b1", "to": "b2", "text": "开工",
                        "role": "bot", "time": 1761600300000, "roomId": "r1"}],
        ))
        d = botboy.overview()
        r = d["rooms"][0]
        self.assertEqual([m["name"] for m in r["members"]], ["Chief", "Dev"])
        self.assertEqual(r["adminName"], "Chief")
        self.assertEqual(r["lastActive"], 1761600300000, "房间活跃时间取自该 roomId 的交接")
        self.assertEqual(d["exchanges"][0]["fromName"], "Chief")

    def test_user_side_exchange_has_no_bot_name(self):
        self.write(_state(bots={"b1": _bot("b1", "Chief")},
                          exchanges=[{"id": "e1", "to": "b1", "text": "你好",
                                      "role": "user", "time": 1761600300000}]))
        e = botboy.overview()["exchanges"][0]
        self.assertEqual(e["fromName"], "用户")
        self.assertEqual(e["toName"], "Chief")

    def test_bad_timestamps_do_not_become_epoch(self):
        self.write(_state(bots={"b1": _bot("b1", "A", created="not-a-number"),
                                "b2": _bot("b2", "B", created=-5)},
                          exchanges=[{"id": "e", "from": "b1", "to": "b1", "text": "x",
                                      "role": "bot", "time": "soon"}]))
        d = botboy.overview()
        self.assertIsNone(d["bots"][0]["createdAt"])
        self.assertIsNone(d["bots"][1]["createdAt"])
        self.assertIsNone(d["exchanges"][0]["time"])

    def test_long_text_truncated(self):
        long = "很长" * 1000
        self.write(_state(bots={"b1": _bot("b1", "A")},
                          exchanges=[{"id": "e", "from": "b1", "to": "b1", "text": long,
                                      "role": "bot", "time": 1761600000000}]))
        t = botboy.overview()["exchanges"][0]["text"]
        self.assertEqual(len(t), botboy.TEXT_KEPT + 1)
        self.assertTrue(t.endswith("…"))

    def test_malformed_children_are_skipped_not_fatal(self):
        self.write(_state(bots={"b1": _bot("b1", "A"), "bad": "not-an-object"},
                          rooms={"r1": []},
                          exchanges=[None, 42, {"id": "e", "from": "b1", "to": "b1",
                                                "text": "ok", "time": 1761600000000}]))
        d = botboy.overview()
        self.assertTrue(d["ok"])
        self.assertEqual([b["name"] for b in d["bots"]], ["A"])
        self.assertEqual(d["rooms"], [])
        self.assertEqual(len(d["exchanges"]), 1)


class TestInstallProbe(_Base):
    """安装态只做文件探测，不 execute 任何东西。"""

    def test_profile_node_modules_detected(self):
        mods = self.home / "profiles" / "web" / "node_modules"
        (mods / "ds-bot").mkdir(parents=True)
        (mods / "ds-bot" / "package.json").write_text("{}", encoding="utf-8")
        (mods / "ds-harness-remote").mkdir()
        d = botboy.overview()
        self.assertTrue(d["install"]["profiles"]["web"]["ds-bot"])
        self.assertFalse(d["install"]["profiles"]["web"]["ds-harness-remote"],
                         "目录在但没有 package.json ⇒ 不算装好（半装/残留）")
        self.assertTrue(d["install"]["any"])

    def test_no_profiles_dir_means_empty_not_crash(self):
        d = botboy.overview()
        self.assertEqual(d["install"]["profiles"], {})
        self.assertFalse(d["install"]["any"])


class TestFrontendWiring(unittest.TestCase):
    """前端四件套 + 产物同步（缺一条就是「菜单点了没反应」或「点了退回总览」）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = TPL.read_text(encoding="utf-8")
        cls.s05 = SHARD05.read_text(encoding="utf-8")
        cls.s01 = SHARD01.read_text(encoding="utf-8")
        cls.s14 = SHARD14.read_text(encoding="utf-8")

    def test_sys_pages_has_botboy_with_existing_sprite_icon(self):
        self.assertRegex(self.s05, r"\['botboy',\s*'Botboy',\s*'([a-z-]+)'\]")
        icon = self.s05.split("['botboy', 'Botboy', '")[1].split("'")[0]
        self.assertIn('<symbol id="i-%s"' % icon, self.html,
                      "ico() 对不存在的 sprite id 静默不画（kb 行整行空白的复发面）")

    def test_page_labels_and_section(self):
        self.assertIn("botboy: 'Botboy'", self.s05)
        self.assertRegex(self.html, r'<section class="page sys" id="page-botboy">')

    def test_page_uses_shared_skeleton(self):
        seg = self.html.split('id="page-botboy">', 1)[1].split("</section>", 1)[0]
        self.assertIn('class="sp"', seg)
        self.assertIn('class="sp-card"', seg)
        self.assertNotRegex(seg, r"""class\s*=\s*['"][^'"]*\bpanel\b""")

    def test_go_hook_present(self):
        self.assertIn("page === 'botboy'", self.s01)
        self.assertIn("loadBotboy(", self.s01)

    def test_works_card_wired(self):
        """「任务列表」= state.works 的投影，页面上必须有承接元素，否则数据到了画不出来。"""
        self.assertIn('id="bbWorks"', self.html)
        self.assertIn('id="bbWorkCount"', self.html)
        self.assertIn("bbWorkRow", self.s14)
        self.assertIn("d.works", self.s14)

    def test_server_js_is_built_from_shards(self):
        self.assertIn("/* v0.13.170 · Botboy 页", SERVED.read_text(encoding="utf-8"),
                      "分片改了没跑 build_hubjs.sh ⇒ 线上仍是旧 bundle（漂移由 test_hubjs_split 判）")


if __name__ == "__main__":
    unittest.main()
