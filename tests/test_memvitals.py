#!/usr/bin/env python3
"""L0 hermetic：记忆中心健康探针（src/memvitals.py，PT-20261018-08 P0）。

★ 本闸门钉的是**判据本身**，不是探测结果：
  1. 每个非 ok 项必须带 reason —— 只返回布尔就是缺陷（「红点没有理由」等于没红）；
  2. 必须能红：造一个必红样本（源不可达）断言 verdict=error 且点名；
  3. 不得把「探针自身失败」渲染成「零命中」；
  4. 只读：模块不得出现 INSERT/UPDATE/DELETE，且投影库必须以 mode=ro 打开。

为什么这么严：本机三次同类事故都是「指标绿而功能死」（claude-mem 静默停更 15 天、
TDAI 单源吃满 200/200、memindex note 截断吃掉 last_build）。探针自己长出恒真判据，
就等于把事故常态化。
"""
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import memvitals  # noqa: E402

NOW = datetime(2026, 10, 18, 21, 0, 0, tzinfo=timezone.utc)


def src(id_, ok=True, enabled=True, status=None, reason="", count=None):
    return {"id": id_, "label": id_, "enabled": enabled, "ok": ok,
            "status": status or ("ok" if ok else "error"), "reason": reason,
            "count": count, "fresh_at": None, "note": ""}


def proj(ok=True, last_build="2026-10-18T10:30:00", chunks=100, by_source=None):
    return {"ok": ok, "files": 10, "chunks": chunks, "last_build": last_build,
            "by_source": by_source if by_source is not None else {"pi_sessions": {"chunks": chunks, "fresh_at": None}},
            "ms": 1.0}


def probe(id_, status="ok", reason="", took=10.0):
    return {"id": id_, "layer": "L2", "name": id_, "status": status,
            "reason": reason, "hard": True, "evidence": {"took_ms": took}}


def good_probes():
    return [probe("probe:disk"), probe("probe:ccr"), probe("probe:backup"),
            probe("probe:reverse"), probe("probe:context")]


class TestRedMustHaveReason(unittest.TestCase):
    def test_every_non_ok_check_has_reason(self):
        """判据：非 ok 必须有理由，且不能是一句空话。"""
        reps = [
            memvitals.build_report([src("amr_memory", ok=False, reason="不可达：路径不存在")],
                                   proj(), good_probes(), None, NOW),
            memvitals.build_report([src("pi_sessions")], proj(last_build="2026-10-15T00:00:00"),
                                   good_probes(), None, NOW),
            memvitals.build_report([src("pi_sessions")], proj(chunks=100, by_source={"pi_sessions": {"chunks": 99}}),
                                   good_probes(), None, NOW),
            memvitals.build_report([src("pi_sessions")], proj(),
                                   good_probes()[:2] + [probe("probe:reverse", "error", "返回 3 条")], None, NOW),
        ]
        for rep in reps:
            for c in rep["checks"]:
                if c["status"] != "ok":
                    self.assertTrue(c["reason"].strip(), f"{c['id']} 红而无理由")
                    self.assertGreater(len(c["reason"]), 4, f"{c['id']} 理由过短")

    def test_anomalies_mirror_checks(self):
        rep = memvitals.build_report([src("amr_memory", ok=False, reason="路径不存在")],
                                     proj(), good_probes(), None, NOW)
        bad = [c["id"] for c in rep["checks"] if c["status"] != "ok"]
        self.assertEqual(sorted(bad), sorted(a["id"] for a in rep["anomalies"]))


class TestMustBeAbleToGoRed(unittest.TestCase):
    def test_unreachable_source_turns_error_and_names_it(self):
        rep = memvitals.build_report([src("amr_memory", ok=False, reason="不可达：路径不存在")],
                                     proj(), good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "error")
        self.assertIn("amr_memory", json.dumps(rep["anomalies"], ensure_ascii=False))
        self.assertTrue(any(a["id"] == "l0:amr_memory" for a in rep["anomalies"]))

    def test_all_green_is_ok(self):
        """反面：全绿不能是 error —— 否则探针是恒红，同样无效。"""
        rep = memvitals.build_report([src("pi_sessions")], proj(), good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "ok", rep["anomalies"])

    def test_disabled_source_is_off_not_red(self):
        """停用 ≠ 故障：amr 裁定停用后不许再天天报红。"""
        rep = memvitals.build_report(
            [src("amr_memory", ok=False, enabled=False, status="off", reason="已停用")],
            proj(), good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "ok")
        self.assertFalse(any(a["id"].startswith("l0:") for a in rep["anomalies"]))

    def test_rows_drift_is_error(self):
        rep = memvitals.build_report([src("pi_sessions")], proj(chunks=100, by_source={"pi_sessions": {"chunks": 90}}),
                                     good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "error")
        self.assertTrue(any(a["id"] == "l3:rows" for a in rep["anomalies"]))

    def test_reverse_probe_red_on_anything_returned(self):
        p = good_probes()
        p[3] = probe("probe:reverse", "error", "不可能命中的词返回 3 条")
        rep = memvitals.build_report([src("pi_sessions")], proj(), p, None, NOW)
        self.assertEqual(rep["verdict"], "error")

    def test_context_zero_chars_is_error(self):
        p = good_probes()
        p[4] = probe("probe:context", "error", "注入包 0 字符")
        rep = memvitals.build_report([src("pi_sessions")], proj(), p, None, NOW)
        self.assertEqual(rep["verdict"], "error")


class TestFreshness(unittest.TestCase):
    def test_projection_fresh(self):
        rep = memvitals.build_report([src("pi_sessions")], proj(last_build="2026-10-18T10:30:00"),
                                     good_probes(), None, NOW)
        c = next(c for c in rep["checks"] if c["id"] == "l1:projection")
        self.assertEqual(c["status"], "ok")

    def test_projection_warn_at_36h(self):
        lb = (NOW - timedelta(hours=40)).isoformat(timespec="seconds")
        rep = memvitals.build_report([src("pi_sessions")], proj(last_build=lb), good_probes(), None, NOW)
        c = next(c for c in rep["checks"] if c["id"] == "l1:projection")
        self.assertEqual(c["status"], "warn")
        self.assertIn("40", c["reason"])

    def test_projection_error_at_72h(self):
        lb = (NOW - timedelta(hours=80)).isoformat(timespec="seconds")
        rep = memvitals.build_report([src("pi_sessions")], proj(last_build=lb), good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "error")

    def test_naive_timestamp_is_read_as_local(self):
        """meta 里的 last_build 是本地时间（systemd 写的），不能当 UTC。"""
        local_now = NOW.astimezone()
        lb = (local_now - timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="seconds")
        age = memvitals._age_hours(lb, NOW)
        self.assertIsNotNone(age)
        self.assertLess(abs(age - 1.0), 0.2, f"age={age}")


class TestLatencyStrikes(unittest.TestCase):
    def test_single_spike_does_not_warn(self):
        """判例 106：单次读数自带不确定度，一次抖动不许报劣化。"""
        prev = {"history": [{"probes": [{"id": "probe:disk", "took_ms": 20}]}],
                "checks": [], "sources": []}
        p = good_probes()
        p[0]["evidence"]["took_ms"] = 200.0
        rep = memvitals.build_report([src("pi_sessions")], proj(), p, prev, NOW)
        self.assertEqual(rep["verdict"], "ok")

    def test_repeated_spike_warns(self):
        """连续 3 次超基线才报 —— 基线取全长中位数（17 次 20ms + 3 次 200ms ⇒ 基线 20ms）。"""
        hist = [{"probes": [{"id": "probe:disk", "took_ms": 20}]} for _ in range(17)]
        hist += [{"probes": [{"id": "probe:disk", "took_ms": 200}]} for _ in range(3)]
        prev = {"history": hist, "checks": [], "sources": []}
        p = good_probes()
        p[0]["evidence"]["took_ms"] = 200.0
        rep = memvitals.build_report([src("pi_sessions")], proj(), p, prev, NOW)
        self.assertEqual(rep["verdict"], "warn")
        self.assertTrue(any(a["id"] == "lat:probe:disk" for a in rep["anomalies"]))


class TestSchemaAndHistory(unittest.TestCase):
    def test_schema_version_is_stable(self):
        """schema 变更必须显式改常量：跨版本比较趋势会失真。"""
        self.assertEqual(memvitals.SCHEMA, 1)

    def test_history_capped(self):
        prev = {"history": [{"as_of": "x", "verdict": "ok", "probes": []}] * (memvitals.HISTORY_MAX + 10)}
        rep = memvitals.build_report([src("pi_sessions")], proj(), good_probes(), prev, NOW)
        self.assertEqual(len(rep["history"]), memvitals.HISTORY_MAX)

    def test_required_keys(self):
        rep = memvitals.build_report([src("pi_sessions")], proj(), good_probes(), None, NOW)
        for k in ("schema", "as_of", "verdict", "summary", "anomalies", "notes",
                  "checks", "sources", "probes"):
            self.assertIn(k, rep)
        self.assertIn(rep["verdict"], ("ok", "warn", "error"))


class TestInfoIsNotWarn(unittest.TestCase):
    """已知的、可接受的陈旧（本地 4 条演示便签）不能把整页永久染黄。

    这是「狼来了」的反面：15 天静默停更的教训要求**可见**，不是要求**报警**。
    """

    def test_stale_local_l1_is_info_and_keeps_verdict_ok(self):
        s = src("local", count=4)
        s["fresh_at"] = "2026-09-06"
        rep = memvitals.build_report([s, src("pi_sessions")], proj(), good_probes(), None, NOW)
        self.assertEqual(rep["verdict"], "ok")
        self.assertTrue(any(n["id"] == "l1:local" for n in rep["notes"]))
        self.assertFalse(any(a["id"] == "l1:local" for a in rep["anomalies"]))
        n = next(n for n in rep["notes"] if n["id"] == "l1:local")
        self.assertIn("TDAI", n["reason"], "理由必须说明为什么不算故障")

    def test_semantic_floor_is_noted_not_red(self):
        p = good_probes()
        p[3]["evidence"]["semantic_floor"] = {"count": 3, "top_score": 0.77}
        rep = memvitals.build_report([src("pi_sessions")], proj(), p, None, NOW)
        self.assertEqual(rep["verdict"], "ok")
        self.assertTrue(any(n["id"] == "note:semantic_floor" for n in rep["notes"]))


class TestReadOnly(unittest.TestCase):
    def test_no_write_sql(self):
        text = (pathlib.Path(memvitals.__file__)).read_text(encoding="utf-8")
        for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "TRUNCATE"):
            self.assertNotIn(bad, text, f"探针不得出现写 SQL：{bad}")

    def test_projection_opened_readonly(self):
        text = (pathlib.Path(memvitals.__file__)).read_text(encoding="utf-8")
        self.assertIn("mode=ro", text)


class TestAtomicWrite(unittest.TestCase):
    def test_write_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "memvitals.json"
            rep = memvitals.build_report([src("pi_sessions")], proj(), good_probes(), None, NOW)
            memvitals.write_report(rep, out)
            got = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(got["verdict"], "ok")
            self.assertEqual([p.name for p in out.parent.iterdir()], ["memvitals.json"],
                             "临时文件必须被清理，不能留 .tmp 在目录里")


if __name__ == "__main__":
    unittest.main(verbosity=2)
