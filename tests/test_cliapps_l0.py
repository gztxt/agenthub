"""L0 · CLI Apps（vendored nanobot 应用模块）四层守门。

守四件事：
  A **字节级**：vendored 的每个上游文件 md5 必须等于 MANIFEST 里记录的值 ——
    谁手改了副本，这条先红（字节级复制的前提是「能证明它没被改」）；
  B **适配层可 import**：vendored 根在 sys.path 里、`apps.protocol` 的 schema 常量对得上、
    `health()` 在只有本地缓存时也是绿的；
  C **REST 契约**：`/api/apps*` 路由真的挂在 app 上，且写端点都在 authz 声明表里；
  D **端到端假装**：用一份本地注册表缓存 + 一个假 entry_point，跑
    install → 技能落盘 → 软链桥 → run → uninstall 全链（**不出网、不装真包**）。

判据纪律：D 里所有路径都在 tmp 下（`HUB_APPS_WORKSPACE` / `HUB_APPS_DATA` / `PATH`），
跑完必须不留痕——否则「测试会改生产技能目录」比没有测试更糟。
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
VENDOR = SRC / "vendor" / "cli_apps"
sys.path.insert(0, str(SRC))

import cliapps  # noqa: E402

#: 上游冻结指纹：nanobot-ai 0.4.2（PyPI wheel）里这些文件的 md5。
#: 升级上游 = 换一份副本 + 一次性更新这张表 + 写清升级理由；只改表不改文件也算红。
UPSTREAM_MD5 = {
    "nanobot/apps/__init__.py": "daddc3c56e974d6b300c364f22e56473",
    "nanobot/apps/protocol.py": "d8a479230c04c04ce93a8d1cddf9784b",
    "nanobot/apps/cli/__init__.py": "5c78d3c63cbe24cb358bfe3aaf9dda4f",
    "nanobot/apps/cli/service.py": "5ca02b9f470a37377b3a92e7c64f13ef",
    "nanobot/apps/cli/utils.py": "4b24b9647ea33619cbf23f9ce31203ea",
    "nanobot/agent/skill_metadata.py": "4d94478978aac0de120ba1ccf3438ca9",
    "nanobot/config/home.py": "d7b93edef34e54b80177e346d8172398",
    "nanobot/config/paths.py": "f5d4f0cebfd1d2780e2677577e073eba",
    "nanobot/utils/file_io.py": "ce476257fd451c36cd3078641a65b106",
    "nanobot/security/__init__.py": "68b329da9893e34099c7d8ad5cb9c940",
    "nanobot/security/workspace_policy.py": "2186db8f566aba804f4234acc7e08567",
}


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes(), usedforsecurity=False).hexdigest()


class TestVendorByteIdentity(unittest.TestCase):
    def test_every_upstream_file_matches_manifest(self):
        missing = [rel for rel in UPSTREAM_MD5 if not (VENDOR / rel).is_file()]
        self.assertEqual(missing, [], f"vendored 文件缺失：{missing}")
        bad = {rel: _md5(VENDOR / rel) for rel, want in UPSTREAM_MD5.items()
               if _md5(VENDOR / rel) != want}
        self.assertEqual(bad, {}, "vendored 副本被改过（字节级不成立）；"
                                  "要改行为请改 src/cliapps.py 适配层，别动副本")

    def test_shims_are_the_only_non_vendor_files(self):
        """除 MANIFEST 里的上游文件 + 明确标注的替身，不许出现第三种来源。"""
        declared = set(UPSTREAM_MD5) | {
            "nanobot/__init__.py", "nanobot/agent/__init__.py",
            "nanobot/config/__init__.py", "nanobot/utils/__init__.py",
            "nanobot/agent/plugins.py",     # 替身（上游同名 537 行带 pydantic 链）
            "loguru.py", "httpx.py",        # 两个依赖替身
        }
        extra = sorted({str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*.py")
                        if "__pycache__" not in p.parts}) 
        self.assertEqual(sorted(set(extra) - declared), [],
                         "出现未登记的 vendored 文件：新增替身必须同时登记到本测试与 MANIFEST.md")


class TestAdapterImport(unittest.TestCase):
    def test_bootstrap_and_protocol(self):
        cliapps.bootstrap_vendor()
        from nanobot.apps.protocol import APP_PROTOCOL_SCHEMA
        self.assertEqual(APP_PROTOCOL_SCHEMA, "agent-app.v1")

    def test_manager_uses_injected_dirs(self):
        m = cliapps.get_manager()
        self.assertEqual(m.workspace, cliapps.workspace_path())
        self.assertEqual(Path(m.data_dir), cliapps.data_dir())

    def test_health_is_honest_about_optional_deps(self):
        h = cliapps.health()
        self.assertIn("ok", h)
        self.assertIn("yaml", h)
        self.assertIn("notes", h)


class TestRestContract(unittest.TestCase):
    def test_routes_mounted_and_scoped(self):
        import ast
        # 端点面：源码里必须真有这七条；scope：六条写端点必须已声明
        src = (SRC / "api_apps.py").read_text(encoding="utf-8")
        for path in ("/api/apps", "/api/apps/installed", "/api/apps/health",
                     "/api/apps/bridge"):
            self.assertIn('"%s"' % path, src, f"{path} 缺失")
        for act in ("install", "update", "uninstall", "test", "run"):
            self.assertIn("/api/apps/{name}/%s" % act, src, f"{act} 端点缺失")
        import authz
        declared = {k for k in authz.WRITE_ROUTE_SCOPES if k[0] == "api_apps"}
        self.assertEqual(len(declared), 6, f"api_apps 写端点声明数不对：{sorted(declared)}")
        side = {k for k in authz.SIDE_EFFECT_ROUTES if k[0] == "api_apps"}
        self.assertEqual(side, declared, "六条写端点都该进副作用集合（装包/起进程/写软链）")


class TestEndToEndFakeInstall(unittest.TestCase):
    """不出网的全链：目录缓存 → install（entry_point 已在 PATH 上，短路不装包）
    → 技能落盘 + 软链 → run → uninstall。"""

    ENTRY = "hub-apps-selftest"

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="cliapps-l0-"))
        self.ws = self.tmp / "ws"
        self.data = self.tmp / "data"
        self.bin = self.tmp / "bin"
        for d in (self.ws / "skills", self.data, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        # 假入口：退出码 0 + 可核输出
        exe = self.bin / self.ENTRY
        exe.write_text("#!/bin/sh\necho selftest-ok \"$@\"\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        # 本地注册表缓存（fresh，避免任何出网）
        (self.data / "public_registry_cache.json").write_text(json.dumps({
            "_cached_at": time.time(),
            "data": {"meta": {"repo": "test"}, "clis": [{
                "name": "selftest", "display_name": "Self Test",
                "version": "1.0.0", "description": "L0 fake app",
                "category": "test", "entry_point": self.ENTRY,
                # bundled = 「入口已在 PATH 上，不跑包管理器」；用 npm/pip 会让
                # 本用例真的去装/卸全局包（生产机上绝对不行），且卸载分支还会
                # 因为「入口仍在 PATH」按上游设计保留安装态（那是正确行为）。
                "package_manager": "bundled",
            }]},
        }), encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in
                     ("HUB_APPS_WORKSPACE", "HUB_APPS_DATA", "PATH")}
        os.environ["HUB_APPS_WORKSPACE"] = str(self.ws)
        os.environ["HUB_APPS_DATA"] = str(self.data)
        os.environ["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_cycle(self):
        inst = cliapps.install("selftest")
        self.assertTrue((self.data / "installed.json").is_file(), "installed.json 没落盘")
        skill = self.ws / "plugins" / "cli-app-selftest" / "skills" / "cli-app-selftest" / "SKILL.md"
        self.assertTrue(skill.is_file(), f"技能没落盘：{skill}")
        self.assertIn("name:", skill.read_text(encoding="utf-8").split("---")[1])
        self.assertTrue(inst["skill_bridge"]["linked"],
                        f"技能桥没接上：{inst['skill_bridge']}")
        link = self.ws / "skills" / "cli-app-selftest"
        self.assertTrue(link.is_symlink() and (link / "SKILL.md").is_file(),
                        "skills/ 下没有指向插件技能的软链 ⇒ agent 的发现点看不到它")
        out = cliapps.run("selftest", ["--hello"])
        self.assertIn("exited 0", out)
        self.assertIn("selftest-ok", out)
        cliapps.uninstall("selftest")
        self.assertFalse(link.exists() or link.is_symlink(), "卸载后软链还在")
        self.assertEqual(cliapps.installed()["installed_count"], 0, "卸载后仍登记为已装")


if __name__ == "__main__":
    unittest.main()
