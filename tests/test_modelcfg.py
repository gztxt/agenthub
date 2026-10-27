#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：各 Agent 默认模型统一设置（src/modelcfg.py，v0.13.41）。

为什么这些用例必须存在：
1. **落点是白名单，不是通用写手**：每家 agent 的模型字段形状不同（json / toml /
   yaml / 命名 profile），通用编辑器必然退化成整文件重写 —— 09-07 pi 改 CCR 配置
   把同文件其它 profile 一起改坏就是那类事故。用例逐家钉"只动了目标 key"。
2. **备份是硬前置**：apply 必须在写之前留下一份 `.bak-<时间戳>`，且内容与原文
   逐字节一致；没有备份的写入等于不可回滚。
3. **预览不落笔**：preview 只能算 diff，磁盘字节不许变（这页是给人在设置页里
   看一眼再决定的，预览就写盘是本功能最不能犯的错）。
4. **口令门 fail-closed**：没配 HUB_PASSCODE ⇒ 503（不是放行），错口令 ⇒ 401，
   且拒绝原因里不许回显口令。
5. **终端注入只认白名单**：MODEL_ARGV 之外的 agent 返回 []，绝不猜 flag。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))

import tiers                     # noqa: E402
import db                        # noqa: E402
import modelcfg                  # noqa: E402

# 清单校验（v0.13.167）在本文件里默认关闭：L0 是 hermetic 环境（假 HOME + 假模型 ID），
# 真打 :3456 会让用例依赖网关活着。清单校验本身由 M_CatalogGuard 打桩专测。
# ⚠ 必须写在**本文件**而不是 conftest.py：标准的 `run_tier.py hermetic` 走 unittest
#    discover，压根不加载 conftest ⇒ 那种写法在 pytest 下绿、在 CI 层红（两套判据分叉）。
os.environ.setdefault("HUB_MODEL_CATALOG", "off")


def _mktmp(prefix: str) -> pathlib.Path:
    _L0_TMP.mkdir(parents=True, exist_ok=True)
    p = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=str(_L0_TMP)))
    return tiers.l0_fixture_register(p)      # v0.13.58：登记，退出时统一删（见 tiers 说明）


CLAUDE_JSON = {
    "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456",
            "ANTHROPIC_MODEL": "old/model-a",
            "CCR_CLAUDE_CODE_MODEL": "old/model-a"},
    "model": "old/model-a",
}
JCODE_TOML = """[provider]
default_model = "old/model-a"
default_provider = "ccr"

[providers.ccr]
type = "open-ai-compatible"
base_url = "http://127.0.0.1:3456/v1"
default_model = "old/model-a"
"""
CODEX_TOML = """# BEGIN CCR managed profile
model_provider = "claude-code-router"
model = "old/model-a"
model_catalog_json = "/home/x/.codex/ccr-model-catalog.json"
# CCR configured model = "old/model-a"
# END CCR managed profile
approval_policy = "never"
"""
PI_SETTINGS = {"defaultProvider": "openrouter-direct", "defaultModel": "old/model-a"}
PI_MODELS = {"providers": {"ccr-free": {"api": "openai-completions",
                                        "baseUrl": "http://127.0.0.1:3456/v1",
                                        "apiKey": "k-fixture",
                                        "models": [{"id": "old/model-b"}]}}}
GROK_TOML = """[model.ccr-old]
model = "old/model-a"
base_url = "http://127.0.0.1:3456/v1"
api_key = "key-fixture-ccr"

[models]
default = "ccr-old"
"""
HERMES_YAML = """model:
  default: old/model-a
  provider: some-provider
toolsets:
  - hermes-cli
"""
CCR_CONFIG = {"APIKEY": "ccr-key-fixture", "Router": {"default": "alibaba,qwen3.8-flash"}}

# ── 2026-10-07 新增三家的夹具（形状照本机真实文件取，见各家 read 的注释）──
OPENCODE_JSON = {
    "$schema": "https://opencode.ai/config.json",
    "mcp": {"some-server": {"type": "local", "command": ["node", "x.js"], "enabled": True}},
    "provider": {"ccr": {"npm": "@ai-sdk/openai-compatible", "name": "CCR local router",
                         "options": {"baseURL": "http://127.0.0.1:3456/v1", "apiKey": "k"},
                         "models": {"old/model-b": {"name": "model-b"}}}},
    "model": "ccr/old/model-b",
    "small_model": "ccr/old/model-b",
    "permission": {"bash": "ask"},
}
QODER_JSON = {
    "model": {"name": "Qwen3.8-Flash"},
    "securityScan": {"l1StaticCheck": True, "l3DeepScan": True},
    "permissions": {"additionalDirectories": [], "trustDirectories": ["/fs/1000/ftp/技术文档"]},
    "security": {"auth": {"selectedType": "qoder-browser"}},
}
CURSOR_JSON = {
    "permissions": {"allow": ["Shell(ls)"], "deny": []}, "version": 1,
    "editor": {"vimMode": False}, "display": {"mode": "zen"},
    "model": {"modelId": "old/model-a", "displayModelId": "old/model-a",
              "displayName": "old/model-a", "displayNameShort": "old/model-a", "aliases": []},
    "hasChangedDefaultModel": True,
    "modelParameters": {"old/model-a": []},
    "selectedModel": {"modelId": "old/model-a", "parameters": []},
    "network": {"useHttp1ForAgent": False}, "sandbox": {"mode": "disabled"},
}


class _HomeFixture(unittest.TestCase):
    """造假 HOME：modelcfg 的所有落点都在 ~ 下，换 HOME 即可全隔离。"""

    def setUp(self):
        self.tmp = _mktmp("modelcfg-")
        self.home = self.tmp / "home"
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        db.init_db(self.tmp / "modelcfg.db")
        self._write(".claude/settings.json", json.dumps(CLAUDE_JSON))
        self._write(".jcode/config.toml", JCODE_TOML)
        self._write(".codex/config.toml", CODEX_TOML)
        self._write(".pi/agent/settings.json", json.dumps(PI_SETTINGS))
        self._write(".pi/agent/models.json", json.dumps(PI_MODELS))
        self._write(".grok/config.toml", GROK_TOML)
        self._write(".hermes/config.yaml", HERMES_YAML)
        self._write(".config/opencode/opencode.json", json.dumps(OPENCODE_JSON))
        self._write(".qoder/settings.json", json.dumps(QODER_JSON))
        self._write(".cursor/cli-config.json", json.dumps(CURSOR_JSON))
        self._write(".claude-code-router/config.json", json.dumps(CCR_CONFIG))

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, text: str) -> pathlib.Path:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _read(self, rel: str) -> str:
        return (self.home / rel).read_text(encoding="utf-8")

    def _backups(self, rel: str):
        p = self.home / rel
        return sorted(p.parent.glob(p.name + ".bak-*-hub-modelcfg-*"))


class AReadCurrent(_HomeFixture):
    def test_each_agent_reads_its_own_field(self):
        for aid, want in (("claude", "old/model-a"), ("jcode", "old/model-a"),
                          ("codex", "old/model-a"), ("pi", "old/model-a"),
                          ("grok", "old/model-a"), ("hermes", "old/model-a"),
                          # v0.13.85 新增三家。⚠ opencode 的现值**自带 provider 前缀**
                          # （`ccr/old/model-b`）—— 这是它文档要求的形状，不是脏数据。
                          ("opencode", "ccr/old/model-b"), ("qoder", "Qwen3.8-Flash"),
                          ("cursor", "old/model-a")):
            st = modelcfg.agent_state(aid)
            self.assertTrue(st["supported"], aid)
            self.assertTrue(st["writable"], aid)
            self.assertEqual(st["current"], want, aid)

    def test_unsupported_agents_are_marked_not_writable(self):
        for aid in ("codebuddy", "qwenpaw", "cloudcli"):
            st = modelcfg.agent_state(aid)
            self.assertFalse(st["supported"], aid)
            self.assertFalse(st["writable"], aid)
            self.assertTrue(st["reason"], f"{aid} 的不支持理由必须写清")

    def test_missing_file_does_not_raise(self):
        (self.home / ".hermes" / "config.yaml").unlink()
        st = modelcfg.agent_state("hermes")
        self.assertFalse(st["writable"], "文件没了就不可写，但不许抛异常打断设置页")
        self.assertIn("未找到", st["note"])

    def test_ccr_router_is_read_only_view(self):
        v = modelcfg.ccr_router_view()
        self.assertTrue(v["read_only"], "Router 视图必须自带只读标记（军规：不写网关配置）")
        self.assertEqual(v["router"]["default"], "alibaba,qwen3.8-flash")


class BPreviewNoWrite(_HomeFixture):
    def test_preview_leaves_bytes_untouched(self):
        before = {f: self._read(f) for f in (".claude/settings.json", ".jcode/config.toml",
                                             ".codex/config.toml", ".pi/agent/settings.json",
                                             ".pi/agent/models.json", ".grok/config.toml",
                                             ".hermes/config.yaml",
                                             ".config/opencode/opencode.json",
                                             ".qoder/settings.json", ".cursor/cli-config.json")}
        for aid in ("claude", "jcode", "codex", "pi", "grok", "hermes"):
            modelcfg.preview(aid, "new/model-z")
        # 新三家各自的合法输入形状（opencode 要前缀、qoder 只认原生名）
        modelcfg.preview("opencode", "ccr/new/model-z")
        modelcfg.preview("qoder", "Qwen3.8-Max")
        modelcfg.preview("cursor", "new/model-z")
        for f, was in before.items():
            self.assertEqual(self._read(f), was, f"preview 落笔了：{f}")

    def test_preview_diff_shape(self):
        p = modelcfg.preview("jcode", "new/model-z")
        self.assertEqual(p["agent_id"], "jcode")
        self.assertEqual(p["argv"], ["--model", "new/model-z"])
        self.assertTrue(p["files"])
        self.assertTrue(any(c["to"] == "new/model-z" for c in p["files"][0]["changes"]))

    def test_bad_model_id_rejected(self):
        for bad in ("", "a b", 'x"y', "x\ny", "a" * 200):
            with self.assertRaises(modelcfg.ModelCfgError, msg=f"{bad!r} 本该被拒"):
                modelcfg.validate_model(bad)

    def test_unsupported_agent_rejected(self):
        with self.assertRaises(modelcfg.ModelCfgError):
            modelcfg.preview("codebuddy", "new/model-z")


class CApplyWritesWithBackup(_HomeFixture):
    def test_claude_writes_model_and_env_only(self):
        out = modelcfg.apply_model("claude", "new/model-z")
        d = json.loads(self._read(".claude/settings.json"))
        self.assertEqual(d["model"], "new/model-z")
        self.assertEqual(d["env"]["ANTHROPIC_MODEL"], "new/model-z")
        self.assertEqual(d["env"]["CCR_CLAUDE_CODE_MODEL"], "new/model-z")
        self.assertEqual(d["env"]["CODEXL_CLAUDE_CODE_MODEL"], "new/model-z")
        # 无关键不许被动过（整文件重写会把它冲掉）
        self.assertEqual(d["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:3456")
        baks = self._backups(".claude/settings.json")
        self.assertEqual(len(baks), 1, "必须留且只留一份备份")
        self.assertEqual(baks[0].read_text(encoding="utf-8"), json.dumps(CLAUDE_JSON),
                         "备份内容必须与原文逐字节一致")
        self.assertEqual(out["agent_id"], "claude")

    def test_jcode_surgical_toml_edit(self):
        modelcfg.apply_model("jcode", "new/model-z")
        text = self._read(".jcode/config.toml")
        self.assertIn('default_model = "new/model-z"', text)
        self.assertIn('base_url = "http://127.0.0.1:3456/v1"', text, "无关键被整段重写了")
        self.assertEqual(text.count("default_model"), 2, "provider 与 providers.ccr 各一处")

    def test_codex_only_touches_managed_block(self):
        modelcfg.apply_model("codex", "new/model-z")
        text = self._read(".codex/config.toml")
        self.assertIn('model = "new/model-z"', text)
        self.assertIn('# CCR configured model = "new/model-z"', text)
        self.assertIn('approval_policy = "never"', text, "块外内容不许动")
        self.assertEqual(text.count("# BEGIN CCR managed profile"), 1)

    def test_pi_appends_model_when_missing_from_list(self):
        modelcfg.apply_model("pi", "new/model-z")
        s = json.loads(self._read(".pi/agent/settings.json"))
        self.assertEqual(s["defaultModel"], "new/model-z")
        self.assertEqual(s["defaultProvider"], modelcfg.PI_CCR_PROVIDER)
        m = json.loads(self._read(".pi/agent/models.json"))
        ids = [x["id"] for x in m["providers"]["ccr-free"]["models"]]
        self.assertIn("new/model-z", ids, "pi 只认清单内的模型 id，缺了必须补")
        self.assertIn("old/model-b", ids, "原有清单不许被覆盖")

    def test_pi_new_entry_inherits_ctx_window(self):
        """新增条目必须带上已有声明：缺 contextWindow 会被 pi 兜底成 128000
        （pi-web 回显 128k，压缩阈值也跟着错，见 agent-knowledge/165）。"""
        m = json.loads(self._read(".pi/agent/models.json"))
        m["providers"]["Alibaba"] = {
            "api": "openai-completions",
            "baseUrl": "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1",
            "models": [{"id": "new/model-z", "contextWindow": 1000000, "maxTokens": 384000}],
        }
        self._write(".pi/agent/models.json", json.dumps(m))
        modelcfg.apply_model("pi", "new/model-z")
        mm = json.loads(self._read(".pi/agent/models.json"))
        ent = [x for x in mm["providers"]["ccr-free"]["models"] if x["id"] == "new/model-z"][0]
        self.assertEqual(ent.get("contextWindow"), 1000000, "新增条目丢了上下文窗口")
        self.assertEqual(ent.get("maxTokens"), 384000, "新增条目丢了最大输出")
        # 已有键不许被动过（整文件重写的旧病）
        self.assertEqual(mm["providers"]["ccr-free"]["apiKey"], "k-fixture")

    def test_pi_append_without_evidence_stays_unset(self):
        """两处声明都查不到时宁缺不猜 —— 写个假数会被上游边界拒。"""
        modelcfg.apply_model("pi", "unknown/model-q")
        mm = json.loads(self._read(".pi/agent/models.json"))
        ent = [x for x in mm["providers"]["ccr-free"]["models"] if x["id"] == "unknown/model-q"][0]
        self.assertNotIn("contextWindow", ent)

    def test_grok_reuses_existing_ccr_key_not_hardcoded(self):
        modelcfg.apply_model("grok", "new/model-z")
        text = self._read(".grok/config.toml")
        self.assertIn('default = "ccr-hub"', text)
        self.assertIn('api_key = "key-fixture-ccr"', text, "应复用文件里已有的 CCR key，不硬编码")
        self.assertIn('model = "new/model-z"', text)

    def test_hermes_yaml_block_scope(self):
        modelcfg.apply_model("hermes", "new/model-z")
        text = self._read(".hermes/config.yaml")
        self.assertIn("default: new/model-z", text)
        self.assertIn("provider: ccr-free", text)
        self.assertIn("- hermes-cli", text, "toolsets 块在 model 块外，不许被写坏")

    def test_hub_side_model_stored_and_injected(self):
        self.assertEqual(modelcfg.hub_model("claude"), "")
        modelcfg.apply_model("claude", "new/model-z")
        self.assertEqual(modelcfg.hub_model("claude"), "new/model-z")
        self.assertEqual(modelcfg.terminal_argv("claude", "new/model-z"), ["--model", "new/model-z"])

    def test_apply_refuses_when_file_missing(self):
        (self.home / ".hermes" / "config.yaml").unlink()
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("hermes", "new/model-z")
        self.assertEqual(cm.exception.status, 404, "文件没了必须拒绝新建，不能凭空造配置")

    @unittest.skipIf(os.geteuid() == 0, "root 下 os.access(W_OK) 恒真，验不出只读位")
    def test_apply_refuses_unwritable_file_before_making_backups(self):
        """v0.13.44：09-27 报障「保存不生效」的一面 —— pi 的 settings.json 被设了
        不可变属性，旧代码先备份再写，于是**备份留了一堆、文件没改、只回一句 500**，
        用户看到的只有一句 4 秒就消失的 toast。预检必须排在备份之前：既说清原因
        （409，含解除办法），也不许在写不动的文件旁留垃圾备份。"""
        p = self.home / ".jcode" / "config.toml"
        os.chmod(p, 0o444)
        try:
            self.assertFalse(os.access(p, os.W_OK), "夹具没造出只读态，用例本身失效")
            with self.assertRaises(modelcfg.ModelCfgError) as cm:
                modelcfg.apply_model("jcode", "new/model-z")
            self.assertEqual(cm.exception.status, 409)
            self.assertIn("不可写", cm.exception.args[0])
            self.assertIn("lsattr", cm.exception.args[0], "错误信息要给得出解除办法")
            self.assertEqual(self._backups(".jcode/config.toml"), [],
                             "写不动就别落备份，否则用户目录下堆一堆没用的副本")
        finally:
            os.chmod(p, 0o644)

    def test_preview_reports_writability(self):
        """预览就要把「这个文件写不动」摆出来，别等保存时才说。"""
        p = modelcfg.preview("jcode", "new/model-z")
        for f in p["files"]:
            self.assertIn("writable", f)
        if os.geteuid() != 0:
            path = self.home / ".jcode" / "config.toml"
            os.chmod(path, 0o444)
            try:
                p = modelcfg.preview("jcode", "new/model-z")
                self.assertFalse(p["files"][0]["writable"])
            finally:
                os.chmod(path, 0o644)


class DTerminalArgvWhitelist(unittest.TestCase):
    def test_only_whitelisted_agents_get_flags(self):
        self.assertEqual(modelcfg.terminal_argv("hermes", "m"), ["-m", "m"])
        self.assertEqual(modelcfg.terminal_argv("codebuddy", "m"), [],
                         "codebuddy 的 --model 只认自有清单，不许注入 CCR 模型")
        self.assertEqual(modelcfg.terminal_argv("claude", ""), [], "没设模型就不插 flag")


class FChatChannelDefault(_HomeFixture):
    """v0.13.50：对话 / 协同子任务 / 定时任务的模型必须认「设置 → 模型」的持久化值。

    事故形态（09-28 实弹取证）：POST /api/agents/claude/chat 打给 CCR 的仍是
    qwen3.8-flash（adapter 启动时算出来的常量），而 agent_models 里存的是
    agnes/agnes-2.5-flash ⇒ 「设置页改了、新建任务照旧」。终端那条通道早就读
    DB，唯独 chat 这条没人接 —— 所以这里锁的是**调用侧**的取值顺序。
    """

    def test_explicit_request_wins(self):
        modelcfg.set_hub_model("claude", "persist/model-p")
        self.assertEqual(modelcfg.chat_model("claude", "req/model-r"), "req/model-r")

    def test_falls_back_to_persisted_model(self):
        modelcfg.set_hub_model("claude", "persist/model-p")
        self.assertEqual(modelcfg.chat_model("claude", ""), "persist/model-p")
        self.assertEqual(modelcfg.chat_model("claude", None), "persist/model-p")
        self.assertEqual(modelcfg.chat_model("claude", "   "), "persist/model-p")

    def test_empty_when_nothing_persisted(self):
        """没有持久化值时必须返回空串，让 adapter 自兜 —— 不许这里替它猜。"""
        self.assertEqual(modelcfg.chat_model("claude", ""), "")


class GDriftReport(_HomeFixture):
    """配置文件漂移体检：只读，绝不落笔。

    CCR 每次启动都会重写 ~/.claude/settings.json 的 env.ANTHROPIC_MODEL 三兄弟
    （09-28 07:06:28 实测把 agnes 改回 alibaba/qwen3.8-flash[1m]，顶层 model 不动）
    ⇒ 不带 --model 的 claude 启动会退回旧值。体检要能在变更它们的那一刻被抓到。
    """

    def test_no_drift_when_file_matches_persisted(self):
        modelcfg.apply_model("claude", "new/model-z")
        self.assertEqual(modelcfg.drift_report(), [])

    def test_env_only_rewrite_is_caught(self):
        """只改 env 三兄弟、顶层 model 不动 —— 正是 CCR 重启干的那一手。"""
        modelcfg.apply_model("claude", "new/model-z")
        before = self._read(".claude/settings.json")
        d = json.loads(before)
        d["env"]["ANTHROPIC_MODEL"] = "alibaba/qwen3.8-flash[1m]"
        d["env"]["CCR_CLAUDE_CODE_MODEL"] = "alibaba/qwen3.8-flash[1m]"
        d["env"]["CODEXL_CLAUDE_CODE_MODEL"] = "alibaba/qwen3.8-flash[1m]"
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        rep = modelcfg.drift_report()
        self.assertEqual([x["id"] for x in rep], ["claude"])
        self.assertEqual(rep[0]["hub_model"], "new/model-z")
        self.assertIn("qwen3.8-flash[1m]", rep[0]["extra"]["env.CCR_CLAUDE_CODE_MODEL"])
        # 摆出来的必须是**不一致的那几个落点**：顶层 model 仍对 ⇒ 不该出现在 owned 里；
        # 被 CCR 改掉的三兄弟必须出现（"还剩一个对得上"不能算没事）。
        self.assertNotIn("current", rep[0]["owned"])
        for k in ("env.ANTHROPIC_MODEL", "env.CCR_CLAUDE_CODE_MODEL"):
            self.assertEqual(rep[0]["owned"][k], "alibaba/qwen3.8-flash[1m]", k)

    def test_report_is_read_only(self):
        modelcfg.apply_model("claude", "new/model-z")
        modelcfg.apply_model("hermes", "old/model-a")
        (self.home / ".hermes" / "config.yaml").write_text(
            "model:\n  default: somebody/else\n  provider: ccr-free\n", encoding="utf-8")
        before = self._read(".hermes/config.yaml")
        baks_before = self._backups(".hermes/config.yaml")   # apply_model 自身的备份不算
        modelcfg.drift_report()
        self.assertEqual(self._read(".hermes/config.yaml"), before, "体检不许落笔")
        self.assertEqual(self._backups(".hermes/config.yaml"), baks_before,
                         "体检不许再新增备份（建言误做成重写）")


class HRepairDrift(_HomeFixture):
    """v0.13.51：漂移写回（用户 09-28 授权）。

    体检只报不修的话，"设置里是 Agens、重启后又变 qwen" 治不干净 —— 用户在别处
    直接敲 claude、以及任何不带 --model 的启动都读配置文件。写回属共享配置写入，
    所以这里钉的是三条护栏：备份前置、写不动就跳过（不许硬写）、dry_run 零字节。
    """

    def _env_only_rewrite(self) -> None:
        """复刻 CCR 重启那一手：只改 env 三兄弟，顶层 model 不动。"""
        modelcfg.apply_model("claude", "new/model-z")
        d = json.loads(self._read(".claude/settings.json"))
        for k in ("ANTHROPIC_MODEL", "CCR_CLAUDE_CODE_MODEL", "CODEXL_CLAUDE_CODE_MODEL"):
            d["env"][k] = "alibaba/qwen3.8-flash[1m]"
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_repair_restores_persisted_value_with_backup(self):
        self._env_only_rewrite()
        baks_before = len(self._backups(".claude/settings.json"))
        out = modelcfg.repair_drift()
        self.assertEqual([x["id"] for x in out["repaired"]], ["claude"])
        self.assertEqual(out["skipped"], [])
        d = json.loads(self._read(".claude/settings.json"))
        for k in ("ANTHROPIC_MODEL", "CCR_CLAUDE_CODE_MODEL", "CODEXL_CLAUDE_CODE_MODEL"):
            self.assertEqual(d["env"][k], "new/model-z", f"{k} 没被写回")
        self.assertEqual(len(self._backups(".claude/settings.json")), baks_before + 1,
                         "写回必须留一份新备份（共享配置铁律）")

    def test_no_drift_means_no_write(self):
        modelcfg.apply_model("claude", "new/model-z")
        before = self._read(".claude/settings.json")
        out = modelcfg.repair_drift()
        self.assertEqual(out["repaired"], [], "没漂移就不许动文件")
        self.assertEqual(self._read(".claude/settings.json"), before)

    def test_dry_run_writes_nothing(self):
        self._env_only_rewrite()
        before = self._read(".claude/settings.json")
        out = modelcfg.repair_drift(dry_run=True)
        self.assertEqual([x["id"] for x in out["repaired"]], ["claude"])
        self.assertTrue(out["repaired"][0].get("dry_run"))
        self.assertEqual(self._read(".claude/settings.json"), before, "dry-run 落笔了")

    def test_invalid_persisted_model_is_skipped_not_written(self):
        """持久化值形状不合法（CCR 爱给 `[1m]` 后缀）时跳过并记原因，绝不硬写。"""
        modelcfg.set_hub_model("claude", "bad model[1m]")
        before = self._read(".claude/settings.json")
        out = modelcfg.repair_drift()
        self.assertEqual(out["repaired"], [])
        self.assertTrue(any("不合法" in x["reason"] for x in out["skipped"]), out)
        self.assertEqual(self._read(".claude/settings.json"), before)

    @unittest.skipIf(os.geteuid() == 0, "root 下 os.access(W_OK) 恒真，验不出只读位")
    def test_unwritable_file_is_skipped(self):
        self._env_only_rewrite()
        p = self.home / ".claude" / "settings.json"
        os.chmod(p, 0o444)
        try:
            out = modelcfg.repair_drift()
            self.assertEqual(out["repaired"], [])
            self.assertTrue(any("不可写" in x["reason"] for x in out["skipped"]), out)
        finally:
            os.chmod(p, 0o644)


class EPasscodeGate(unittest.TestCase):
    """HTTP 层：口令门必须 fail-closed（复用 writeauth 同款口径）。"""

    def setUp(self):
        self.tmp = _mktmp("modelcfg-gate-")
        self.old_pc = os.environ.get("HUB_PASSCODE")
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp)
        db.init_db(self.tmp / "gate.db")

    def tearDown(self):
        if self.old_pc is None:
            os.environ.pop("HUB_PASSCODE", None)
        else:
            os.environ["HUB_PASSCODE"] = self.old_pc
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _err(self, coro):
        """跑协程并取出它抛的 HTTPException（FastAPI 在测试里没有 TestClient 时用）。"""
        import asyncio
        from fastapi import HTTPException
        try:
            asyncio.run(coro)     # 新建 loop：unittest 主线程里 get_event_loop() 已不再自动创建
        except HTTPException as e:
            return e
        return None

    def test_missing_passcode_config_is_503(self):
        os.environ.pop("HUB_PASSCODE", None)
        e = self._err(modelcfg.settings_model_apply(
            modelcfg.ApplyIn(agent_id="claude", model="m", passcode="x"), None))
        self.assertIsNotNone(e, "没配口令必须拒绝，不能静默放行")
        self.assertEqual(e.status_code, 503)

    def test_wrong_passcode_is_401_and_never_echoed(self):
        os.environ["HUB_PASSCODE"] = "secret-pc"
        e = self._err(modelcfg.settings_model_apply(
            modelcfg.ApplyIn(agent_id="claude", model="m", passcode="guess-me"), None))
        self.assertIsNotNone(e)
        self.assertEqual(e.status_code, 401)
        self.assertNotIn("secret-pc", str(e.detail), "拒绝原因里回显了口令")



class IThreeNewAgents(_HomeFixture):
    """v0.13.85（2026-10-07）：用户报障「模型设置里没有 agents 的所有模型」。

    原先 SPECS 只有 6 家（claude/jcode/codex/pi/grok/hermes），而本机实有的 CLI agent 是
    10 家 ⇒ opencode / qoder / cursor 三家在设置页里**压根不出现**。
    这三家的落点形状互不相同，故逐家钉「只动目标键」与「拒绝不该写的东西」。
    """

    # ── opencode：双登记（opencode.json 一份 + CLI 侧一份），缺一份就哑火 ──
    def test_opencode_writes_model_and_registers_in_provider(self):
        modelcfg.apply_model("opencode", "ccr/new/model-z")
        d = json.loads(self._read(".config/opencode/opencode.json"))
        self.assertEqual(d["model"], "ccr/new/model-z")
        self.assertIn("new/model-z", d["provider"]["ccr"]["models"],
                      "opencode 只认 provider.models 里登记过的 ID，缺登记会 UnknownError")
        self.assertIn("old/model-b", d["provider"]["ccr"]["models"], "原有登记不许被删")
        # 无关面必须原样（整文件重写最容易冲掉这些）
        self.assertEqual(d["permission"], {"bash": "ask"})
        self.assertTrue(d["mcp"]["some-server"]["enabled"])
        self.assertEqual(d["provider"]["ccr"]["options"]["baseURL"], "http://127.0.0.1:3456/v1")

    def test_opencode_small_model_follows_only_when_it_matched(self):
        """small_model 与 model 原本同值 ⇒ 跟随（否则"主模型换了、轻量任务还走旧模型"）。"""
        modelcfg.apply_model("opencode", "ccr/new/model-z")
        d = json.loads(self._read(".config/opencode/opencode.json"))
        self.assertEqual(d["small_model"], "ccr/new/model-z")

    def test_opencode_small_model_not_clobbered_when_user_set_otherwise(self):
        """用户**故意**把 small_model 设成别的模型 ⇒ 那是他的配置意图，hub 不许改。"""
        path = self.home / ".config/opencode/opencode.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        d["small_model"] = "ccr/poolside/laguna-xs-2.1:free"
        path.write_text(json.dumps(d), encoding="utf-8")
        modelcfg.apply_model("opencode", "ccr/new/model-z")
        d2 = json.loads(self._read(".config/opencode/opencode.json"))
        self.assertEqual(d2["small_model"], "ccr/poolside/laguna-xs-2.1:free",
                         "不许覆盖用户自己设的 small_model")
        self.assertEqual(d2["model"], "ccr/new/model-z")

    def test_opencode_rejects_model_without_provider_prefix(self):
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("opencode", "no-prefix-model")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("provider 前缀", cm.exception.args[0])

    def test_opencode_refuses_to_invent_provider(self):
        """provider 段不存在时拒绝凭空新建（那样会缺 baseURL/apiKey，比报错更坏）。"""
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("opencode", "nosuchprov/new/model-z")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("拒绝凭空新建 provider", cm.exception.args[0])

    # ── qoder：官方明令 BYOK 只走 /model 向导、禁止手写 settings.json ──
    def test_qoder_writes_only_native_name(self):
        modelcfg.apply_model("qoder", "Qwen3.8-Max")
        d = json.loads(self._read(".qoder/settings.json"))
        self.assertEqual(d["model"]["name"], "Qwen3.8-Max")
        for k in ("securityScan", "permissions", "security"):
            self.assertIn(k, d, f"qoder 的 {k} 段不许被写坏")

    def test_qoder_rejects_ccr_model_id(self):
        """CCR 的 provider/model ID 塞进 qoder 的 model.name 会被 CLI 拒 ⇒ 写前就必须拒。"""
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("qoder", "alibaba/deepseek-v4.1-flash")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("原生名", cm.exception.args[0])
        self.assertEqual(json.loads(self._read(".qoder/settings.json"))["model"]["name"],
                         "Qwen3.8-Flash", "被拒的写入不许留痕")

    # ── cursor：三键同源 + 参数表索引 ──
    def test_cursor_writes_all_three_same_source_keys(self):
        modelcfg.apply_model("cursor", "new/model-z")
        d = json.loads(self._read(".cursor/cli-config.json"))
        self.assertEqual(d["model"]["modelId"], "new/model-z")
        self.assertEqual(d["model"]["displayModelId"], "new/model-z")
        self.assertEqual(d["selectedModel"]["modelId"], "new/model-z")
        self.assertTrue(d["hasChangedDefaultModel"])
        self.assertIn("new/model-z", d["modelParameters"],
                      "参数表要按 ID 建索引，否则参数面板查不到这个模型")
        # 用户与 cursor 自己的配置面一概不许碰
        for k, want in (("permissions", {"allow": ["Shell(ls)"], "deny": []}),
                        ("display", {"mode": "zen"}), ("editor", {"vimMode": False}),
                        ("sandbox", {"mode": "disabled"}),
                        ("network", {"useHttp1ForAgent": False})):
            self.assertEqual(d[k], want, f"cursor 的 {k} 被动了")

    def test_new_agents_get_argv_injection(self):
        """三家都认 --model（逐家 --help 实测），故 hub 拉起的终端必须带注入。"""
        for aid in ("opencode", "qoder", "cursor"):
            self.assertEqual(modelcfg.terminal_argv(aid, "m/x"), ["--model", "m/x"], aid)

    def test_write_mode_is_exposed_for_every_agent(self):
        """字段认什么必须可机读（前端文案由它派生），否则用户会把 CCR ID 塞进原生字段。"""
        modes = {a["id"]: a["write_mode"] for a in modelcfg.list_agents()}
        self.assertEqual(modes["qoder"], "native")
        for aid in ("claude", "jcode", "codex", "pi", "grok", "hermes", "opencode", "cursor"):
            self.assertEqual(modes[aid], "ccr", aid)


class MValuePrefix(_HomeFixture):
    """v0.13.86（2026-10-07 续报）：**下拉能点到的值必须就是能写进去的值**。

    根因不是"没校验"，而是设置页把不可写的值摆成了可选项 —— CCR 清单给裸 ID
    （`alibaba/qwen3.8-max`），opencode 的字段要 `ccr/<裸 ID>`
    （官方 Models 页：格式即 provider/model；本机 `opencode models ccr` 逐行皆 `ccr/…`）
    ⇒ 用户在设置页点任何一个都必然 409。所以前缀必须由**后端**作为单一真相源透出，
    前端只做拼接，不许自己猜。
    """

    def test_value_prefix_exposed_for_opencode_only(self):
        st = {a["id"]: a for a in modelcfg.list_agents()}
        self.assertEqual(st["opencode"]["value_prefix"], "ccr/")
        for aid in ("claude", "qoder", "cursor", "codex"):
            self.assertEqual(st[aid]["value_prefix"], "", aid)

    def test_prefix_is_what_the_writer_actually_needs(self):
        """判据：后端透出的前缀 + 裸 CCR ID == 写手接受的完整值（两端同源，不靠约定）。"""
        st = {a["id"]: a for a in modelcfg.list_agents()}
        bare = "alibaba/qwen3.8-max"
        prefixed = st["opencode"]["value_prefix"] + bare
        self.assertEqual(prefixed, "ccr/alibaba/qwen3.8-max")
        modelcfg.apply_model("opencode", prefixed)          # 拼出来的值必须真能落笔
        d = json.loads(self._read(".config/opencode/opencode.json"))
        self.assertEqual(d["model"], prefixed)
        self.assertIn(bare, d["provider"]["ccr"]["models"],
                      "provider.models 的键是无前缀裸 ID（本机实测形状）")
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("opencode", bare)          # 裸 ID 仍必须被拒（不许悄悄放行）
        self.assertEqual(cm.exception.status, 409)

    def test_preview_carries_prefix_and_native_models(self):
        """预览与状态必须带同样的两列：前端选 agent 时与保存后刷新时走的是同一份形状。"""
        pv = modelcfg.preview("opencode", "ccr/a/b")
        self.assertEqual(pv["value_prefix"], "ccr/")
        pv2 = modelcfg.preview("qoder", "Qwen3.8-Max")
        self.assertEqual(pv2["models"], ["Qwen3.8-Max", "Qwen3.8-Flash"])
        self.assertEqual(pv2["value_prefix"], "")


class MNativeInventory(_HomeFixture):
    """v0.13.86：native 型 agent（qoder）的字段只认本家名 ⇒ 界面不许摆 CCR 清单。

    改前 qoder 那一栏摆着 13 个 CCR ID，点任何一个都必然 400 —— "看起来能选、
    实际必被拒"是操作逻辑缺陷，不是缺校验。清单取证：`qodercli --list-models`
    实测输出（2026-10-07）= Qwen3.8-Max / Qwen3.8-Flash。
    """

    def test_native_models_exposed_for_qoder_only(self):
        st = {a["id"]: a for a in modelcfg.list_agents()}
        self.assertEqual(st["qoder"]["models"], ["Qwen3.8-Max", "Qwen3.8-Flash"])
        for aid in ("opencode", "cursor", "claude"):
            self.assertEqual(st[aid]["models"], [], aid)

    def test_every_exposed_native_name_is_actually_writable(self):
        """清单里每一个名字都必须真能落笔 —— 否则界面又摆了一排"看着能点"的假选项。"""
        for aid, names in modelcfg.NATIVE_MODELS.items():
            for nm in names:
                modelcfg.apply_model(aid, nm)
                d = json.loads(self._read(".qoder/settings.json"))
                self.assertEqual(d["model"]["name"], nm, aid)

    def test_opencode_read_reports_real_provider_not_hardcoded(self):
        """`provider` 是给用户看的参考信息，写死 "ccr" 会把别家 provider 说成 ccr。"""
        path = self.home / ".config/opencode/opencode.json"
        d = json.loads(path.read_text(encoding="utf-8"))
        d["provider"]["other"] = {"options": {"baseURL": "http://x/v1"}, "models": {}}
        d["model"] = "other/some-model"
        path.write_text(json.dumps(d), encoding="utf-8")
        self.assertEqual(modelcfg.agent_state("opencode")["provider"], "other")


class JGrokReadFix(_HomeFixture):
    """2026-10-07 修的真缺陷：grok 的「当前」在设置页恒显示"（未读到）"。

    根因＝`_toml_get` 把 section 按 "." 切分，而 grok 的表头 `[model.ccr-hub]`
    在 tomllib 里是**一个含点号的键** ⇒ data["model"]["ccr-hub"] 恒 None。
    表现＝「库里明明设了模型，设置页却说没读到」。
    """

    def test_dotted_section_header_is_readable(self):
        text = ('[models]\ndefault = "ccr-hub"\n\n'
                '[model.ccr-hub]\nmodel = "alibaba/deepseek-v4.1-flash"\n')
        self.assertEqual(modelcfg._toml_get(text, "model.ccr-hub", "model"),
                         "alibaba/deepseek-v4.1-flash", "含点号的表头必须整键查得到")
        self.assertEqual(modelcfg._toml_get(text, "models", "default"), "ccr-hub")

    def test_nested_section_still_works(self):
        """整键优先不能把「逐级嵌套 section」的既有能力改坏。"""
        text = '[providers.ccr]\ndefault_model = "x"\n'
        self.assertEqual(modelcfg._toml_get(text, "providers.ccr", "default_model"), "x")

    def test_grok_current_is_model_id_and_provider_is_header_key(self):
        """口径对齐 grok 自己的 `grok models`：它报 `Default model: ccr-hub`，
        而真正发出去的模型是 `[model.ccr-hub].model`。设置页两者都要给对。"""
        st = modelcfg.agent_state("grok")
        self.assertEqual(st["current"], "old/model-a", "current 必须是真正发出去的模型 ID")
        self.assertEqual(st["provider"], "ccr-old", "provider 必须是表头键（profile 名）")

    def test_grok_argv_injects_profile_key_not_raw_id(self):
        """判据（本机实弹）：`grok -m alibaba/qwen3.8-max` ⇒ 硬报 unknown model id；
        `grok -m ccr-hub` ⇒ 正常且账本 served 精确命中。故注入表头键。"""
        self.assertEqual(modelcfg.terminal_argv("grok", "alibaba/deepseek-v4.1-flash"),
                         ["--model", "ccr-hub"])
        self.assertEqual(modelcfg.terminal_argv("grok", "anything/else"), ["--model", "ccr-hub"])

    def test_grok_default_pointing_at_builtin_id(self):
        """官方 README 允许 `[models] default = "grok-4.6"`（内置 ID，无 [model.*] 块）
        ⇒ 此时 current 就该是它本身，不许读成空。"""
        self._write(".grok/config.toml", '[models]\ndefault = "grok-4.6"\n')
        st = modelcfg.agent_state("grok")
        self.assertEqual(st["current"], "grok-4.6")
        self.assertEqual(st["provider"], "grok-4.6")


class KClearHubModel(_HomeFixture):
    """「撤销 hub 侧默认」这条路（设置页下拉的第一项）。

    2026-10-07 前的缺陷：前端 `if (!model) return`、后端 `validate_model("")` 抛 400
    ⇒ 这一项**永远存不了**，用户点了保存什么都不发生（连请求都不发）。
    """

    def test_clear_removes_persisted_value(self):
        modelcfg.apply_model("cursor", "new/model-z")
        self.assertEqual(modelcfg.hub_model("cursor"), "new/model-z")
        out = modelcfg.apply_model("cursor", "")
        self.assertEqual(out["mode"], "clear")
        self.assertEqual(modelcfg.hub_model("cursor"), "")

    def test_clear_does_not_touch_any_config_file(self):
        """清空**只**撤销注入，不改任何配置文件（不猜"原本是什么"——那个值只在备份里）。"""
        modelcfg.apply_model("cursor", "new/model-z")
        before = self._read(".cursor/cli-config.json")
        n_bak = len(self._backups(".cursor/cli-config.json"))
        modelcfg.apply_model("cursor", "")
        self.assertEqual(self._read(".cursor/cli-config.json"), before, "清空不许改配置")
        self.assertEqual(len(self._backups(".cursor/cli-config.json")), n_bak,
                         "清空不该产生备份")

    def test_clear_makes_argv_empty(self):
        modelcfg.apply_model("claude", "new/model-z")
        self.assertEqual(modelcfg.terminal_argv("claude", modelcfg.hub_model("claude")),
                         ["--model", "new/model-z"])
        modelcfg.apply_model("claude", "")
        self.assertEqual(modelcfg.terminal_argv("claude", modelcfg.hub_model("claude")), [],
                         "撤销后 hub 拉起的终端不应再带 --model")

    def test_preview_clear_is_read_only_and_says_so(self):
        modelcfg.apply_model("cursor", "new/model-z")
        before = self._read(".cursor/cli-config.json")
        p = modelcfg.preview("cursor", "")
        self.assertEqual(p["mode"], "clear")
        self.assertEqual(p["files"], [])
        self.assertIn("配置文件", p["note"])
        self.assertEqual(self._read(".cursor/cli-config.json"), before)

    def test_clear_on_unknown_agent_is_rejected(self):
        with self.assertRaises(modelcfg.ModelCfgError):
            modelcfg.apply_model("codebuddy", "")


class LQoderDriftExempt(_HomeFixture):
    """qoder 的漂移比对必须豁免 —— 否则恒报假漂移，且 repair_drift 会去写它不认的值。"""

    def test_qoder_is_not_reported_as_drifted(self):
        modelcfg.set_hub_model("qoder", "alibaba/deepseek-v4.1-flash")
        ids = [d["id"] for d in modelcfg.drift_report()]
        self.assertNotIn("qoder", ids, "原生名 vs CCR ID 形状不可比，比了是恒假警报")

    def test_qoder_drift_repair_skips_it(self):
        modelcfg.set_hub_model("qoder", "alibaba/deepseek-v4.1-flash")
        out = modelcfg.repair_drift()
        self.assertNotIn("qoder", [r["id"] for r in out["repaired"]],
                         "repair 不许把 CCR ID 写进 qoder 的原生字段")


class MAutoDiscovery(_HomeFixture):
    """agent 清单自动发现（2026-10-25 用户令「应该是自动发现并添加」）。

    背景：本页原先只列 SPECS+UNSUPPORTED 两张硬编码表 ⇒ 画像里新登记的 agent
    （dsh 10-23、omp 10-24）在设置页零存在感。判据三条：
    ① 注册表在册的 agent 必须出现在 list_agents（可设或带理由置灰）；
    ② omp 是 argv-only（实测 --help 有 --model、无明文配置文件）⇒ hub 侧可设；
    ③ dsh 实测 TUI 无 --model ⇒ 置灰且理由里带证据关键词。
    """

    def test_registry_agents_appear_in_list(self):
        ids = [a["id"] for a in modelcfg.list_agents()]
        for aid in ("dsh", "omp", "claude", "pi"):
            self.assertIn(aid, ids, f"{aid} 在 hub 注册表在册，设置页必须列出来")

    def test_list_has_no_duplicate_ids(self):
        ids = [a["id"] for a in modelcfg.list_agents()]
        self.assertEqual(len(ids), len(set(ids)), "静态表与注册表合并后不许有重复 id")

    def test_omp_is_argv_only_and_writable(self):
        st = modelcfg.agent_state("omp")
        self.assertTrue(st["supported"], "omp 已实测 --model 支持，必须可设置")
        self.assertTrue(st["writable"], "argv-only 的 writable 语义 = hub 侧可设置")
        self.assertEqual(st["write_mode"], "argv")
        self.assertEqual(st["files"], [], "omp 无明文配置文件（sqlite + models.yml）")
        self.assertIn("--model", st["note"])
        self.assertEqual(st["value_prefix"], "ccr/",
                         "omp 的 --model 收 <provider-key>/<id>（实测裸 ID 报 No API key）")

    def test_omp_apply_only_touches_hub_side(self):
        # 值必须带 ccr/ 前缀 —— 2026-10-25 实测 omp --model 的形状（见 VALUE_PREFIX）。
        out = modelcfg.apply_model("omp", "ccr/openrouter/openrouter/free")
        self.assertEqual(out["applied"], [], "argv-only 不许写任何文件")
        self.assertEqual(modelcfg.hub_model("omp"), "ccr/openrouter/openrouter/free")
        self.assertEqual(modelcfg.terminal_argv("omp", modelcfg.hub_model("omp")),
                         ["--model", "ccr/openrouter/openrouter/free"],
                         "hub 拉起 omp 终端必须追加 --model（term.py:912 同款通路）")
        # 假 HOME 里不该被创建出任何 omp 配置文件
        self.assertEqual(list((self.home / ".omp").glob("**/*.json")) if
                         (self.home / ".omp").exists() else [], [],
                         "apply 不许顺手创建配置文件")

    def test_omp_preview_reports_injection_not_files(self):
        p = modelcfg.preview("omp", "ccr/openrouter/nvidia/nemotron-3-ultra-550b-a55b:free")
        self.assertEqual(p["files"], [])
        self.assertEqual(p["argv"],
                         ["--model", "ccr/openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"])
        self.assertEqual(p["write_mode"], "argv")
        self.assertEqual(p["value_prefix"], "ccr/")

    def test_dsh_is_unsupported_with_evidence_reason(self):
        st = modelcfg.agent_state("dsh")
        self.assertFalse(st["supported"], "dsh TUI 实测无 --model，必须置灰")
        self.assertFalse(st["writable"])
        self.assertEqual(st["write_mode"], "none")
        self.assertIn("--profile tui --help", st["reason"],
                      "置灰理由必须带实测证据（dsh --profile tui --help 的选项清单）")

    def test_omp_not_reported_as_drift(self):
        """argv-only 没有配置文件可比 ⇒ 不许进漂移体检（否则恒假警报）。"""
        modelcfg.set_hub_model("omp", "openrouter/openrouter/free")
        ids = [d["id"] for d in modelcfg.drift_report()]
        self.assertNotIn("omp", ids, "无文件可比的 agent 进漂移 = 恒假警报")

    def test_registry_merge_adds_unlisted_agent(self):
        """注册表在册但两张表都没登记 ⇒ 必须合并进清单并如实置灰。

        用 mock 钉合并行为本身（不依赖本机安装态）：去掉 list_agents 里的注册表
        合并段，本用例立刻红（zzz-fake 消失）。"""
        from unittest import mock
        with mock.patch.object(modelcfg, "_registry_agent_ids",
                               return_value=["zzz-fake"]):
            ids = [a["id"] for a in modelcfg.list_agents()]
            self.assertIn("zzz-fake", ids, "注册表在册的 agent 必须合并进设置页清单")
            st = modelcfg.agent_state("zzz-fake")
            self.assertFalse(st["supported"])
            self.assertIn("未接入", st["reason"],
                          "未登记的新 agent 要给「未接入」的如实理由，不是白名单外一句话")


if __name__ == "__main__":
    unittest.main()


class _CatalogFixture(_HomeFixture):
    """把「网关清单」换成桩数据。

    L0 不许真打 :3456（网关一挂全红，且假模型 ID 必然"不在册"）。所以清单校验的
    正确性靠打桩 `_fetch_catalog_ids` 来测：既验证"不在册就拒写"，也验证
    "取不到清单时不许误拒"—— 后者是 fail-open 与 fail-closed 的分界线。
    """

    IDS = {"alibaba/qwen3.8-max", "deepseek/deepseek-flash"}

    def setUp(self):
        super().setUp()
        self._old_env = os.environ.pop("HUB_MODEL_CATALOG", None)
        self._old_fetch = modelcfg._fetch_catalog_ids
        modelcfg._CATALOG.update({"ts": 0.0, "ids": set(), "checked": False, "size": 0,
                                  "error": ""})

    def tearDown(self):
        modelcfg._fetch_catalog_ids = self._old_fetch
        modelcfg._CATALOG.update({"ts": 0.0, "ids": set(), "checked": False, "size": 0,
                                  "error": ""})
        if self._old_env is not None:
            os.environ["HUB_MODEL_CATALOG"] = self._old_env
        super().tearDown()

    def _serve(self, ids=None, err=""):
        got = set(self.IDS if ids is None else ids)
        modelcfg._fetch_catalog_ids = lambda: (got, err)


class MCatalogGuard(_CatalogFixture):
    """v0.13.164：写入前先问「这个 ID 现在还活着吗」。

    病根：设置页只证明"写进去了"。存着一个已经下线的模型 ID ⇒ 终端照旧注入
    ⇒ agent 报 unknown model，而页面显示"已生效"。这条闸门挡的就是它。
    """

    def test_model_in_catalog_is_written(self):
        self._serve()
        modelcfg.apply_model("jcode", "deepseek/deepseek-flash")
        self.assertEqual(modelcfg.hub_model("jcode"), "deepseek/deepseek-flash")

    def test_model_not_in_catalog_is_refused_without_writing(self):
        self._serve()
        before = self._read(".jcode/config.toml")
        with self.assertRaises(modelcfg.ModelCfgError) as cm:
            modelcfg.apply_model("jcode", "dead/gone-model")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("不在 CCR 当前清单", cm.exception.args[0])
        self.assertEqual(self._read(".jcode/config.toml"), before, "拒绝时不许落笔")
        self.assertEqual(self._backups(".jcode/config.toml"), [], "拒绝时不许留备份")
        self.assertEqual(modelcfg.hub_model("jcode"), "", "拒绝时不许写 hub 侧")

    def test_catalog_unavailable_must_not_block(self):
        """取不到清单 ≠ 清单里没有。网关抖动时把用户锁死是更坏的失败。"""
        self._serve(ids=set(), err="URLError: refused")
        modelcfg.apply_model("jcode", "whatever/model-x")
        self.assertEqual(modelcfg.hub_model("jcode"), "whatever/model-x")

    def test_prefix_is_stripped_before_catalog_lookup(self):
        """opencode/omp 的值带 provider 前缀；比对必须用裸 ID，否则在册也判成不在。"""
        self._serve()
        modelcfg.apply_model("opencode", "ccr/deepseek/deepseek-flash")
        self.assertEqual(modelcfg.hub_model("opencode"), "ccr/deepseek/deepseek-flash")

    def test_native_mode_is_not_catalog_checked(self):
        """qoder 的原生名（Qwen3.8-Max）天然不在 CCR 清单里，拿清单比必然误拒。"""
        self._serve()
        modelcfg.apply_model("qoder", "Qwen3.8-Max")
        self.assertEqual(modelcfg.hub_model("qoder"), "Qwen3.8-Max")


class NEffectiveState(_CatalogFixture):
    """三通道 × 清单的单一判定（v0.13.164）。"""

    def test_effective_reports_all_four_checks(self):
        self._serve()
        modelcfg.apply_model("claude", "alibaba/qwen3.8-max")
        eff = modelcfg.effective_state("claude")
        self.assertEqual(eff["model"], "alibaba/qwen3.8-max")
        self.assertTrue(eff["catalog"]["ok"])
        self.assertTrue(eff["channels"]["term_new"]["applies"])
        self.assertEqual(eff["channels"]["term_new"]["argv"],
                         ["--model", "alibaba/qwen3.8-max"])
        self.assertTrue(eff["channels"]["chat"]["applies"], "claude 有 chat 适配器")
        self.assertIsNone(eff["drift"])

    def test_verify_passes_after_apply(self):
        self._serve()
        modelcfg.apply_model("claude", "alibaba/qwen3.8-max")
        v = modelcfg.verify_model("claude")
        self.assertEqual(v["verdict"], "ok")
        self.assertTrue(all(c["ok"] is not False for c in v["checks"]))

    def test_verify_catches_config_drift(self):
        """配置文件被别的进程改回旧值 ⇒ 静态验证必须当场判否（不能只看 hub 侧）。"""
        self._serve()
        modelcfg.apply_model("claude", "alibaba/qwen3.8-max")
        d = json.loads(self._read(".claude/settings.json"))
        d["model"] = "old/model-a"
        self._write(".claude/settings.json", json.dumps(d))
        v = modelcfg.verify_model("claude")
        self.assertEqual(v["verdict"], "fail")
        bad = [c["name"] for c in v["checks"] if c["ok"] is False]
        self.assertIn("配置文件落点", bad)
        self.assertTrue(any("漂移" in w for w in v["warnings"]))

    def test_verify_catches_model_removed_from_catalog(self):
        self._serve()
        modelcfg.apply_model("claude", "alibaba/qwen3.8-max")
        self._serve(ids={"other/model"})          # 网关侧删了这个模型
        modelcfg._CATALOG.update({"ts": 0.0, "checked": False})
        v = modelcfg.verify_model("claude")
        self.assertEqual(v["verdict"], "fail")
        bad = [c["name"] for c in v["checks"] if c["ok"] is False]
        self.assertIn("CCR 清单", bad)

    def test_omp_argv_only_skips_file_check(self):
        self._serve()
        modelcfg.apply_model("omp", "ccr/deepseek/deepseek-flash")
        v = modelcfg.verify_model("omp")
        self.assertEqual(v["verdict"], "ok")
        file_check = [c for c in v["checks"] if c["name"] == "配置文件落点"][0]
        self.assertIsNone(file_check["ok"], "argv-only 无文件可落 ⇒ 该判据必须是 na 不是 fail")
        self.assertTrue(any("argv-only" in w for w in v["warnings"]))

    def test_unset_is_not_reported_as_failure(self):
        """没设过模型 = 未配置，不是"验证失败"；否则每个 agent 都挂红叉，红叉就没信号了。"""
        v = modelcfg.verify_model("claude")
        self.assertEqual(v["verdict"], "unset")
        self.assertFalse(any(c["ok"] is False for c in v["checks"]))

    def test_agent_state_carries_effective_and_real_argv(self):
        self._serve()
        modelcfg.apply_model("grok", "alibaba/qwen3.8-max")
        st = modelcfg.agent_state("grok")
        self.assertEqual(st["argv_effective"], ["--model", "ccr-hub"],
                         "grok 注入的是 profile 表头键，不是模型 ID（页面必须如实显示）")
        self.assertEqual(st["effective"]["model"], "alibaba/qwen3.8-max")
