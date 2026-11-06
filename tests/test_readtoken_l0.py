# P0 读端点令牌门（v0.13.192，方案 AgentHub优化设计方案.md §6.6）L0 判据。
# 纪律同 test_writeauth.py：纯函数判 + 中间件形态验证，不拉活网络。
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import writeauth  # noqa: E402


def test_read_verdict_loopback_anonymous_allowed(monkeypatch):
    secrets = ["sekret"]
    for host in ("127.0.0.1", "::1"):
        v, r = writeauth.read_verdict(host, "/api/agents", "", secrets)
        assert v == "allow", (host, r)


def test_read_verdict_nonloopback_no_token_denied():
    v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", "", ["sekret"])
    assert v == "deny", (v, r)


def test_read_verdict_nonloopback_token_match_allowed():
    v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", "sekret", ["sekret"])
    assert v == "allow", (v, r)
    v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", "wrong", ["sekret"])
    assert v == "deny", (v, r)


def test_read_verdict_no_secrets_is_misconfig_not_open():
    v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", "", [])
    assert v == "misconfig", (v, r)


def test_read_verdict_disabled_gate_always_allows():
    old = writeauth.READ_TOKEN_DISABLED
    writeauth.READ_TOKEN_DISABLED = True
    try:
        v, _ = writeauth.read_verdict("8.8.8.8", "/api/memory/search", "", [])
        assert v == "allow"
    finally:
        writeauth.READ_TOKEN_DISABLED = old


def test_read_allow_prefixes_injected_like_csp_report():
    # main 的注入模式：加前缀 → 放行；逐条写理由，测试钉住「注入即生效」
    allow = writeauth._READ_TOKEN_ALLOW
    allow.add("/api/settings/model/catalog")
    try:
        v, _ = writeauth.read_verdict("8.8.8.8", "/api/settings/model/catalog", "", ["sekret"])
        assert v == "allow"
        v, _ = writeauth.read_verdict("8.8.8.8", "/api/memory/search", "", ["sekret"])
        assert v == "deny"
    finally:
        allow.discard("/api/settings/model/catalog")


def test_read_banner_state_shapes():
    assert writeauth.read_banner_state("127.0.0.1", "/", "", ["k"])["state"] == "ok"
    assert writeauth.read_banner_state("8.8.8.8", "/", "", ["k"])["state"] == "locked"
    assert writeauth.read_banner_state("8.8.8.8", "/", "", [])["state"] == "misconfig"


def test_read_gate_is_async_middleware():
    import asyncio
    assert asyncio.iscoroutinefunction(writeauth.read_gate)


def test_main_registers_read_gate_before_write_gate():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
    i = src.index("writeauth.read_gate")
    j = src.index("app.middleware(\"http\")(write_gate)")
    assert i < j, "read_gate 必须先于 write_gate 注册（外层先放行/拒读，再轮到写门）"


def test_main_injects_disable_switch_and_allow_prefix():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
    assert "HUB_READ_TOKENS" in src
    assert "writeauth._READ_TOKEN_ALLOW.add" in src


def test_version_bumped():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
    assert 'VERSION = "0.13.192"' in src
