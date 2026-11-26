#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：agenthub × magpie 只读互调（2026-11-27）。

【本文件守三件事】任何一件失守都会让「互调」静默退化成「写死」：
1. `gwurl` 不把 `/v1` 拼两次 —— 2026-11-27 实测 remote judge 一直在打
   `/v1/v1/chat/completions`（网关 404），journal 里是 `judge_fail kind=chain`。
2. 档位派生**默认关闭**，关着时与写死表逐字相同（kill switch 不许被悄悄绕过）。
3. 额度闸门只挡被挡 provider；**读不到额度时不挡**（不把「不知道」当「没额度」）。
"""
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
        with mock.patch.object(mc, "quotas", lambda ttl=None: (cards, "")):
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
