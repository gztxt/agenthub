#!/usr/bin/env python3
"""L0 hermetic：静态资源服务的**外部 interface**（`staticguard.serve` / `to_response`）。

2026-10-12 C1 深化新增。此前组装（ETag / gzip / 分支选择 / 返回路径）内联在 main.py 的
56 行路由里、无法离线测（import src.main 会跑 lifespan）。本文件把整条策略的**可观察行为**
钉在 tempfile 造的目录树上 —— 不 import src.main、不起服务、不打网络、不 fork pty，
换任何机器结论一致。

golden 矩阵逐条对应冻结前的生产实测（2026-10-12，:3102）：
  no ?v → 200 revalidate（no-cache + ETag，FileResponse 故带 accept-ranges）；
  ?v==内容哈希 → 200 immutable（全字节，**不发任何校验器**）；
  ?v 写错 → 退回 revalidate；Accept-Encoding: gzip → Content-Encoding: gzip；
  If-None-Match 命中 → 304（带 Vary）；.bak / 逃逸 / 不存在 → 404。

端到端那一份（真起影子实例、含红臂自证）在 `tests/verify_static_headers_matrix.py`（L2）。
"""
import gzip
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import staticguard as g          # noqa: E402


class TestServeGoldenMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-serve-")
        self.root = Path(self.tmp.name)
        self.sibling = self.root.parent / (self.root.name + "_evil")   # 逃逸靶
        self.sibling.mkdir(exist_ok=True)
        (self.sibling / "secret.txt").write_text("CANARY-DO-NOT-SERVE")

        self.big = self.root / "hub.js"
        self.big.write_bytes(b"console.log(1);\n" * 120)               # 1800B > _GZ_MIN
        self.tok = g.content_token(self.big)
        self.small = self.root / "tiny.css"
        self.small.write_bytes(b"a{}")                                 # < _GZ_MIN
        self.txt = self.root / "notes.txt"
        self.txt.write_bytes(b"hello\n" * 10)
        self.blob = self.root / "blob.zzz"
        self.blob.write_bytes(b"\x00\x01\x02")
        (self.root / "hub.js.bak-x").write_text("secret-backup")

    def tearDown(self):
        import shutil
        self.tmp.cleanup()
        shutil.rmtree(self.sibling, ignore_errors=True)

    # ── ① revalidate：无提手 ⇒ 可复验路径（FileResponse） ─────────────────
    def test_no_token_revalidate_file_backed(self):
        p = g.serve(self.root, "hub.js")
        self.assertEqual(p.status, 200)
        self.assertTrue(p.is_file_backed)                 # ⇒ 路由走 FileResponse（带 accept-ranges）
        self.assertEqual(p.headers["Cache-Control"], "no-cache")
        self.assertIn("ETag", p.headers)
        self.assertEqual(p.headers["Vary"], "Accept-Encoding")
        self.assertEqual(p.headers["X-Asset-Token"], self.tok)
        self.assertNotIn("Content-Encoding", p.headers)
        self.assertIn("javascript", p.media_type)

    # ── ② immutable：提手==内容哈希 ⇒ 全字节、零校验器 ───────────────────
    def test_correct_token_immutable_full_bytes(self):
        p = g.serve(self.root, "hub.js", self.tok)
        self.assertEqual(p.status, 200)
        self.assertFalse(p.is_file_backed)                # ⇒ 路由走 Response(bytes)，不带 accept-ranges
        self.assertEqual(p.body, self.big.read_bytes())
        self.assertEqual(p.headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertNotIn("ETag", p.headers)               # ★ 校验器一个都不许有
        self.assertNotIn("Last-Modified", p.headers)

    def test_immutable_never_304(self):
        """★安全阀：immutable 不参与协商，带条件头仍 200（换新体只能靠 URL 变化）。"""
        p = g.serve(self.root, "hub.js", self.tok, "", '"anything"')
        self.assertEqual(p.status, 200)
        self.assertFalse(p.is_file_backed)

    def test_wrong_token_falls_back_to_revalidate(self):
        p = g.serve(self.root, "hub.js", "deadbeef")
        self.assertEqual(p.status, 200)
        self.assertTrue(p.is_file_backed)
        self.assertIn("ETag", p.headers)

    # ── ③ 304 ────────────────────────────────────────────────────────────
    def test_if_none_match_returns_304(self):
        etag = g.serve(self.root, "hub.js").headers["ETag"]
        p = g.serve(self.root, "hub.js", "", "", etag)
        self.assertEqual(p.status, 304)
        self.assertIsNone(p.body)
        self.assertIsNone(p.file)
        self.assertIsNone(p.media_type)                   # 逐字对齐：304 不带 content-type
        self.assertEqual(p.headers["ETag"], etag)
        self.assertEqual(p.headers["Vary"], "Accept-Encoding")

    # ── ④ gzip ───────────────────────────────────────────────────────────
    def test_gzip_immutable(self):
        p = g.serve(self.root, "hub.js", self.tok, "gzip")
        self.assertEqual(p.status, 200)
        self.assertEqual(p.headers["Content-Encoding"], "gzip")
        self.assertNotIn("ETag", p.headers)
        self.assertTrue(p.body.startswith(b"\x1f\x8b"))
        self.assertEqual(gzip.decompress(p.body), self.big.read_bytes())

    def test_gzip_revalidate_keeps_etag(self):
        p = g.serve(self.root, "hub.js", "", "gzip")
        self.assertEqual(p.headers["Content-Encoding"], "gzip")
        self.assertIn("ETag", p.headers)                  # revalidate 的校验器不得被 gzip 分支吞掉

    def test_small_file_not_gzipped(self):
        p = g.serve(self.root, "tiny.css", g.content_token(self.small), "gzip")
        self.assertEqual(p.status, 200)
        self.assertNotIn("Content-Encoding", p.headers)
        self.assertEqual(p.body, self.small.read_bytes())

    def test_noncompressible_suffix_not_gzipped(self):
        p = g.serve(self.root, "notes.txt", g.content_token(self.txt), "gzip")
        self.assertNotIn("Content-Encoding", p.headers)

    def test_gzip_cache_is_bounded(self):
        for i in range(40):
            f = self.root / ("f%02d.js" % i)
            f.write_bytes(b"x" * 1500 + str(i).encode())
            g.serve(self.root, f.name, g.content_token(f), "gzip")
        self.assertLessEqual(len(g._gz_cache), 33)         # 上界：>32 即清空，故最多 33

    # ── ⑤ 404 语义 ───────────────────────────────────────────────────────
    def test_404_for_missing_backup_and_escape(self):
        for fp in ("nope.js", "hub.js.bak-x", "../%s_evil/secret.txt" % self.root.name):
            p = g.serve(self.root, fp)
            self.assertEqual(p.status, 404, "应 404：%s" % fp)
            self.assertEqual(p.headers, {})
            self.assertIsNone(p.body)
            self.assertIsNone(p.file)

    def test_media_type_fallback_is_octet_stream(self):
        p = g.serve(self.root, "blob.zzz", g.content_token(self.blob))
        self.assertEqual(p.media_type, "application/octet-stream")


class TestToResponseAdapter(unittest.TestCase):
    """`to_response` 是唯一的框架耦合点 —— 钉住「哪种 plan 贴成哪种响应」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-emit-")
        self.root = Path(self.tmp.name)
        self.big = self.root / "hub.js"
        self.big.write_bytes(b"console.log(1);\n" * 120)
        self.tok = g.content_token(self.big)

    def tearDown(self):
        self.tmp.cleanup()

    def test_immutable_emits_plain_response_without_validators(self):
        from starlette.responses import FileResponse, Response
        r = g.to_response(g.serve(self.root, "hub.js", self.tok))
        self.assertIsInstance(r, Response)
        self.assertNotIsInstance(r, FileResponse)          # ⇒ 不会补出 accept-ranges
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("etag", {k.lower() for k in r.headers})

    def test_file_backed_emits_fileresponse(self):
        from starlette.responses import FileResponse
        self.assertIsInstance(g.to_response(g.serve(self.root, "hub.js")), FileResponse)

    def test_304_and_404_emit_correct_status(self):
        etag = g.serve(self.root, "hub.js").headers["ETag"]
        self.assertEqual(g.to_response(g.serve(self.root, "hub.js", "", "", etag)).status_code, 304)
        self.assertEqual(g.to_response(g.serve(self.root, "nope.js")).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)