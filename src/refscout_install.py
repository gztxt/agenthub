"""安装前体检（2026-10-31，PT-20261031-03 #3「安装测试执行链」的第一档）。

**为什么只做「体检」而不自动跑测试**：真跑测试要先装依赖（`npm ci` / `pip install`），
而依赖的 `postinstall` 钩子是**仓库自带的可执行代码**——在 NAS 上盲跑等于把「我们审计过
的源码」换成「它想跑什么就跑什么」。所以本模块分两档，且**默认只到第一档**：

  ① `probe()`：浅克隆到 `/vol1`（不是根分区）→ 只做**静态判定**（测试命令候选、
     安装钩子、许可证/copyleft、体积、可执行脚本），**一个字节都不执行**；
  ② `run_tests()`：仅当 `allow_run=True` **且** ①里没有安装钩子、没有 copyleft、
     体积在预算内时才真跑，超时硬杀，只回 stdout/stderr 尾部。

  第二档的依赖准备分两种，**都是为了不执行仓库自带的安装钩子**：
  Node 走 `npm ci --ignore-scripts`（pnpm/yarn 同 `--ignore-scripts`）；
  Python **不 pip install**（构建后端会跑 `setup.py`，风险等同 `postinstall`），
  改为把 `<root>/src` 与 `<root>` 注入 `PYTHONPATH` —— 实测 `pytest-dev/iniconfig`
  从 `ModuleNotFoundError` 变为 54 passed。第三方依赖缺失时测试照常失败并回显
  ImportError，那是**如实结论**，不做静默兜底。

判据来源（不做第二套）：copyleft 用 `refscout_base.GPL_RE`，候选资格用
`refscout_kb.install_candidates`。`probe` 不写知识库，只回结果。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import refscout_base as _rb

#: 克隆落盘根。**必须离开根分区**（实测根分区长期 >80%），且不放进仓库目录。
PROBE_ROOT = Path(os.getenv("REF_PROBE_ROOT", "/vol1/refscout-probe"))
#: 浅克隆超时与体积上限（MB）。超限即放弃，不"先克隆下来再看"。
CLONE_TIMEOUT_S = int(os.getenv("REF_PROBE_CLONE_TIMEOUT", "120"))
MAX_MB = int(os.getenv("REF_PROBE_MAX_MB", "200"))
#: 真跑测试的硬超时（秒）。跑不动就杀，不留后台进程。
RUN_TIMEOUT_S = int(os.getenv("REF_PROBE_RUN_TIMEOUT", "180"))
#: 装依赖的硬超时（秒）。冷缓存下 `npm ci` 比测试本身慢一个量级，故与测试分开计时。
INSTALL_TIMEOUT_S = int(os.getenv("REF_PROBE_INSTALL_TIMEOUT", "240"))

#: 测试命令候选（按检出顺序）。**只认这几条**：都能在无交互、无服务端依赖下跑。
TEST_COMMANDS: List[tuple] = [
    ("package.json", ["npm", "test", "--silent"]),
    ("pnpm-lock.yaml", ["pnpm", "test"]),
    ("yarn.lock", ["yarn", "test", "--silent"]),
    ("pyproject.toml", ["python3", "-m", "pytest", "-q"]),
    ("setup.py", ["python3", "-m", "pytest", "-q"]),
    ("pytest.ini", ["python3", "-m", "pytest", "-q"]),
    ("Cargo.toml", ["cargo", "test", "--quiet"]),
    ("go.mod", ["go", "test", "./..."]),
]

#: 会在「装依赖」阶段执行任意代码的钩子键。命中即**禁止**第二档。
INSTALL_HOOK_KEYS = ("preinstall", "install", "postinstall", "prepare")


def _repo_dir(full_name: str) -> Path:
    return PROBE_ROOT / full_name.replace("/", "__")


def _size_mb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return round(total / 1048576, 2)


def detect_test_command(root: Path) -> Optional[List[str]]:
    """按检出顺序找第一条可用的测试命令；找不到返回 None（≠ 失败）。"""
    for marker, cmd in TEST_COMMANDS:
        if (root / marker).exists():
            return list(cmd)
    return None


def install_hooks(root: Path) -> List[str]:
    """列出 package.json 里的安装期钩子（会在 `npm i` 时执行任意代码）。"""
    pkg = root / "package.json"
    if not pkg.exists():
        return []
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return ["<package.json 解析失败>"]
    scripts = data.get("scripts") or {}
    return sorted(k for k in scripts if k in INSTALL_HOOK_KEYS)


def license_kind(root: Path) -> Dict[str, Any]:
    """许可证粗判：copyleft 只用 base 的正则，不另立一套判据。"""
    text = ""
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        f = root / name
        if f.exists():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                text = ""
            break
    if not text:
        pkg = root / "package.json"
        if pkg.exists():
            try:
                text = str((json.loads(pkg.read_text(encoding="utf-8")) or {}).get("license") or "")
            except ValueError:
                text = ""
    return {"found": bool(text.strip()), "copyleft": bool(text and _rb.GPL_RE.search(text)),
            "head": text.strip().splitlines()[0][:80] if text.strip() else ""}


def probe(full_name: str, ref: str = "") -> Dict[str, Any]:
    """浅克隆 + 静态体检。**不执行任何仓库内代码**。

    返回 `{ok, repo, dir, size_mb, test_command, install_hooks, license, executables,
    can_run, reasons}`。`can_run=False` 时 `reasons` 写明为什么不让跑。
    """
    full_name = (full_name or "").strip()
    if "/" not in full_name:
        return {"ok": False, "error": "需要 owner/repo 形状"}
    dst = _repo_dir(full_name)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/%s.git" % full_name
    cmd = ["git", "clone", "--depth", "1", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "克隆超时 %ds" % CLONE_TIMEOUT_S, "repo": full_name}
    if r.returncode != 0:
        return {"ok": False, "repo": full_name, "error": (r.stderr or "clone failed")[-300:]}
    size = _size_mb(dst)
    hooks = install_hooks(dst)
    lic = license_kind(dst)
    test_cmd = detect_test_command(dst)
    reasons: List[str] = []
    if size > MAX_MB:
        reasons.append("体积 %.0fMB 超上限 %dMB" % (size, MAX_MB))
    if hooks:
        reasons.append("安装期钩子会在装依赖时执行任意代码: %s" % ", ".join(hooks))
    if lic["copyleft"]:
        reasons.append("copyleft 许可证（军规：不得自行引入）")
    if not test_cmd:
        reasons.append("没识别到测试入口（不是失败，是不知道跑什么）")
    executables = [str(p.relative_to(dst)) for p in dst.rglob("*")
                   if p.is_file() and os.access(p, os.X_OK)][:10]
    return {"ok": True, "repo": full_name, "dir": str(dst), "size_mb": size,
            "test_command": test_cmd, "install_hooks": hooks, "license": lic,
            "executables": executables, "can_run": not reasons, "reasons": reasons}


def test_env(root: Path) -> Dict[str, str]:
    """测试进程的环境：只注入 import 路径，**不装任何包、不执行仓库代码**。"""
    env = os.environ.copy()
    src = root / "src"
    parts = ([str(src)] if src.is_dir() else []) + [str(root)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def node_install_cmd(root: Path, tool: str) -> Optional[List[str]]:
    """Node 系的装依赖命令，**一律带 `--ignore-scripts`**。

    装包时会执行仓库的 `preinstall/postinstall/prepare`——那正是 `probe` 要拦的东西。
    probe 已经拦了一道，这里再兜一道：判据漂移时也不会盲跑仓库代码。
    """
    if tool == "npm":
        base = ["npm", "ci"] if (root / "package-lock.json").exists() else ["npm", "install"]
        return base + ["--ignore-scripts", "--no-audit", "--no-fund"]
    if tool == "pnpm":
        return ["pnpm", "install", "--ignore-scripts"]
    if tool == "yarn":
        return ["yarn", "install", "--ignore-scripts"]
    return None


def prepare(root: Path, test_cmd: List[str]) -> Dict[str, Any]:
    """第二档的前置：装依赖。返回 `{ran, cmd, rc, tail|error}`。

    只有 Node 系需要前置：Python 走 `test_env` 的 `PYTHONPATH`，cargo/go 的测试
    子命令自带拉依赖。**装依赖失败也算结果**（`stage=deps`），不报成「测试失败」。
    """
    cmd = node_install_cmd(root, (list(test_cmd) or [""])[0])
    if not cmd:
        return {"ran": False, "reason": "无需前置（Python 走 PYTHONPATH；cargo/go 自带）"}
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ran": True, "cmd": cmd, "rc": -1,
                "error": "装依赖超时 %ds，已杀进程" % INSTALL_TIMEOUT_S}
    except FileNotFoundError as e:
        return {"ran": True, "cmd": cmd, "rc": -1, "error": "命令不存在: %s" % e}
    return {"ran": True, "cmd": cmd, "rc": r.returncode,
            "tail": ((r.stdout or "") + (r.stderr or ""))[-1200:]}


def run_tests(full_name: str, allow_run: bool = False, extra_args: Optional[List[str]] = None
              ) -> Dict[str, Any]:
    """第二档：真跑测试。**默认拒绝**，且必须过第一档的全部禁令。

    为什么默认拒绝：这一档会执行仓库自带的测试/构建脚本，可能联网、可能写盘。
    调用方必须显式 `allow_run=True`（前端按钮 = 人工点头），且 `probe` 里
    `can_run=False` 时这里一律 403，不给"绕过体检"的口子。
    """
    if not allow_run:
        return {"ok": False, "skipped": True,
                "reason": "默认不跑：执行外部仓库的测试需显式 allow_run=true（人工点头）"}
    info = probe(full_name)
    if not info.get("ok"):
        return info
    if not info.get("can_run"):
        return {"ok": False, "refused": True, "reasons": info.get("reasons") or []}
    root = Path(info["dir"])
    cmd = list(info["test_command"] or []) + list(extra_args or [])
    prep = prepare(root, cmd)
    if prep.get("ran") and prep.get("rc") != 0:
        return {"ok": False, "stage": "deps", "cmd": cmd, "prep": prep, "dir": info["dir"],
                "reasons": [prep.get("error") or "装依赖失败 rc=%s" % prep.get("rc")]}
    try:
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S, env=test_env(root))
        out, err = (r.stdout or ""), (r.stderr or "")
        return {"ok": r.returncode == 0, "rc": r.returncode, "cmd": cmd, "prep": prep,
                "stdout_tail": out[-2000:], "stderr_tail": err[-1000:],
                "size_mb": info["size_mb"], "dir": info["dir"]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": RUN_TIMEOUT_S, "cmd": cmd, "prep": prep,
                "error": "测试超时 %ds，已杀进程" % RUN_TIMEOUT_S}
    except FileNotFoundError as e:
        return {"ok": False, "cmd": cmd, "prep": prep, "error": "命令不存在: %s" % e}


def cleanup(full_name: str) -> Dict[str, Any]:
    dst = _repo_dir(full_name)
    existed = dst.exists()
    shutil.rmtree(dst, ignore_errors=True)
    return {"removed": existed, "dir": str(dst)}
