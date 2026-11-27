#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：agenthub × magpie 只读互调（2026-11-27）。

【本文件守三件事】任何一件失守都会让「互调」静默退化成「写死」：
1. `gwurl` 不把 `/v1` 拼两次 —— 2026-11-27 实测 remote judge 一直在打
   `/v1/v1/chat/completions`（网关 404），journal 里是 `judge_fail kind=chain`。
2. 档位派生**默认关闭**，关着时与写死表逐字相同（kill switch 不许被悄悄绕过）。
3. 额度闸门只挡被挡 provider；**读不到额度时不挡**（不把「不知道」当「没额度」）。
"""
import json
import os
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import cost_backfill as cb  # noqa: E402
import gwurl                # noqa: E402
import magpie_client as mc  # noqa: E402
import tiercfg              # noqa: E402


class TestGwurl(unittest.TestCase):
    def test_does_not_double_v1(self):
        self.assertEqual(gwurl.url("http://h:3425/v1", "/v1/chat/completions"),
                         "http://h:3425/v1/chat/completions")
        self.assertEqual(gwurl.url("http://h:3425", "/v1/chat/completions"),
                         "http://h:3425/v1/chat/completions")
        self.assertEqual(gwurl.url("http://h:3425/v1", "chat/completions"),
                         "http://h:3425/v1/chat/completions")
        self.assertEqual(gwurl.url("http://h:3425/v1", "/v1/messages"),
                         "http://h:3425/v1/messages")
        self.assertEqual(gwurl.url("", "/v1/models"), "")

    def test_helpers_agree(self):
        self.assertTrue(gwurl.chat_completions("http://h/v1").endswith("/v1/chat/completions"))
        self.assertTrue(gwurl.messages("http://h").endswith("/v1/messages"))
        self.assertTrue(gwurl.models("http://h/v1").endswith("/v1/models"))


class TestQuotaGate(unittest.TestCase):
    def test_card_state(self):
        self.assertEqual(mc._card_state({"error": "401 refused"})[0], "error")
        self.assertEqual(
            mc._card_state({"windows": [{"remaining": 0}, {"remaining": 1.2}]})[0],
            "exhausted")
        # 一张窗口还有额度 ⇒ 不是耗尽
        self.assertEqual(
            mc._card_state({"windows": [{"remaining": 0}, {"remaining": 40}]})[0], "ok")
        # 不报窗口 ≠ 不可用
        self.assertEqual(mc._card_state({"windows": []})[0], "ok")

    def test_one_ok_account_wins(self):
        cards = [
            {"provider": "workbuddy", "error": "401", "windows": []},
            {"provider": "workbuddy", "windows": [{"remaining": 80}]},
            {"provider": "raccoon", "windows": [{"remaining": 0}]},
            {"provider": "dumate", "windows": [{"remaining": 60}]},
        ]
        with mock.patch.object(mc, "quotas", lambda ttl=None: (cards, "")), \
                mock.patch.object(mc, "recent_failures", lambda *a, **k: {}):
            health, err = mc.provider_health()
            self.assertEqual(err, "")
            self.assertEqual(health["workbuddy"]["state"], "ok")
            self.assertEqual(health["raccoon"]["state"], "exhausted")
            blocked, _ = mc.blocked_providers()
            self.assertEqual(set(blocked), {"raccoon"})

    def test_unavailable_is_not_a_block(self):
        with mock.patch.object(mc, "quotas", lambda ttl=None: (None, "magpie 不可达")):
            blocked, err = mc.blocked_providers()
            self.assertIsNone(blocked)
            self.assertTrue(err)


_FAKE_MODELS = [
    {"id": "dumate/cheap", "pricing": {"prompt": "0.0000001"}},
    {"id": "dumate/mid", "pricing": {"prompt": "0.0000005"}},
    {"id": "dumate/pricey", "pricing": {"prompt": "0.000003"}},
    {"id": "raccoon/cheap", "pricing": {"prompt": "0.0000002"}},
    {"id": "raccoon/pricey", "pricing": {"prompt": "0.000002"}},
    {"id": "auto/group", "pricing": {"prompt": "0.0000001"}},
]


def _fake_health(blocked=()):
    d = {p: {"state": "ok", "reason": ""} for p in ("dumate", "raccoon")}
    for p in blocked:
        d[p] = {"state": "exhausted", "reason": "empty"}
    return d


class TestDeriveTiers(unittest.TestCase):
    def setUp(self):
        # 2026-11-28：本类断言的是 **flash 倾斜关闭时**的老分支。生产 `.env` 里有
        # `TASK_TIERS_FLASH_FIRST=on`，`src/config` 导入时会把它灌进 os.environ
        # ⇒ 全量跑走到 flash 分支（t2/t3 装满 flash 后报「档位不完整」）而假红；
        # 单跑（未加载 .env）才是老分支。所以在类级别钉住 off，不依赖环境巧合。
        _p = mock.patch.dict(os.environ,
                             {k: v for k, v in os.environ.items()
                              if k != "TASK_TIERS_FLASH_FIRST"}, clear=True)
        _p.start()
        self.addCleanup(_p.stop)

    def test_excludes_routing_groups_and_blocked(self):
        with mock.patch.object(mc, "models", lambda ttl=None: (_FAKE_MODELS, "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: (_fake_health(), "")):
            tiers, err = mc.derive_tiers()
            self.assertEqual(err, "")
            flat = [m for v in tiers.values() for m in v]
            self.assertFalse(any(m.startswith("auto/") for m in flat))
            self.assertEqual(tiers["t1"][0], "dumate/cheap")
            self.assertIn("dumate/pricey", tiers["t3"])

        with mock.patch.object(mc, "models", lambda ttl=None: (_FAKE_MODELS, "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: (_fake_health(blocked=("dumate",)), "")):
            tiers2, _ = mc.derive_tiers()
            self.assertTrue(all(m.startswith("raccoon/")
                                for v in tiers2.values() for m in v))

    def test_gateway_down_returns_none(self):
        with mock.patch.object(mc, "models", lambda ttl=None: (None, "down")):
            tiers, err = mc.derive_tiers()
            self.assertIsNone(tiers)
            self.assertIn("down", err)


class TestErrorWindow(unittest.TestCase):
    def setUp(self):
        # 2026-11-28：本类断言的是 **flash 倾斜关闭时**的老分支。生产 `.env` 里有
        # `TASK_TIERS_FLASH_FIRST=on`，`src/config` 导入时会把它灌进 os.environ
        # ⇒ 全量跑走到 flash 分支（t2/t3 装满 flash 后报「档位不完整」）而假红；
        # 单跑（未加载 .env）才是老分支。所以在类级别钉住 off，不依赖环境巧合。
        _p = mock.patch.dict(os.environ,
                             {k: v for k, v in os.environ.items()
                              if k != "TASK_TIERS_FLASH_FIRST"}, clear=True)
        _p.start()
        self.addCleanup(_p.stop)
    """实时错误窗（2026-11-27 事故：dumate 连续 429 每次 38 秒，闸门只看额度 ⇒ 派发卡死）。"""

    def _write(self, rows):
        import tempfile
        from datetime import datetime, timedelta, timezone
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        now = datetime.now(timezone.utc)
        for i, (prov, status) in enumerate(rows):
            f.write(json.dumps({"t": (now - timedelta(seconds=i * 10)).isoformat(),
                                "provider": prov, "status": status,
                                "model": "m", "req": "%s/m" % prov}) + "\n")
        f.close()
        return f.name

    def test_ratio_blocks_only_bad_provider(self):
        import os
        from datetime import datetime, timedelta, timezone
        path = self._write([("dumate", "429")] * 6 + [("workbuddy", "200")] * 6)
        old = os.environ.get("MAGPIE_USAGE_PATH")
        os.environ["MAGPIE_USAGE_PATH"] = path
        try:
            mc._err_cache = (0.0, {})
            errs = mc.recent_failures()
            blocked = mc._error_blocked(errs)
        finally:
            mc._err_cache = (0.0, {})
            if old is None:
                os.environ.pop("MAGPIE_USAGE_PATH", None)
            else:
                os.environ["MAGPIE_USAGE_PATH"] = old
            os.unlink(path)
        self.assertEqual(errs["dumate"]["ratio"], 1.0)
        self.assertEqual(errs["workbuddy"]["ratio"], 0.0)
        self.assertIn("dumate", blocked)
        self.assertNotIn("workbuddy", blocked)

    def test_missing_ledger_is_not_a_block(self):
        import os
        old = os.environ.get("MAGPIE_USAGE_PATH")
        os.environ["MAGPIE_USAGE_PATH"] = "/nonexistent/usage.jsonl"
        try:
            mc._err_cache = (0.0, {})
            errs = mc.recent_failures()
        finally:
            mc._err_cache = (0.0, {})
            if old is None:
                os.environ.pop("MAGPIE_USAGE_PATH", None)
            else:
                os.environ["MAGPIE_USAGE_PATH"] = old
        self.assertEqual(errs, {})
        self.assertEqual(mc._error_blocked(errs), {})

    def test_degraded_provider_is_excluded_from_tiers(self):
        with mock.patch.object(mc, "models", lambda ttl=None: (self._models(), "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: (_fake_health(blocked=("dumate",)), "")):
            tiers, err = mc.derive_tiers()
            self.assertEqual(err, "")
            self.assertFalse(any(m.startswith("dumate/")
                                 for v in tiers.values() for m in v))

    @staticmethod
    def _models():
        return [
            {"id": "dumate/a-flash", "pricing": {"prompt": "0.0000001"}},
            {"id": "raccoon/a-flash", "pricing": {"prompt": "0.0000002"}},
            {"id": "raccoon/b", "pricing": {"prompt": "0.000002"}},
        ]


class TestFlashTilt(unittest.TestCase):
    """flash 倾斜（2026-11-27 用户令）：t1 全 flash、t2 2 flash、t3 质量优先且档间不重复。"""

    def test_off_is_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != "TASK_TIERS_FLASH_FIRST"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mc, "models", lambda ttl=None: (_FAKE_MODELS, "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: (_fake_health(), "")):
            tiers, err = mc.derive_tiers()
            self.assertEqual(err, "")
            self.assertFalse(mc._flash_first())

    def test_on_fills_flash_and_dedupes(self):
        env = dict(os.environ)
        env["TASK_TIERS_FLASH_FIRST"] = "on"
        # 三个 flash 候选（dumate 两个 + raccoon 一个），保证 t1 能装满三席
        fake = _FAKE_MODELS + [
            {"id": "raccoon/raccoon-flash", "pricing": {"prompt": "0.0000003"}},
            {"id": "dumate/dumate-flash", "pricing": {"prompt": "0.0000002"}},
            {"id": "dumate/dumate-flash2", "pricing": {"prompt": "0.0000004"}},
        ]
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mc, "models", lambda ttl=None: (fake, "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: (_fake_health(), "")):
            tiers, err = mc.derive_tiers()
            self.assertEqual(err, "")
            flat = [m for v in tiers.values() for m in v]
            self.assertEqual(len(flat), len(set(flat)), "档间不许重复")
            self.assertTrue(all(mc.is_flash(m) for m in tiers["t1"]))
            # t3 首项必须是质量项（非 flash），不能为省钱把难题也压到 flash
            self.assertFalse(mc.is_flash(tiers["t3"][0]))

    def test_partial_tiers_are_rejected(self):
        env = dict(os.environ)
        env["TASK_TIERS_FLASH_FIRST"] = "on"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mc, "models",
                                  lambda ttl=None: ([{"id": "dumate/x"}], "")), \
                mock.patch.object(mc, "provider_health",
                                  lambda ttl=None: ({"dumate": {"state": "ok", "reason": ""}}, "")):
            tiers, err = mc.derive_tiers()
            self.assertIsNone(tiers)
            self.assertIn("不完整", err)


class TestTiercfgWiring(unittest.TestCase):
    def test_default_matches_hardcoded(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TASK_TIERS", "TASK_TIERS_SOURCE")}
        with mock.patch.dict(os.environ, env, clear=True):
            got = tiercfg.parse_tiers()
        self.assertEqual(got, {k: list(v) for k, v in tiercfg.DEFAULT_TIERS.items()})

    def test_magpie_source_used_when_enabled(self):
        env = {k: v for k, v in os.environ.items() if k != "TASK_TIERS"}
        env["TASK_TIERS_SOURCE"] = "magpie"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(tiercfg, "_derived_tiers",
                                  lambda: {"t1": ["x/a"], "t2": ["x/b"], "t3": ["x/c"]}):
            self.assertEqual(tiercfg.parse_tiers()["t1"], ["x/a"])

    def test_derive_failure_falls_back(self):
        env = {k: v for k, v in os.environ.items() if k != "TASK_TIERS"}
        env["TASK_TIERS_SOURCE"] = "magpie"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(tiercfg, "_derived_tiers", lambda: {}):
            self.assertEqual(tiercfg.parse_tiers(),
                             {k: list(v) for k, v in tiercfg.DEFAULT_TIERS.items()})

    def test_explicit_raw_wins_over_env(self):
        env = dict(os.environ)
        env["TASK_TIERS_SOURCE"] = "magpie"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(tiercfg, "_derived_tiers", lambda: {"t1": ["x/a"]}):
            self.assertEqual(tiercfg.parse_tiers("t1=manual/m")["t1"], ["manual/m"])

    def test_first_usable_skips_blocked(self):
        tiers = {"t1": ["workbuddy/a", "raccoon/b"]}
        cat = {"workbuddy/a", "raccoon/b"}
        self.assertEqual(
            tiercfg.first_usable("t1", tiers, cat, True, {"workbuddy": "exhausted"}),
            ("raccoon/b", "ok"))
        m2, why2 = tiercfg.first_usable(
            "t1", tiers, cat, True, {"workbuddy": "exhausted", "raccoon": "empty"})
        self.assertIsNone(m2)
        self.assertTrue(why2.startswith("provider_blocked:"))


class TestCostMatch(unittest.TestCase):
    BASE = dict(model="workbuddy/x",
                started_at="2026-11-27T00:00:00+00:00",
                finished_at="2026-11-27T00:01:00+00:00")

    @staticmethod
    def _row(req, credits="1.5", ttft="900"):
        return {"req": req, "status": "200", "credits": credits, "ttft_ms": ttft,
                "_t": datetime(2026, 11, 27, 0, 0, 30, tzinfo=timezone.utc)}

    def test_exact(self):
        got = cb.match_task(self.BASE, [self._row("workbuddy/x")])
        self.assertEqual(got["cost_confidence"], "exact")
        self.assertEqual(got["credits"], 1.5)

    def test_approx_when_multiple(self):
        got = cb.match_task(self.BASE, [self._row("workbuddy/x"), self._row("workbuddy/x")])
        self.assertEqual(got["cost_confidence"], "approx")
        self.assertEqual(got["matches"], 2)

    def test_none_is_not_zero(self):
        got = cb.match_task(self.BASE, [self._row("other/y")])
        self.assertEqual(got["cost_confidence"], "none")
        self.assertNotIn("credits", got)


if __name__ == "__main__":
    unittest.main()
