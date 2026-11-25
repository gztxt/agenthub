"""`nanobot.agent.plugins` 的替身（**适配层，非上游副本**）。

为什么需要：上游 `apps/cli/service.py` 的 `_record_installed()` 里有一句
`from nanobot.agent.plugins import set_agent_plugin_enabled`。真正的 plugins.py
（537 行）会拖进 `pydantic` + `nanobot.config.loader` + `nanobot.config.schema`，
那是整个 nanobot 的配置子系统；为了一个「写 enabled 标记」的调用把它整体搬进来，
等于把 hub 绑到 nanobot 的配置模型上——升级上游时最先爆炸的就是这一层。

所以这里只实现 service.py **实际调用到的**那一个函数，语义按上游对照：
  · 上游 = 在 `<workspace>/plugins/<name>/data/enabled` 写一个激活指纹，0600；
  · 这里 = 同样写这个标记文件（内容换成显式说明串），0600。
差别只有「指纹算法」——那个指纹只有 nanobot 自己的 loader 会读，hub 用的是自己的
技能路由（`src/skill.py`），不消费它。

**判据**：如果将来升级上游后发现 service.py 多调了 plugins 的别的函数，
本文件必须补实现或改回整包 vendor；静默不实现会变成 AttributeError 在最难复现的安装路径上。
"""
from __future__ import annotations

from pathlib import Path

_MARKER_NOTE = "enabled-by=hub-cliapps-adapter\n"


def _plugin_data_dir(workspace: Path, name: str, *, create: bool = False) -> Path:
    data = Path(workspace) / "plugins" / name / "data"
    if create:
        data.mkdir(parents=True, exist_ok=True)
    return data


def set_agent_plugin_enabled(workspace: Path, name: str, enabled: bool) -> None:
    """与上游同名同签名：写/删 `<workspace>/plugins/<name>/data/enabled`。"""
    plugin_root = Path(workspace) / "plugins" / name
    if not (plugin_root / "plugin.json").is_file():
        # 上游在这里抛 ValueError；保持同语义，别让安装路径静默成功
        raise ValueError(f"unknown Agent Plugin '{name}'")
    marker = _plugin_data_dir(workspace, name, create=True) / "enabled"
    if enabled:
        marker.write_text(_MARKER_NOTE, encoding="utf-8")
        marker.chmod(0o600)
    else:
        marker.unlink(missing_ok=True)


def enabled_agent_plugin_skills(workspace: Path):
    """hub 不消费 nanobot 的插件启用态（技能发现走 src/skill.py）。

    显式返回空表而不是抛异常：上游 `enabled_agent_plugin_skill_documents` 之类
    若被将来某条路径调到，返回空表等于「没有额外技能」，不会伪造出技能。
    """
    return []
