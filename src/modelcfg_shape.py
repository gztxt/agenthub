"""模型 ID 形状闸门 + agent 常量（2026-10-31 从 modelcfg.py 拆出，B4 片2）。

依赖方向：本模块**不 import 任何本组的兄弟模块**，任何人不许在这里加 I/O。
拆它的理由：设置页的「能不能写」判据（MODEL_ID_RE / MODEL_ARGV / WRITE_MODE /
VALUE_PREFIX）原来和 HTTP 与写盘混在 1871 行里，改一条判据要在一堆写盘代码里找。
"""
from __future__ import annotations

import re
from typing import Dict, List

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,119}$")

#: 终端注入白名单（实测 `<cli> --help` 输出）：值 = 传给该 CLI 的 flag。
#: 只有这里在册的 agent 才会被 hub 拉起时追加 --model；其余仅写配置文件。
MODEL_ARGV: Dict[str, str] = {
    "claude": "--model",
    "jcode": "--model",
    "codex": "--model",
    "grok": "--model",
    "pi": "--model",
    "hermes": "-m",          # hermes help 只给短选项 [-m MODEL]
    # ── 2026-10-07 新增三家（逐家 `--help` 实测，不是猜的）──
    "opencode": "--model",   # opencode --help: -m, --model  model to use in the format of provider/model
    "qoder": "--model",      # qodercli --help: -m, --model <model>  Model for the current session
    "cursor": "--model",     # cursor-agent --help: --model <model>  Model to use (e.g. gpt-5, sonnet-4-thinking)
    # ── 2026-10-25 新增（omp 入册，实测 omp --help：`--model=<value>  Model to use
    #    (fuzzy match: "opus", "gpt-5.2", or "openai/gpt-5.2")`）──
    "omp": "--model",
}

#: 写入模式（2026-10-07，用户裁定「原生 + 插入」）。三类语义不同，设置页必须分开说：
#:   "ccr"    —— 该字段**就是**给 CCR/OpenAI 兼容网关用的，可直写 CCR 的 `provider/model` ID。
#:   "native" —— 该字段只认**本家原生模型名**（或本家 ID 形状）。hub 可以写，但要写原生名；
#:               塞 CCR ID 会被 CLI 拒（qoder 实测），故 _qoder_write 里硬拒并给出替代路径。
#:   "argv"   —— **无默认配置文件可落**，只能靠 hub 拉起终端时的 `--model` 注入。
#:               设置页对这类要写明「只影响 hub 拉起的会话」，避免"改了没生效"的误解。
#: 这一列同时是设置页文案与测试判据的单一真相源。
WRITE_MODE: Dict[str, str] = {
    "claude": "ccr", "jcode": "ccr", "codex": "ccr", "pi": "ccr",
    "grok": "ccr", "hermes": "ccr",
    "opencode": "ccr", "cursor": "ccr",
    "qoder": "native",
    # argv-only：无默认模型配置文件可落（见 _omp_read），hub 侧设置只经终端注入。
    "omp": "argv",
}

#: 该 agent 的模型字段要求的**值前缀**（v0.13.86，2026-10-07；2026-10-25 补 omp）。
#: 为什么需要：CCR 清单（/api/models）给的是裸 ID（`alibaba/qwen3.8-max`），而
#: opencode 的 `model` / `--model` 收的是 `provider/model_id` 形状 —— 官方文档 Models 页
#: 原话「The format is `provider/model`」，本机同名 CLI 亦实测回 `ccr/<id>`（`opencode
#: models ccr` 逐行都是 `ccr/…`）。用户若照着清单点裸 ID，`_opencode_write` 会 409
#: 且指错方向（它说「没有 provider alibaba」，实际缺的是 `ccr/` 前缀）。
#: ⇒ 设置页据此把选项值拼成 `<前缀><CCR ID>`，让"能点到的"与"能写进的"是同一个东西。
#: omp 同族（2026-10-25 实测）：`omp --model openrouter/openrouter/free` 报
#: 「No API key found for openrouter」—— 它把裸 ID 的**首段**当成 provider 名；
#: 正确形状是 `ccr/openrouter/openrouter/free`（provider key = ~/.omp/agent/models.yml
#: 里的 `ccr`，实测该形状一句话正常返回）。⚠ 前缀跟着 provider key 走，用户若把
#: models.yml 里的 provider 改名，这里要同步改。
VALUE_PREFIX: Dict[str, str] = {"opencode": "ccr/", "omp": "ccr/"}

#: 该 agent 的**原生模型清单**（v0.13.86）。取证：`qodercli --list-models` 实测输出
#: （2026-10-07）= Qwen3.8-Max / Qwen3.8-Flash；`~/.qoder/.models/default` 的 key
#: 亦为 `qmodel_38max`，与该清单同族。qoder 的 `model.name` **只认原生名**（塞 CCR ID
#: 会被 `_qoder_write` 硬拒）⇒ 设置页若只摆 CCR 清单，用户点任一 ID 都必然 400，
#: 属"看起来能选、实际必被拒"。故把原生清单透出来，且对 native 型 agent 不再摆 CCR 清单。
#: ⚠ 清单会随 qoder 发版变化：这里的提示语写明「以 qodercli --list-models 为准」，
#: 写入侧仍由 `_qoder_write` 复核形状（真源不在这张表）。
NATIVE_MODELS: Dict[str, List[str]] = {
    "qoder": ["Qwen3.8-Max", "Qwen3.8-Flash"],
}

#: pi 走 CCR 时用的 provider（~/.pi/agent/models.json 里 baseUrl = CCR 的那个）
PI_CCR_PROVIDER = "ccr-free"
#: grok 的命名 profile 键（[model.<key>] + [models].default 指向它）
GROK_PROFILE_KEY = "ccr-hub"
#: hermes 走 CCR 的 provider 名（config.yaml 顶层 model.provider）
HERMES_CCR_PROVIDER = "ccr-free"


class ModelCfgError(Exception):
    """带 HTTP 语义的配置错误（message 直接回给前端）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status

def bare_model_id(agent_id: str, model: str) -> str:
    """把该 agent 的落点值还原成 CCR 裸模型 ID（用于比对清单）。

    opencode/omp 的字段带 provider 前缀（`ccr/alibaba/...`），reasonix 若接入同族。
    只剥**该 agent 登记过的**前缀，不做通用剥离 —— 通用剥离会把 `openrouter/xxx`
    这种真 ID 的首段吃掉，造成"明明在清单里却判成不在"。"""
    pre = VALUE_PREFIX.get(agent_id, "")
    if pre and model.startswith(pre):
        return model[len(pre):]
    return model

def validate_model(model: str) -> str:
    m = (model or "").strip()
    if not m:
        raise ModelCfgError("模型 ID 为空")
    if not MODEL_ID_RE.match(m):
        raise ModelCfgError("模型 ID 形状不合法（只允许字母数字与 . _ : / + -，≤120 字符）")
    return m

#: 漂移比对豁免表（形状不可比的 agent，见 modelcfg_verify.drift_report）
DRIFT_EXEMPT: Dict[str, str] = {
    "qoder": "字段为本家原生模型名，与 CCR 的 provider/model ID 不可互比，"
             "CCR 模型只经终端 --model 注入生效",
}
