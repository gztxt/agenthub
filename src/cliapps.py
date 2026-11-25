"""CLI Apps（应用功能模块）：nanobot 应用模块的 hub 适配层。

## 这是什么

`nanobot`（HKUDS，MIT）自带的**应用管理模块**——从 CLI-Anything / nanobot-extension
等注册表拉目录，装/更新/卸载 CLI 应用，并给每个应用生成一份 SKILL.md，让 agent
知道什么时候用它、怎么用。上游实现逐字节 vendored 在
`src/vendor/cli_apps/nanobot/apps/`，本文件**不改上游一行代码**，只负责四件事：

1. **import 面**：把 `src/vendor/cli_apps` 塞进 `sys.path`，让 `import nanobot.apps.cli`
   能穿到 vendored 副本（上游 service.py 里是绝对 import，改它就破坏字节级）；
2. **实例参数**：`CliAppManager(workspace=..., data_dir=...)`。上游把这两个都做成
   可注入构造参数，所以 hub 不需要碰任何全局配置；
3. **技能桥接**：上游把技能写到 `<workspace>/plugins/cli-app-x/skills/cli-app-x/SKILL.md`，
   而 hub 的技能白名单只认 `/fs/1000/ftp/技术文档/skills`（`src/skill.py:SKILL_DIRS["techdocs"]`）。
   ⇒ 安装后补一条软链 `skills/cli-app-x -> ../plugins/cli-app-x/skills/cli-app-x`，
   让 **hub 技能路由 + 各 agent 自己的技能发现**都能看到它（这是「所有 agent 自动调用」的那一环）。
4. **阻塞隔离**：安装最长 300s、运行最长 600s 的子进程，REST 层用 `asyncio.to_thread`
   把它甩出事件循环，别把 hub 的 /health 一起卡住。

## 落点（都可 env 覆盖，测试靠它隔离）

| 项 | 默认 | env |
|---|---|---|
| workspace | `/fs/1000/ftp/技术文档` | `HUB_APPS_WORKSPACE` |
| 状态/缓存 | `agenthub/data/cli-apps` | `HUB_APPS_DATA` |
| 安装超时 | 300s | `HUB_APPS_INSTALL_TIMEOUT` |
| 运行超时 | 60s | `HUB_APPS_RUN_TIMEOUT` |

## 为什么 workspace 是工作区根而不是 hub 目录

技能落到 `<workspace>/skills/` 才在 `SKILL_DIRS["techdocs"]` 里；落到 hub 目录里
等于每个 agent 都要额外加一条发现点，而 5 路 agent 的发现点各不相同——那是
「装了但没人能找到」的经典形态（判例 268）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

#: vendored 副本根（含 `nanobot/` 包与 `loguru.py`/`httpx.py` 两个替身）
VENDOR = Path(__file__).resolve().parent / "vendor" / "cli_apps"

_VENDOR_LOCK = threading.Lock()
_VENDOR_READY = False


def bootstrap_vendor() -> None:
    """把 vendored 根放进 sys.path（幂等，线程安全）。

    顺序上必须**插到最前**：hub venv 里若哪天装了真的 nanobot，`import nanobot`
    会拿到整包上游而不是我们的 vendored 副本——那份没有 hub 需要的适配（例如
    我们的 `agent/plugins.py` 替身），行为会变但不会报错。插最前消除这个歧义。
    """
    global _VENDOR_READY
    if _VENDOR_READY:
        return
    with _VENDOR_LOCK:
        if _VENDOR_READY:
            return
        if not VENDOR.is_dir():
            raise RuntimeError(f"vendored CLI Apps 目录不存在：{VENDOR}")
        p = str(VENDOR)
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
        _VENDOR_READY = True


def workspace_path() -> Path:
    return Path(os.getenv("HUB_APPS_WORKSPACE") or "/fs/1000/ftp/技术文档").expanduser()


def data_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / "data" / "cli-apps"
    d = Path(os.getenv("HUB_APPS_DATA") or default).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runtime_config():
    from nanobot.apps.cli import CliAppsRuntimeConfig

    return CliAppsRuntimeConfig(
        install_timeout=int(os.getenv("HUB_APPS_INSTALL_TIMEOUT", "300")),
        run_timeout=int(os.getenv("HUB_APPS_RUN_TIMEOUT", "60")),
    )


def get_manager():
    """每次现建：上游 manager 无状态（状态在 installed.json 与 registry 缓存里），
    而 data_dir 允许被 env 换掉——缓存实例会让测试/影子实例读到生产的目录。"""
    bootstrap_vendor()
    from nanobot.apps.cli import CliAppManager

    return CliAppManager(
        workspace=workspace_path(),
        data_dir=data_dir(),
        runtime=_runtime_config(),
    )


# ── 技能桥接 ────────────────────────────────────────────────────────────


def _app_skill_name(name: str) -> str:
    bootstrap_vendor()
    from nanobot.apps.cli.service import _skill_name  # 同版本副本里的私有函数

    return _skill_name(name)


def bridge_skill(name: str) -> Dict[str, Any]:
    """把上游写在 `plugins/…` 下的技能软链进 `skills/`，让全部 agent 的发现点可见。

    返回 {"linked": bool, "link": str, "reason": str}；**失败不抛**——安装本身已经成功，
    技能不可见是降级不是失败，但要如实回给调用方（静默降级是本仓判例 268 的病根）。
    """
    ws = workspace_path()
    skill = _app_skill_name(name)
    src = ws / "plugins" / skill / "skills" / skill
    link = ws / "skills" / skill
    if not (src / "SKILL.md").is_file():
        return {"linked": False, "link": str(link), "reason": f"上游技能未落盘：{src}/SKILL.md 不存在"}
    try:
        if link.is_symlink():
            if link.resolve(strict=False) == src.resolve(strict=False):
                return {"linked": True, "link": str(link), "reason": "已存在且指向同一目录"}
            link.unlink()
        elif link.exists():
            return {"linked": False, "link": str(link),
                    "reason": "同名真实目录已存在（不覆盖，报请人工确认）"}
        link.symlink_to(Path("..") / "plugins" / skill / "skills" / skill)
        return {"linked": True, "link": str(link), "reason": "新建软链"}
    except OSError as e:
        return {"linked": False, "link": str(link), "reason": f"{type(e).__name__}: {e}"}


def bridge_all() -> List[Dict[str, Any]]:
    """把已安装应用逐个桥一遍（幂等）。用于「上游装了但 hub 看不见」的修复路径。"""
    mgr = get_manager()
    return [bridge_skill(n) | {"name": n} for n in mgr.installed_names()]


# ── 业务薄封装（REST/MCP 只调这些）──────────────────────────────────────


class AppsUnavailable(RuntimeError):
    """vendored 模块不可用（缺依赖/目录被删）。REST 层映射 503，不伪装成 500。"""


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except AppsUnavailable:
        raise
    except ImportError as e:
        raise AppsUnavailable(f"CLI Apps 依赖缺失：{e}") from e


def catalog(*, force_refresh: bool = False, cache_only: bool = False) -> Dict[str, Any]:
    return _guard(lambda: get_manager().payload(
        force_refresh=force_refresh, cache_only=cache_only))


def installed() -> Dict[str, Any]:
    return _guard(lambda: get_manager().installed_payload())


def install(name: str) -> Dict[str, Any]:
    payload = _guard(lambda: get_manager().install(name))
    payload["skill_bridge"] = bridge_skill(name)
    return payload


def update(name: str) -> Dict[str, Any]:
    payload = _guard(lambda: get_manager().update(name))
    payload["skill_bridge"] = bridge_skill(name)
    return payload


def uninstall(name: str) -> Dict[str, Any]:
    payload = _guard(lambda: get_manager().uninstall(name))
    payload["skill_bridge"] = _unbridge(name)
    return payload


def test_install(name: str) -> Dict[str, Any]:
    return _guard(lambda: get_manager().test(name))


def _unbridge(name: str) -> Dict[str, Any]:
    """卸载后收掉软链（上游只删 plugins/ 下的目录，不管我们加的软链）。"""
    link = workspace_path() / "skills" / _app_skill_name(name)
    if link.is_symlink():
        try:
            link.unlink()
            return {"linked": False, "link": str(link), "reason": "已删除软链"}
        except OSError as e:
            return {"linked": True, "link": str(link), "reason": f"{type(e).__name__}: {e}"}
    return {"linked": False, "link": str(link), "reason": "没有软链（无需清理）"}


def run(name: str, args: Optional[List[str]] = None, *, json_output: bool = False,
        working_dir: Optional[str] = None, timeout: Optional[int] = None) -> str:
    return _guard(lambda: get_manager().run(
        name, list(args or []), json_output=json_output,
        working_dir=working_dir, timeout=timeout,
        restrict_to_workspace=True,   # hub 侧一律锁在工作区内（上游默认 False）
    ))


def health() -> Dict[str, Any]:
    """自检：vendored 可 import、data 目录可写、技能根在 SKILL_DIRS 里。"""
    out: Dict[str, Any] = {"vendor": str(VENDOR), "workspace": str(workspace_path()),
                           "data": str(data_dir()), "ok": False, "notes": []}
    try:
        bootstrap_vendor()
        from nanobot.apps.protocol import APP_PROTOCOL_SCHEMA
        from nanobot.apps.cli import CliAppManager  # noqa: F401
        out["protocol_schema"] = APP_PROTOCOL_SCHEMA
    except Exception as e:  # 显式失败，不静默
        out["notes"].append(f"import 失败：{type(e).__name__}: {e}")
        return out
    # 上游用到 yaml 解析 SKILL.md frontmatter；缺了就降级成 fallback 技能
    try:
        import yaml  # noqa: F401
        out["yaml"] = True
    except ImportError:
        out["yaml"] = False
        out["notes"].append("PyYAML 缺失：应用技能会用 fallback 模板（功能可用但不完美）")
    ws = workspace_path()
    out["skills_root"] = str(ws / "skills")
    out["skills_root_exists"] = (ws / "skills").is_dir()
    try:
        (data_dir() / ".write-probe").write_text("ok", encoding="utf-8")
        (data_dir() / ".write-probe").unlink()
        out["data_writable"] = True
    except OSError as e:
        out["data_writable"] = False
        out["notes"].append(f"data 目录不可写：{e}")
    out["ok"] = bool(out["skills_root_exists"] and out.get("data_writable"))
    return out
