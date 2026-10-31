"""PWA manifest 的 icon purpose 与 canvas 回读属性（2026-10-31，两条控制台实测告警）。

判据来自浏览器原话，不是猜测：

1. `Manifest: found icon with no valid purpose; ignoring it.`
   ⇒ `purpose` 的合法词是 `any` / `maskable` / `monochrome`（Web App Manifest 规范）。
   `"default"` **不是**合法值 ⇒ Chrome 直接丢掉这两枚图标，装成 PWA 后没有 app 图标。
2. `Canvas2D: Multiple readback operations using getImageData are faster with the
   willReadFrequently attribute set to true.`
   ⇒ 那块画布的唯一用途是 readback 量字形墨水，必须显式声明。

不导入 src.main（L0 口径），只读文本。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "static" / "manifest.json"
CARDS = ROOT / "static" / "hub" / "03-agents-cards.js"

VALID_PURPOSES = {"any", "maskable", "monochrome"}


class TestManifestIcons(unittest.TestCase):
    def test_every_icon_has_valid_purpose(self):
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(d.get("icons") or []), 1, "manifest 没有图标")
        for ic in d["icons"]:
            toks = set((ic.get("purpose") or "any").split())
            bad = toks - VALID_PURPOSES
            self.assertFalse(
                bad,
                f"{ic['src']} 的 purpose={ic.get('purpose')!r} 含非法词 {sorted(bad)}"
                f" ⇒ 浏览器会整枚忽略（合法值只有 {sorted(VALID_PURPOSES)}）")

    def test_has_maskable_icon(self):
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertTrue(any("maskable" in (ic.get("purpose") or "").split()
                            for ic in d["icons"]),
                        "缺 maskable 图标 ⇒ Android 装桌面后会裁成白框")


class TestCanvasReadback(unittest.TestCase):
    def test_terms_ink_canvas_sets_will_read_frequently(self):
        src = CARDS.read_text(encoding="utf-8")
        # 定位那块 48x48 量墨水的画布：它必然是 getContext('2d') 后紧跟 getImageData
        seg = src[src.index("function termCjkUsable"):]
        # 从 getContext 往后取到 getImageData：注释里也出现了 "getImageData" 这个词，
        # 从函数名开始硬切会把注释算进切片里 ⇒ 断言对着注释假绿（本闸门第一版就这样红过）。
        seg = seg[seg.index("getContext"):]
        seg = seg[:seg.index("getImageData")]
        self.assertRegex(seg, r"getContext\('2d',\s*\{\s*willReadFrequently:\s*true\s*\}\)",
                         "量墨水画布未声明 willReadFrequently ⇒ Chrome 走同步回读慢路径")
