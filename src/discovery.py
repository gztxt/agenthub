"""Agent 发现（画像驱动版：按本机实际运行检测，非端口判定）"""
import asyncio
import json
import re
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

import boundary
import profiles
import vitals


# 应答态（rt_state）→ 中文后缀。全部是**情报**，没有一个能把卡判成不可用。
RT_SUFFIX = {
    "answered": "，真实请求已应答",
    "blocked_by_account": "；额度/登录受限（账号条件，非程序故障）",
    "rate_limited": "；上游限流 429（外部负载条件，非程序故障）",
    "model_unsupported": "；测试模型标识不被该 CLI 接受（配置问题，非程序故障）",
    "timeout": "；真实请求超时（模型侧常见现象，不影响可用性结论）",
    "probe_rejected": "；探针被 CLI 参数/信任检查拒（探针缺陷，未用作证据）",
    "no_output": "；真实请求无输出",
    "skipped": "，本轮未做真实请求实测",
}


def _verdict_view(p: dict, vrec: Optional[dict]):
    """翻译 vitals 记录给前端。两层口径（2026-09-22 用户指令）：

      verdict  —— 生死：能不能打开窗口 + 能不能自检，**与模型应答无关**
      rt_state —— 情报：真请求那次模型层发生了什么（应答/限流/额度/模型标识/超时）
    """
    if not vrec:
        return {"usable": None, "verdict": "pending", "reason": "尚未体检（等首轮探针跑完）",
                "source": "pending", "confidence": None, "at": None, "attested": None,
                "rt_state": None, "rt_note": None, "rt_model": None}
    v = vrec.get("verdict", "unknown")
    ev = vrec.get("evidence", {})
    rts = vrec.get("rt_state") or "skipped"
    if v == "usable":
        if ev.get("agent_shape") == "terminal-cli":
            head = "自检正常（%s）" % (ev.get("resolved_path") or ev.get("resolved_name") or "-")
        else:
            code = ev.get("endpoint_http_v4") or ev.get("endpoint_http_v6")
            head = "自有端点应声 HTTP %s" % code
        reason = head + RT_SUFFIX.get(rts, "")
    else:
        reason = {
            "not_installed": ("假卡：命中的是入口壳 %s，它打印「找不到目标命令」后就退出，"
                              "该 Agent 本体未安装" % (ev.get("resolved_path") or "-")),
            "broken": "已安装但自检失败：--version 与 --help 都无响应，窗口打不开",
            "stopped": "服务未运行（端口无应答），程序本身没坏",
        }.get(v, "证据不足，未能判定")
    if vrec.get("rt_flaky"):
        reason += "（外部条件，每轮重试，不固化为故障）"
    ts = vrec.get("checked_at")
    usable = v == "usable"
    # 凭据 = 真应答跑通，或服务型 Agent 自有端口在应声
    attested = bool(usable and (rts == "answered" or
                                (ev.get("agent_shape") == "web-service" and
                                 ev.get("endpoint_serving"))))
    return {"usable": usable, "verdict": v, "reason": reason,
            "rt_state": rts, "rt_note": vrec.get("rt_note") or ev.get("run_note"),
            "rt_model": vrec.get("rt_model") or ev.get("run_model"),
            "rt_at": vrec.get("rt_at") or vrec.get("checked_at"),
            "source": vrec.get("source", "rule"), "confidence": vrec.get("confidence"),
            "at": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            "attested": attested}



@dataclass
class AgentInfo:
    id: str
    name: str
    kind: str          # agent|gateway|service|memory|tool
    status: str        # running|installed|stopped
    endpoint: str
    port: Optional[int]
    auth_required: bool
    description: str = ""
    config_path: Optional[str] = None
    last_seen: Optional[str] = None
    builtin: bool = True
    working_dir: Optional[str] = None
    entries: List[dict] = field(default_factory=list)
    # ── vitals 判定层（2026-09-21）：status 说「在不在」，verdict 说「能不能用」
    usable: Optional[bool] = None
    verdict: str = ""
    verdict_reason: str = ""
    verdict_source: str = ""      # local | local-lowconf | rule | pending（史实数据还可能有 jev/jev-lowconf）
    verdict_confidence: Optional[float] = None
    verdict_at: Optional[str] = None
    # attested：这条「可用」是有实测凭据支撑的（真应答 / 自有端点应声）。
    # 只跑了 L2 自述探针时为 False —— 前端据此显示「未见异常」而不是「可用」。
    attested: Optional[bool] = None
    # 模型层情报（2026-09-22）：与 usable/verdict 解耦 —— 额度、限流、超时、模型标识
    # 全都只改这几个字段，改不了「这个 Agent 能不能用」。
    rt_state: str = ""
    rt_note: Optional[str] = None
    rt_model: Optional[str] = None
    # ── 纳管边界四维（P0-5，2026-10-15）：强制/观测 × 覆盖/未覆盖。
    # `label` 只可能是「只读」或「未观察到写操作」；后者是默认值 —— 不写就降级。
    boundary: Optional[dict] = None

    def to_dict(self):
        return asdict(self)


class AgentDiscovery:
    def __init__(self, config, db=None):
        self.config = config
        self.db = db
        self._cache: Dict[str, AgentInfo] = {}

    async def discover_all(self) -> List[AgentInfo]:
        procs, units, dockers = await asyncio.to_thread(self._probe_env)
        # 第一遍：全部状态（供跨画像联动，如 claude 的 Web UI 宿主 cloudcli）
        prof_list = profiles.all_profiles()
        status_map: Dict[str, str] = {}
        for p in prof_list:
            st = profiles.detect_status(p, procs, units, dockers)
            port = p.get("port")
            if st == "stopped" and port and self._sync_check_port(port):
                st = "running"
            status_map[p["id"]] = st
        agents: List[AgentInfo] = []
        for p in prof_list:
            status = status_map[p["id"]]
            ui = p.get("ui")
            # vitals 闸门放在发现层（每个请求都算）：profiles 那边的同名单子有 30s 缓存，
            # 只靠它会造成「每次重启后假卡再活 30 秒」的反复。
            # 判定为不可用的统一跳过不出卡（静态画像、动态发现一律处理）。
            # 异常时判定层挂掉也不能把菜单清空。
            vrec = None
            try:
                vrec = vitals.vitals.get(p["id"])
            except Exception:  # noqa: BLE001
                vrec = None
            # 只有 truly gone（not_installed / broken）才跳过；
            # blocked_by_account 是额度/登录问题，agent 本身可用，保留卡片标状态。
            if vrec and vrec.get("verdict") in ("not_installed", "broken"):
                continue
            entries = profiles.entries_for(p, status)
            # 通用宿主探测：dict ui = 独立 Web 界面宿主（如 claude←cloudcli :3010）。
            # v0.13.151 改判（2026-10-25）：embed/open 入口**不再**等端口活才注入 ——
            # 与 agent string ui 同因（按需拉起下 stopped 是常态，v0.13.149 的
            # 「端口活才出原生会话」会把 cloudcli 卡的按钮在正常状态下删掉）。
            # 端口活性只保留两个既有语义：① 计入实体 running 证据；② endpoint 回落。
            # 停着的服务由前端 probeEmbed 出「目标界面未响应」覆盖层，按钮本身必须在。
            if isinstance(ui, dict):
                ui_alive = self._sync_check_port(ui.get("port"))
                entries = ([{"type": "embed", "label": "原生会话", "url": ui["url"]},
                            {"type": "open", "label": "↗UI", "url": ui["url"]}]
                           + entries)
                if ui_alive and status in ("installed", "stopped"):
                    status = "running"
                endpoint = ui["url"] if ui_alive else (p.get("panel") or "")
            else:
                endpoint = ui or p.get("panel") or (f"http://127.0.0.1:{p['port']}" if p.get("port") else "")
            # 死面板不出按钮（probe_port 过滤）—— 自 v0.13.151 起只覆盖 **panel 类**
            # 入口（services 的面板）：agent 的 embed/open 不再带 probe_port，按需拉起
            # 政策下它们的正常态就是停着的（用户 2026-10-25 报障「dsh的原生界面按钮
            # 也没有了」推翻 v0.13.149 把该 doctrine 扩展到 agent ui 的改判）。
            # v0.13.149（2026-10-24）：过滤判据从「loopback+::1 双栈」扩展为
            # _entry_alive——URL 端口与 probe_port 相同（URL 指向真实服务而非代理）时，
            # 按 URL 里的 host 再补探一次。为什么需要：dsh 只绑 192.168.5.102，
            # loopback/::1 全拒连，不补探会把**活着的** dsh 误判成死面板、按钮消失。
            # 反向保护：qwenpaw 的 ui 是注入代理 :3103 ≠ probe_port :8088 ⇒ 不补探，
            # 代理进程内常驻，补探会把停着的 qwenpaw 误判成活的。
            entries = [e for e in entries
                       if not (e.get("probe_port") and not self._entry_alive(e))]
            # 判定层异常 / 无记录：正常出卡，前端会显示 null 字段
            vv = _verdict_view(p, vrec)
            agents.append(AgentInfo(
                id=p["id"], name=p["name"], kind=p["kind"], status=status,
                endpoint=endpoint, port=p.get("port"),
                auth_required=bool(p.get("panel_auth")) or
                bool(p.get("panel") and re.search(r"18083|12700", str(p.get("panel")))),
                description=p.get("desc", ""),
                config_path=self._find_config_path(p["id"]),
                last_seen=self._iso_now(), builtin=True,
                working_dir=(p.get("terminal") or {}).get("cwd"),
                entries=entries,
                usable=vv["usable"], verdict=vv["verdict"],
                verdict_reason=vv["reason"], verdict_source=vv["source"],
                verdict_confidence=vv["confidence"], verdict_at=vv["at"],
                attested=vv["attested"],
                rt_state=vv.get("rt_state") or "", rt_note=vv.get("rt_note"),
                rt_model=vv.get("rt_model"),
                boundary=p.get("boundary") or boundary.cell_for_profile(p)))
        # 动态注册/扫描项（一律 service 类，仅快捷方式）
        for c in self._custom_agents():
            port = c.get("port")
            alive = bool(port and self._sync_check_port(port))
            agents.append(AgentInfo(
                id=c["id"], name=c["name"], kind="service",
                status="running" if alive else "stopped",
                endpoint=c.get("endpoint") or "", port=port, auth_required=False,
                description=c.get("description") or "", last_seen=self._iso_now(),
                builtin=False, working_dir=c.get("working_dir"),
                boundary=boundary.observed_product(),
                entries=([{"type": "open", "label": "打开面板", "url": c["endpoint"]}]
                         if c.get("endpoint") else []) + [{"type": "detail", "label": "详情"}]))
        for a in agents:
            self._cache[a.id] = a
        return agents

    def _probe_env(self):
        return (profiles.list_processes(),
                profiles.running_systemd_units(),
                profiles.docker_states())

    def _custom_agents(self) -> List[Dict]:
        if not self.db:
            return []
        try:
            rows = self.db.query("SELECT * FROM custom_agents ORDER BY created_at")
        except Exception:  # noqa: BLE001
            return []
        # 与画像去重：端口命中任一画像 port/panel 端口的 scan 项跳过
        prof_ports = set()
        for p in profiles.all_profiles():
            for k in ("port", "ui", "panel"):
                v = p.get(k)
                if isinstance(v, int):
                    prof_ports.add(v)
                elif isinstance(v, str):
                    m = re.search(r":(\d{2,5})", v)
                    if m:
                        prof_ports.add(int(m.group(1)))
        out = []
        for r in rows:
            if r["port"] and r["port"] in prof_ports:
                continue
            out.append(r)
        return out

    def reload(self):
        self._cache.clear()

    def _sync_check_port(self, port: Optional[int]) -> bool:
        """双栈探活（2026-09-21）：127.0.0.1 拒连不等于服务没起。
        实测 qwenpaw 2.2.1 只监听 IPv6（:8088 上 127.0.0.1=拒连、::1=通），
        单栈探测会把它误报成离线。"""
        if not port:
            return False
        return vitals.port_open(port) or vitals.port_open(port, "::1")

    def _entry_alive(self, e: dict) -> bool:
        """probe_port 入口活性（v0.13.149）：双栈 + URL host 补探。

        补探条件两条同时成立才生效：① URL 端口 == probe_port（URL 指向真实服务，
        不是 hub 注入代理之类的前置）；② URL host 不是回环（回环的上面已经探过）。
        v0.13.151 起只服务 **panel 类**入口（services 的面板）—— agent 的 embed/open
        不再带 probe_port（按需拉起下停了是常态，见 discover_all 同处注释）。
        机制本身保留：将来任何「面板死了就别出按钮」的场景仍走它。
        """
        port = e.get("probe_port")
        if self._sync_check_port(port):
            return True
        url = e.get("url") or ""
        m = re.search(r"https?://([^/:]+):(\d+)", url)
        if m and int(m.group(2)) == port and m.group(1) not in ("127.0.0.1", "localhost", "::1"):
            return vitals.port_open(port, m.group(1))
        return False

    def _find_config_path(self, agent_id: str) -> Optional[str]:
        paths = {
            "claude": Path.home() / ".claude" / "settings.json",
            "pi": Path.home() / ".pi" / "settings.json",
            "jcode": Path.home() / ".config" / "jcode" / "config.toml",
            "grok": Path.home() / ".grok" / "config.toml",
            "hermes": Path.home() / ".hermes" / "config.yaml",
            "ccr": Path.home() / ".ccr" / "config.json",
        }
        path = paths.get(agent_id)
        return str(path) if path and path.exists() else None

    def _iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        return self._cache.get(agent_id)
