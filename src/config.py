"""配置管理"""
import os
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"

#: **隔离类**变量：允许显式环境变量压过 `.env`（2026-10-09 起）。
#:
#: 为什么要有这个开关：`.env` 无条件覆盖会让「起一个影子实例用独立数据目录」
#: 这件事**静默失效** —— `DATA_DIR=work/shadow PORT=3205 venv/bin/python`
#: 实测拿到的仍是生产 `data/` 与 3102 端口（判例 105 坑2）。
#: 而 `run_dualstack.py` 的 `_port()` 早就做对了（`os.getenv("PORT")` 优先），
#: 只有本模块反着来 ⇒ **同一个进程里端口能覆盖、数据目录不能**，是纯粹的配置层不一致。
#:
#: 为什么是**白名单**而不是改成 `override=False`：`.env` 存着全部凭据
#: （`CCR_AUTH_TOKEN`/`HOOK_AUTH_TOKEN`/`TERM_TOKEN`/`HUB_PASSCODE`…，已被 .gitignore 排除）。
#: 一刀改成非覆盖 ⇒ 任何「不起 systemd 就裸跑」的场景都会丢凭据，
#: 从「隔离不了」变成「全挂」。故只放行**下面这几个不含凭据的隔离旋钮**，
#: 其余键继续由 `.env` 兜底，**生产行为逐字不变**。
#:
#: 本白名单**只增不改**：新增隔离旋钮必须先想清楚「误设它会不会写坏生产」。
ENV_OVERRIDABLE = ("DATA_DIR", "LOG_DIR", "PORT", "HOST")

#: **顺序是本修复的承重点**：`load_dotenv(override=True)` 会把 `os.environ` 改写成
#: `.env` 的内容，所以**任何在它之后读 `os.environ` 的快照都只能拿到 `.env` 的值**
#: —— 本文件第一版就是这么写的（快照在第 38 行、load_dotenv 在第 10 行），
#: 结果隔离开关看起来存在、实际一行都没生效，日志还打出一串 `.env` 的值当"生效证据"。
#: ⇒ 「我设的值」与「.env 里的值」两个来源**必须在被覆盖之前就分开保存**。
_env_override: Dict[str, str] = {
    k: os.environ[k] for k in ENV_OVERRIDABLE if os.environ.get(k)
}

if env_path.exists():
    load_dotenv(env_path, override=True)
    print(f"[Config] 已加载 .env（覆盖模式）: {env_path}")

if _env_override:
    print(f"[Config] 隔离变量优先于 .env: "
          + ", ".join(f"{k}={v}" for k, v in sorted(_env_override.items())))


class Config:
    """全局配置"""
    
    def __init__(self):
        # 强制使用 .env 中的值，即使环境变量已存在
        # ⚠️ 但白名单隔离旋钮（ENV_OVERRIDABLE）例外，见文件头注释。
        #    `os.getenv` 在load_dotenv(override=True) 之后**读不出**调用方的值，
        #    所以必须读模块级快照 `_env_override`，这是整个修复的承重点。
        self.port = int(_env_override.get("PORT") or os.getenv("PORT", "3102"))
        self.host = _env_override.get("HOST") or os.getenv("HOST", "0.0.0.0")
        
        # Agent 端点
        self.ccr_url = os.getenv("CLAUDE_CCR_URL", "http://127.0.0.1:3456")
        self.pi_url = os.getenv("PI_WEB_URL", "http://127.0.0.1:30141")
        self.jcode_url = os.getenv("JCODE_URL", "http://127.0.0.1:3457")
        self.tdaI_url = os.getenv("TDAI_URL", "http://127.0.0.1:8420")
        
        # 认证
        self.ccr_auth = os.getenv("CCR_AUTH_TOKEN", "")
        self.pi_api_key = os.getenv("PI_API_KEY", "")
        # CCR OpenAI 口(/v1/chat/completions)的 Bearer key：
        # 优先 CCR_OPENAI_KEY → jcode provider-ccr.env（实测唯一验证可用链）→ CCR_AUTH_TOKEN
        self.ccr_openai_key = self._resolve_ccr_openai_key()

        # 对话模型（CCR/jcode 直连时的默认 model，留空由网关路由）
        self.claude_model = os.getenv("CLAUDE_CHAT_MODEL", "")
        self.jcode_model = os.getenv("JCODE_CHAT_MODEL", "")

        # Manager Agent LLM 网关（FCC 优先，见 MEMORY PT-20260905-02）
        # 默认值指向 **CCR**（:3456）。旧默认是 FCC 的 :8082/v1，而 FCC 已于 2026-09-25 彻底退役
        # （PT-20260924-15：与 CCR 上游同 key、provider 为 CCR 子集、Claude 档实为 Qwen 别名）
        # ⇒ 留着它就是一个「.env 丢失/新克隆即指向死网关」的隐形故障源，与军规第 2 条
        # 「端口以实连为准：CCR API 口 3456」对齐。真值仍由 .env override，不改现有生产行为。
        self.manager_llm_base = os.getenv("MANAGER_LLM_BASE_URL", "http://127.0.0.1:3456/v1")
        self.manager_llm_key = os.getenv("MANAGER_LLM_API_KEY", "")
        self.manager_llm_model = os.getenv("MANAGER_LLM_MODEL", "agnes/agnes-2.5-flash")

        # Hook 遥测写入鉴权（留空=仅回环可写）
        self.hook_auth_token = os.getenv("HOOK_AUTH_TOKEN", "")
        
        # 数据路径
        # 2026-10-02：默认值由 ~/agent-hub/data 改为**仓库相对** data/，与同仓
        # memindex.DB_PATH（__file__/../data）同源。此前默认写死在迁移前的旧项目路径，
        # 而 config.__init__ 紧接着就 mkdir(parents=True) ⇒ 任何未加载 .env 的进程
        # （临时脚本 / 子代理 / 裸跑 pytest）都会**静默把第二个数据根重建出来**，
        # 且 rebuild 出来的空壳长得跟真数据根一模一样，专治"看不见的复发"。
        # 生产无行为变化：.env:21 的 DATA_DIR 仍优先生效。
        _repo_data = Path(__file__).resolve().parent.parent / "data"
        self.data_dir = Path(_env_override.get("DATA_DIR") or os.getenv("DATA_DIR", _repo_data))
        # 2026-10-10：`log_dir` 的默认推导曾被 `.env` 的**绝对** LOG_DIR 遮蔽 ——
        # 只覆盖 DATA_DIR 起影子时，data_dir 隔离了、log_dir 仍指生产（判例 PT-20261009-04 残留缺口）。
        # 根因：`_env_override.get("LOG_DIR") or os.getenv("LOG_DIR", ...)` 在 LOG_DIR 未显式覆盖时
        # 回退到 os.getenv ⇒ 读到的是 .env 的绝对生产路径，那个 `self.data_dir / "logs"` 默认值
        # **永远到不了**。修法：调用方覆盖了 DATA_DIR 却没给 LOG_DIR ⇒ 从**被覆盖的** data_dir 派生，
        # 让「影子数据根」与「影子日志根」同源。生产无行为变化（生产 _env_override 为空 ⇒ 走 else 兜底）。
        if "LOG_DIR" in _env_override:
            self.log_dir = Path(_env_override["LOG_DIR"])
        elif "DATA_DIR" in _env_override:
            self.log_dir = self.data_dir / "logs"
        else:
            self.log_dir = Path(os.getenv("LOG_DIR", self.data_dir / "logs"))
        
        # 确保目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
    
    def _resolve_ccr_openai_key(self) -> str:
        env_key = os.getenv("CCR_OPENAI_KEY", "")
        if env_key:
            return env_key
        import re
        p = Path.home() / ".config" / "jcode" / "provider-ccr.env"
        if p.is_file():
            try:
                m = re.search(r"JCODE_PROVIDER_CCR_API_KEY=(\S+)", p.read_text())
                if m:
                    return m.group(1)
            except OSError:
                pass
        return self.ccr_auth

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agents.db"
    
    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.json"


config = Config()
