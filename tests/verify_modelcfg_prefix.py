"""L2 实弹探针（v0.13.86）：设置页「能点到的值」== 写手「能写进去的值」。

跑法：`bash scripts/run_tests.sh probe verify_modelcfg_prefix.py`
（本文件在 tests/ 下、名字不是 test_*，故天然不进 unittest discover —— 见 tests/tiers.py）


用**本机真实配置文件形状**（拷贝到临时 HOME，绝不碰原文件）跑三条路径：
  ① opencode：模拟前端把下拉选项值（prefix + 裸 ID）提交 → 必须落笔成功、
     且 provider.ccr.models 的键是无前缀裸 ID（本机实测形状）
  ② opencode：裸 CCR ID（改前前端会提交的东西）→ 必须被拒（不许悄悄放行）
  ③ qoder：NATIVE_MODELS 里每个名字 → 必须落笔成功；CCR ID → 必须 400
"""
import json, os, pathlib, shutil, sys, tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

home = tempfile.mkdtemp(prefix="hub-prefix-e2e-")
real = pathlib.Path(os.path.expanduser("~"))
for rel in (".config/opencode/opencode.json", ".qoder/settings.json", ".cursor/cli-config.json"):
    dst = pathlib.Path(home) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real / rel, dst)
os.environ["HOME"] = home

import db  # noqa: E402
db.init_db(pathlib.Path(home) / "hub.db")     # set_hub_model 要走 hub 侧库
import modelcfg  # noqa: E402
import modelcfg_writers
modelcfg_writers._home = lambda: pathlib.Path(home)  # 2026-10-31 拆模块后打在使用处

ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS {name}")
    else:
        fail += 1; print(f"  FAIL {name} {detail}")

st = {a["id"]: a for a in modelcfg.list_agents()}
print("① opencode：下拉选项值 = prefix + 裸 CCR ID")
bare = "alibaba/qwen3.8-max"
prefixed = st["opencode"]["value_prefix"] + bare
check("value_prefix 与声称一致", prefixed == "ccr/alibaba/qwen3.8-max", prefixed)
OCP = pathlib.Path(home) / ".config/opencode/opencode.json"
before = json.loads(OCP.read_text())
try:
    modelcfg.apply_model("opencode", prefixed)
    d = json.loads(OCP.read_text())
    check("落笔成功", d["model"] == prefixed, d["model"])
    check("provider.models 键是无前缀裸 ID（本机实测形状）", bare in d["provider"]["ccr"]["models"])
    check("small_model 同源跟随", d["small_model"] == prefixed, d["small_model"])
    # 「不破坏原生」的最强判据：**除目标两键外，整份 JSON 必须逐键相等**
    import copy
    # ⚠ 必须 deepcopy：浅拷贝下 rest_before["provider"] 与 before["provider"] 是同一对象，
    # 下面那行置 None 会把用来对比的基准也一起改掉（第一版探针就是这么自伤的）。
    rest_before = copy.deepcopy({k: v for k, v in before.items()
                                 if k not in ("model", "small_model")})
    rest_after = copy.deepcopy({k: v for k, v in d.items() if k not in ("model", "small_model")})
    rest_before["provider"]["ccr"]["models"] = None   # 登记是允许的插入，单独比
    rest_after["provider"]["ccr"]["models"] = None
    same = rest_before == rest_after
    check("无关面逐键不变（permission/mcp/provider.options…）", same,
          f"差异={[k for k in set(rest_before)|set(rest_after) if rest_before.get(k)!=rest_after.get(k)]}")
    old_models = before["provider"]["ccr"]["models"]
    new_models = d["provider"]["ccr"]["models"]
    check("原有登记一条没删", all(k in new_models for k in old_models),
          f"{len(old_models)}→{len(new_models)}")
    check("终端注入值同源", modelcfg.terminal_argv("opencode", prefixed) == ["--model", prefixed])
except Exception as e:
    check("落笔成功", False, repr(e))

print("② opencode：裸 ID 仍必须被拒（不许悄悄放行）")
try:
    modelcfg.apply_model("opencode", bare)
    check("裸 ID 被拒", False, "竟然通过了")
except modelcfg.ModelCfgError as e:
    check("裸 ID 被拒", e.status == 409, f"status={e.status}")

print("③ qoder：原生清单每个名字都能落笔；CCR ID 必须 400")
for nm in st["qoder"]["models"]:
    try:
        modelcfg.apply_model("qoder", nm)
        d = json.loads((pathlib.Path(home) / ".qoder/settings.json").read_text())
        check(f"原生名 {nm} 落笔", d["model"]["name"] == nm)
        check(f"原生名 {nm} 不破坏 securityScan/permissions",
              "securityScan" in d and "permissions" in d)
    except Exception as e:
        check(f"原生名 {nm} 落笔", False, repr(e))
try:
    modelcfg.apply_model("qoder", bare)
    check("CCR ID 被拒", False, "竟然通过了")
except modelcfg.ModelCfgError as e:
    check("CCR ID 被拒", e.status == 400, f"status={e.status}")

print("④ cursor：仍只动 4 处（回归）")
CUR = pathlib.Path(home) / ".cursor/cli-config.json"
cb = json.loads(CUR.read_text())
try:
    modelcfg.apply_model("cursor", "ccr/agnes/agnes-3.0-pro")
    d = json.loads(CUR.read_text())
    check("model.modelId", d["model"]["modelId"] == "ccr/agnes/agnes-3.0-pro")
    check("参数表补索引", "ccr/agnes/agnes-3.0-pro" in d["modelParameters"])
    untouched = ("permissions", "display", "editor", "sandbox", "version", "network",
                 "attribution", "hooks", "notifications", "hints", "steering", "rewind",
                 "modelSlashCommands", "exploreSubagentModel")
    bad = [k for k in untouched if k in cb and cb[k] != d.get(k)]
    check("permissions/display/editor/sandbox 等逐键未动", not bad, f"被改了：{bad}")
except Exception as e:
    check("cursor 落笔", False, repr(e))

print(f"\n结果：PASS={ok} FAIL={fail}")
shutil.rmtree(home, ignore_errors=True)
sys.exit(1 if fail else 0)
