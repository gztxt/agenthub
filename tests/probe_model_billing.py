"""探针：模型下拉的 [免费]/[付费] 标注与付费二次确认（真浏览器，非 hook）。

为什么要真浏览器：`modelOptionLabel` / `modelConfirmPaid` 的纯函数闸门
（`tests/test_models_billing.py`）证明不了三件事 ——
  ① 标记真的进了 `<option>`（而不是只在函数返回值里）；
  ② 原生 `confirm` 在 renderer 里会把页面挂住，**必须被应答**；
  ③ 拒绝之后 select **真的回到原值**（这是 §4.2 ③「浮层单一出口」的 UI 版）。

口径：`Page.javascriptDialogOpening` 由本探针显式应答；不应答就会
「Input.dispatchMouseEvent 无应答」—— 09-23 的探针就是这么栽的。

用法：python3 tests/probe_model_billing.py http://127.0.0.1:3202
"""
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3202"
PORT = 9333
PROFILE = str(_HERE.parent / "work" / "_chromeprof-billing")   # 仓内产物，不落 /tmp

_result = {"dialog": None, "answer": None}
_lock_holder = {}


def on_event(method, params):
    """应答原生 confirm：按 `_result['answer']` 决定接受/拒绝。"""
    if method != "Page.javascriptDialogOpening":
        return
    _result["dialog"] = params.get("message", "")
    accept = bool(_result["answer"])
    try:
        cdp = _lock_holder.get("cdp")
        cdp.send("Page.handleJavaScriptDialog", accept=accept, promptText="")
    except Exception as e:                              # noqa: BLE001
        print("  !! 应答对话框失败:", e)


def wait_for(cdp, expr, desc, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cdp.eval(expr)
        except Exception:                               # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(0.4)
    raise RuntimeError("等待超时：%s" % desc)


def main():
    proc = launch_chrome(URL, PORT, PROFILE, width=1280, height=900)
    cdp = None
    try:
        cdp = CDP(page_target(PORT), on_event=on_event)
        _lock_holder["cdp"] = cdp
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")

        # 首页默认停在「终端」模式，模型下拉的加载函数只在对话模式里被调（02-nav:167）
        # ⇒ 显式触发一次，等价于用户点一下「对话」页签。
        # ⚠ 必须 awaitPromise：`loadChatModels` 是 async，不 await 时返回的是
        #   pending Promise（序列化成 `{}`），探针会误判「点了没反应」。
        # ⚠ 还要先等函数定义出来：脚本是顺序执行的，`#chatModel` 存在不代表
        #   hub.js 已跑到 04 片（首次就是在这里把 ReferenceError 吞成了超时）。
        wait_for(cdp, "typeof loadChatModels === 'function'", "loadChatModels 定义")
        r = cdp.send("Runtime.evaluate", awaitPromise=True, returnByValue=True,
                     expression="loadChatModels(true).then(() => true)")
        exc = r.get("exceptionDetails")
        assert not exc, "loadChatModels 抛异常：%s" % json.dumps(exc)[:300]
        n = wait_for(cdp, "document.querySelector('#chatModel').options.length > 1",
                     "模型清单加载完成")
        opts = cdp.eval("""Array.from(document.querySelector('#chatModel').options)
            .map(o => ({v: o.value, t: o.textContent}))""")
        print("option 数 =", len(opts))
        free = [o for o in opts if "[免费]" in o["t"]]
        paid = [o for o in opts if "[付费]" in o["t"]]
        untagged = [o for o in opts if "[免费]" not in o["t"] and "[付费]" not in o["t"]]
        print("标 [免费] =", len(free), "| 标 [付费] =", len(paid), "| 无标记 =", len(untagged))
        print("无标记的项（应只有「默认（网关路由）」这一项）:", [o["t"] for o in untagged])
        print("样例：")
        for o in (free[:2] + paid[:2]):
            print("   %-70s → %r" % (o["t"], o["v"]))
        # 值形状不能变（改了前缀会让后端 409/400）
        bad_val = [o["v"] for o in free + paid if "/" not in o["v"]]
        print("值形状异常（不含 / ）:", bad_val or "无")

        # ── 先落到一个免费模型：不弹框，且成为「拒绝后应回到的值」──
        # 原值取空值时这条断言是假绿的（'' → '' 看不出是否真的回滚）。
        f0 = free[0]
        _result["answer"] = False          # 若这条弹了框，拒绝会让它落空 ⇒ 可判别
        _result["dialog"] = None
        cdp.eval("""(() => {
            const s = document.querySelector('#chatModel');
            s.value = %s;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return s.value;
        })()""" % json.dumps(f0["v"]))
        time.sleep(0.6)
        free_sel = cdp.eval("document.querySelector('#chatModel').value")
        _result["free_asked"] = bool(_result["dialog"])
        print("\n[免费] 选 %r → value=%r 弹框=%s" % (f0["v"], free_sel, _result["free_asked"]))

        # ── 拒绝确认必须回滚到「刚才那个免费模型」──
        target = paid[0]
        orig = free_sel
        print("\n[拒绝] 原值=%r 尝试选 %r" % (orig, target["v"]))
        _result["answer"] = False
        _result["dialog"] = None
        cdp.eval("""(() => {
            const s = document.querySelector('#chatModel');
            s.value = %s;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return s.value;
        })()""" % json.dumps(target["v"]))
        time.sleep(0.8)
        after_no = cdp.eval("document.querySelector('#chatModel').value")
        print("   弹框文案 =", repr(_result["dialog"]))
        print("   拒绝后 value =", repr(after_no), "| 期望回到 =", repr(orig))
        rollback_ok = (after_no == orig and orig != "")

        # ── 接受确认必须保留并记住 ──
        print("\n[接受] 再选同一模型")
        _result["answer"] = True
        _result["dialog"] = None
        cdp.eval("""(() => {
            const s = document.querySelector('#chatModel');
            s.value = %s;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return s.value;
        })()""" % json.dumps(target["v"]))
        time.sleep(0.8)
        after_yes = cdp.eval("document.querySelector('#chatModel').value")
        # 记住的键是**模型 ID**（不是 select 的 value，后者可能带 `ccr/` 前缀）
        acked = cdp.eval("Object.keys(localStorage).filter(k => k.indexOf('hub.paidack.') === 0)")
        print("   接受后 value =", repr(after_yes), "| 记住的键 =", acked)
        accept_ok = (after_yes == target["v"] and
                     any(k == "hub.paidack." + target["v"] or
                         target["v"].endswith("/" + k[len("hub.paidack."):]) for k in acked))

        # ── 已确认过的模型不再弹 ──
        print("\n[再次选择已确认模型] 不应再弹框")
        _result["answer"] = False          # 若真弹了，拒绝会让它回滚 ⇒ 可判别
        _result["dialog"] = None
        cdp.eval("""(() => {
            const s = document.querySelector('#chatModel');
            s.value = '';
            s.dispatchEvent(new Event('change', {bubbles: true}));
            s.value = %s;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return s.value;
        })()""" % json.dumps(target["v"]))
        time.sleep(0.8)
        again = cdp.eval("document.querySelector('#chatModel').value")
        no_reprompt = (_result["dialog"] is None and again == target["v"])
        print("   二次选择后 value =", repr(again), "| 又弹框 =", bool(_result["dialog"]))

        print("\n[设置页] 切到 设置→模型，验证同一套标注也在")
        cdp.send("Runtime.evaluate", awaitPromise=True, returnByValue=True,
                 expression="go('settings-model')")
        wait_for(cdp, "typeof SET_AGENTS !== 'undefined' && SET_AGENTS.length > 0",
                 "SET_AGENTS 加载")
        cdp.send("Runtime.evaluate", awaitPromise=True, returnByValue=True,
                 expression="settingsPickAgent(SET_AGENTS[0].id)")
        wait_for(cdp, "document.querySelector('#setModelSel') && "
                      "document.querySelector('#setModelSel').options.length > 1",
                 "设置页模型下拉渲染")
        s_opts = cdp.eval("""Array.from(document.querySelector('#setModelSel').options)
            .map(o => ({v:o.value, t:o.textContent}))""")
        s_free = [o for o in s_opts if "[免费]" in o["t"]]
        s_paid = [o for o in s_opts if "[付费]" in o["t"]]
        s_unt = [o for o in s_opts if "[免费]" not in o["t"] and "[付费]" not in o["t"]]
        print("   设置页 option 数 =", len(s_opts), "| [免费] =", len(s_free),
              "| [付费] =", len(s_paid), "| 无标记 =", [o["t"] for o in s_unt])
        set_tagged = bool(s_free and s_paid)

        # 选中付费档 ⇒ hint 必须出现「付费·按量」（这是「保存后会一直生效」的唯一提示位）
        if s_paid:
            _result["answer"] = True
            cdp.eval("""(() => {
                const s = document.querySelector('#setModelSel');
                s.value = %s;
                s.dispatchEvent(new Event('change', {bubbles: true}));
                return s.value;
            })()""" % json.dumps(s_paid[0]["v"]))
            time.sleep(0.8)
            hint = cdp.eval("document.querySelector('#setModelHint').textContent")
            print("   hint 文本 =", repr((hint or "")[:80]))
            hint_ok = "付费" in (hint or "")
        else:
            hint_ok = False

        print("\n=== 判定 ===")
        checks = [
            ("每个具体模型都带计费标记", not untagged or all(o["v"] == "" for o in untagged)),
            ("value 形状未被改动", not bad_val),
            ("免费模型不弹框", not _result.get("free_asked")),
            ("拒绝确认后回滚到原值", rollback_ok),
            ("接受确认后保持选中", accept_ok),
            ("已确认模型不重复弹框", no_reprompt),
            ("设置页同样带标记", set_tagged),
            ("设置页 hint 显示付费档", hint_ok),
        ]
        for name, ok in checks:
            print("  %-24s %s" % (name, "PASS" if ok else "FAIL"))
        return 0 if all(ok for _, ok in checks) else 1
    finally:
        if cdp:
            cdp.close()
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
