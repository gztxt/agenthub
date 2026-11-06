/** 资源监控页（v0.13.52）——列出运行中 Agent 的进程资源，支持 Kill。
 *
 *  v0.13.54（2026-09-29 报障「资源页面还是无法加载」的真身）：删掉原第 5 行
 *  `if (resLoaded && !force) return;`。go()（01 分片）写的是
 *  `if (page === 'resources' && !resLoaded) { resLoaded = true; loadResources(); }`
 *  —— **先置位、后调用**，所以首次进页这道内部闸门必然命中，函数直接空返回：
 *  既不发请求也不写 hint，页面就永远停在「加载中…」，而且**控制台零报错**
 *  （没抛异常，什么都没发生）。probe 实测量到的正是：page_on=true、
 *  resLoaded=true、cards=0、hint=""。
 *  同型的 lpLoaded / ghLoaded 两个页面没炸，是因为 loadLocalProjects /
 *  loadGithubRepos 内部**没有**这道闸门 —— 「进页只由 go() 一处把关」
 *  是本仓既定纪律，资源页是唯一一个在加载函数里又关了一道的。 */
var resLoaded = false;

async function loadResources(force = false) {
    const listEl = document.getElementById("resList");
    const hintEl = document.getElementById("resHint");
    const summaryEl = document.getElementById("resSummary");
    if (!listEl) return;

    hintEl && (hintEl.textContent = "采样中…");
    listEl.innerHTML = '<div class="hint" style="padding:10px">采样中…</div>';

    try {
        const resp = await api("/api/resources");
        if (!resp.ok) throw new Error(resp.error || "请求失败");
        renderResources(resp);
        hintEl && (hintEl.textContent = "共 " + resp.total_agents + " 个 Agent · 总 CPU " + resp.total_cpu + "% · 总内存 " + resp.total_rss_mb + " MB");
        if (summaryEl) summaryEl.textContent = "总计：" + resp.total_agents + " 个 Agent · CPU " + resp.total_cpu + "% · 内存 " + resp.total_rss_mb + " MB";
        resLoaded = true;
    } catch (e) {
        console.error("[Resources] load failed:", e);
        hintEl && (hintEl.textContent = "加载失败");
        /* P1-24：此前直吐 e.message 进 innerHTML —— 后端 message 里带 &<>"' 就破版，
           且与全站其它页的 boxFail 口径不一致（少了 escapeHtml 与「重试」按钮）。
           统一走 boxFail：同一个渲染 + 同一个转义 + 同一条重试路径。 */
        /* 重试入口用显式函数名：onclick="loadResources()" 会把事件对象当 force 传进去
           （隐式 truthy），重试语义恰好也该强制刷新，但不该靠这个巧合成立。 */
        boxFail("resList", e, "resourcesRetry");
    }
}

/** 资源页「重试」按钮的显式入口（不依赖 onclick 传参的隐式 truthy）。 */
function resourcesRetry() { loadResources(true); }

function renderResources(data) {
    const listEl = document.getElementById("resList");
    if (!listEl) return;

    if (!data.agents || data.agents.length === 0) {
        listEl.innerHTML = '<div class="hint" style="padding:20px;text-align:center">当前没有运行中的 Agent 进程</div>';
        return;
    }

    let html = "";
    for (const agent of data.agents) {
        const agentId = agent.agent_id;
        const agentName = agent.agent_name;
        const kind = agent.kind;
        const summary = agent.summary;
        const processes = agent.processes;

        // 根据 kind 给不同颜色标记
        const kindBadge = {
            agent: '<span class="s-badge running"></span>',
            gateway: '<span class="s-badge" style="background:var(--accent);color:var(--on-accent)">网关</span>',
            // 三个色值全走 :root 实名 token。此前 service 用 var(--primary)、
            // tool/memory 写字面色，而这三个变量在 :root 里**定义数为 0**
            // ⇒ var() 解析失败回退到初始值（透明），服务徽章看起来「没上色」。
            // 语义映射：service/工具/记忆都是「非交互的分类标识」，
            // 用灰阶 --text-2 / --muted 表达层级差，不与 --accent（可点击主色）抢语义。
            service: '<span class="s-badge" style="background:var(--text-2);color:var(--on-accent)">服务</span>',
            tool: '<span class="s-badge" style="background:var(--muted);color:var(--on-accent)">工具</span>',
            memory: '<span class="s-badge" style="background:var(--accent);color:var(--on-accent)">记忆</span>'
        }[kind] || '<span class="s-badge"></span>';

        /* P2-D：agent_id / pid 一律 escapeHtml + data-*，不再拼进 inline onclick。
           两层理由（任一层单独成立就该改）：
           ① 纪律层——inline onclick 旁路事件委托（本批 P1-17 已把 09/10 两个页面
              改成 data-* 走委托；inline 会跳过「点完收场」逻辑）。
           ② 纵深层——id 直接进属性字符串，一个含引号的 id 就能破出属性、加第二个
              onclick。实测不可利用（agent_id 来自画像白名单、后端输出经净化），
              但「不可利用」是后端当前的性质，不是前端的保证：纵深该在前端补，
              否则哪天画像来源放宽（自定义 Agent 名 / 扫到奇怪进程名）就是真漏洞。 */
        html +=
        '<div class="agent-card" data-agent="' + escapeHtml(agentId) + '" style="border:1px solid var(--divider);border-radius:8px;margin-bottom:8px;background:var(--bg);overflow:hidden">' +
            '<div class="agent-header" data-toggle="1" data-agent="' + escapeHtml(agentId) + '" style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--surface-2);cursor:pointer;border-bottom:1px solid var(--divider)">' +
                kindBadge +
                '<span class="agent-name" style="flex:1;font-weight:500">' + escapeHtml(agentName) + '</span>' +
                '<span class="agent-meta" style="font-size:12px;color:var(--text-2)">' +
                    'CPU <b>' + summary.cpu_percent + '%</b> · 内存 <b>' + summary.rss_mb + ' MB</b> · <b>' + summary.count + '</b> 进程' +
                '</span>' +
                '<svg class="i xs chevron" aria-hidden="true" style="transition:transform .15s;flex:none"><use href="#i-chevron-down"/></svg>' +
            '</div>' +
            '<div class="agent-procs" id="procs-' + agentId + '" style="display:none;padding:8px 12px;max-height:300px;overflow-y:auto">' +
                '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
                    '<thead>' +
                        '<tr style="position:sticky;top:0;background:var(--surface-2);z-index:1">' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">PID</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">CPU%</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">内存</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">命令行</th>' +
                            '<th style="text-align:center;padding:4px 8px;border-bottom:1px solid var(--divider);width:80px">操作</th>' +
                        '</tr>' +
                    '</thead>' +
                    '<tbody>';

        for (const proc of processes) {
            html +=
                        '<tr data-pid="' + escapeHtml(proc.pid) + '">' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.pid + '</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.cpu_percent + '%</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.rss_mb + ' MB</td>' +
                            '<td class="res-cmd" style="padding:4px 8px;border-bottom:1px solid var(--divider);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(proc.cmdline) + '">' + escapeHtml(proc.cmdline) + '</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider);text-align:center">' +
                                '<button class="btn sm danger" data-kill="SIGTERM" data-agent="' + escapeHtml(agentId) + '" data-pid="' + escapeHtml(proc.pid) + '" title="优雅结束 (SIGTERM)">结束</button>' +
                                '<button class="btn sm danger" style="margin-left:4px" data-kill="SIGKILL" data-agent="' + escapeHtml(agentId) + '" data-pid="' + escapeHtml(proc.pid) + '" title="强制结束 (SIGKILL)">强杀</button>' +
                            '</td>' +
                        '</tr>';
        }

        html +=
                    '</tbody>' +
                '</table>' +
            '</div>' +
        '</div>';
    }
    listEl.innerHTML = html;
    bindResourceActions(listEl);
}

/** P2-D：卡片展开 / 结束 / 强杀三条动作的**唯一出口**（事件委托）。
 *
 *  为什么不是 inline onclick：
 *   - inline 属性里的 JS 字符串要求 id 必须是「安全的 JS 字面量」，任何引号都要
 *     转义层级，转义错了就是 XSS；data-* 只是属性值，escapeHtml 一层就够。
 *   - 委托是本批 P1-17 定的纪律（inline 旁路收场逻辑），三处动作保持同一出口。
 *  bind 幂等：容器上打标记，重渲染（采样轮询）不会重复绑。 */
function bindResourceActions(listEl) {
    if (!listEl || listEl.dataset.resBound === "1") return;
    listEl.dataset.resBound = "1";
    listEl.addEventListener("click", function (e) {
        const killBtn = e.target.closest("[data-kill]");
        if (killBtn) {
            /* stopPropagation 收拢到委托这一处统一做：kill 按钮在可展开的卡片头语义
               之外，必须不冒泡到头部的展开动作，否则点「结束」会顺手把卡片展开。 */
            e.stopPropagation();
            killProc(killBtn.dataset.agent, parseInt(killBtn.dataset.pid, 10),
                     killBtn.dataset.kill);
            return;
        }
        const head = e.target.closest(".agent-header[data-toggle]");
        if (head) toggleAgentProcs(head.dataset.agent);
    });
}

function toggleAgentProcs(agentId) {
    const procEl = document.getElementById("procs-" + agentId);
    const chevron = document.querySelector('[data-agent="' + agentId + '"] .chevron');
    if (!procEl) return;
    const isHidden = procEl.style.display === "none";
    procEl.style.display = isHidden ? "block" : "none";
    if (chevron) chevron.style.transform = isHidden ? "rotate(180deg)" : "";
}

async function killProc(agentId, pid, signal) {
    if (!confirm("确定要 " + (signal === "SIGTERM" ? "结束" : "强制结束") + " Agent「" + agentId + "」的进程 PID " + pid + " 吗？此操作不可逆。")) return;

    const rowEl = document.querySelector('#procs-' + agentId + ' tr[data-pid="' + pid + '"]');
    const killBtns = rowEl ? rowEl.querySelectorAll('button') : [];
    killBtns.forEach(b => { b.disabled = true; b.style.opacity = '0.5'; });

    try {
        const resp = await api("/api/resources/kill", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_id: agentId, pid: pid, signal_name: signal })
        });

        if (!resp.ok) {
            const failedPids = resp.failed || [pid];
            const killedPids = resp.killed || [];
            let msg = "";
            if (killedPids.length && failedPids.length) {
                msg = "部分成功：PID " + killedPids.join(',') + " 已结束；PID " + failedPids.join(',') + " 失败";
            } else if (failedPids.length) {
                msg = "失败：PID " + failedPids.join(',') + " 未能结束";
            } else {
                msg = resp.error || "Kill 失败";
            }
            throw new Error(msg);
        }

        toast("PID " + pid + " " + (signal === "SIGTERM" ? "已结束" : "已强杀"), "ok");

        // 实时移除该进程行
        if (rowEl) {
            rowEl.style.transition = "opacity 0.2s, height 0.2s";
            rowEl.style.opacity = "0";
            rowEl.style.height = "0";
            setTimeout(() => rowEl.remove(), 200);
        }

        // 更新 Agent 汇总信息
        updateAgentSummary(agentId, -1);

        // 若该 Agent 已无进程，移除整张卡片
        checkAndRemoveEmptyAgent(agentId);

    } catch (e) {
        console.error("[Resources] kill failed:", e);
        killBtns.forEach(b => { b.disabled = false; b.style.opacity = ''; });
        toast("操作失败：" + e.message, "err");
    }
}

function updateAgentSummary(agentId, deltaCount) {
    const cardEl = document.querySelector('[data-agent="' + agentId + '"]');
    if (!cardEl) return;
    const metaEl = cardEl.querySelector('.agent-meta');
    if (!metaEl) return;
    /* P1-24：此前读的是 textContent（纯文本，形如 "2 进程 · 120MB"），
       却拿它去跑 /<b>(\d+)<\/b>/ 这种**只可能匹配 innerHTML** 的正则 ——
       永远匹配不上，计数更新是死代码；即便某次碰巧匹配上，把 textContent
       的结果塞回 innerHTML 也会把实体（&<>）重新解释成标签。
       改成对纯文本做正则，输出也走 textContent：不碰解析器就没有二次解释。 */
    const text = metaEl.textContent || '';
    const m = text.match(/(\d+)\s*进程/);
    if (m) {
        const newCount = Math.max(0, parseInt(m[1], 10) + deltaCount);
        metaEl.textContent = text.replace(/\d+\s*进程/, newCount + ' 进程');
    }
}

function checkAndRemoveEmptyAgent(agentId) {
    const procsEl = document.getElementById('procs-' + agentId);
    if (!procsEl) return;
    const rows = procsEl.querySelectorAll('tbody tr');
    if (rows.length === 0) {
        const cardEl = document.querySelector('[data-agent="' + agentId + '"]');
        if (cardEl) {
            cardEl.style.transition = "opacity 0.2s, height 0.2s, margin 0.2s";
            cardEl.style.opacity = "0";
            cardEl.style.height = "0";
            cardEl.style.margin = "0";
            setTimeout(() => {
                cardEl.remove();
                checkEmptyList();
            }, 200);
        }
    }
}

function checkEmptyList() {
    const listEl = document.getElementById("resList");
    if (!listEl) return;
    const cards = listEl.querySelectorAll('.agent-card');
    if (cards.length === 0) {
        listEl.innerHTML = '<div class="hint" style="padding:20px;text-align:center">当前没有运行中的 Agent 进程</div>';
    }
}

/* P2-D：此处原有的 escapeHtml **副本**已删除，统一用 01-core-boot.js 的那份。
   两个理由：
     ① 同一语义两份实现＝迟早漂（副本里转义表用 \u0026 写码点、正本用字面量，
        读的人得逐个解码才知道它们等价）；本仓已因「异常判据四处各判各的」吃过
        一次同型亏（P1-20）。
     ② 副本有真 bug：`(s || "")` 对 0 / false 会返回空串——pid=0、计数 0
        都会被渲染成空白。正本用 `String(s == null ? '' : s)`，无此问题。 */

// 供外部调用（如从其它页面跳转）
window.loadResources = loadResources;
/* ── P1-16（2026-09-30）：资源页窄屏档 ─────────────────────────────────
   症状：命令行列写死 max-width:400px，加 PID/CPU/RSS/操作四列后在 390px 视口
   必然横向溢出（手机上表现为整页左右拖、右侧「结束/强杀」按钮点不到）。
   修法：宽度交给 CSS 表格布局按视口分配。min-width:0 是关键 —— 表格单元格默认
   min-width:auto，内容多宽就撑多宽，text-overflow 永远不生效。
   断点 767px 与 01-core-boot.js 的 HUB_NARROW_MQ / templates/index.html 的
   @media(max-width:767px) 同源同值（分档偏好不变量第 3 条：断点只允许一处定义）。
   注意：本文件是 JS 分片，裸 CSS 文本不会被解析，必须 insertRule 真注入。 */
(function () {
    var CSS = [
        ".res-cmd { max-width: 400px; }",
        "@media (max-width: 767px) {",
        "  .res-cmd { max-width: none; }",
        "  #resList .agent-card table { table-layout: fixed; width: 100%; }",
        "  #resList .agent-card td, #resList .agent-card th { padding: 4px 6px; }",
        "  #resList .agent-card td.res-cmd { word-break: break-all; white-space: normal; }",
        "}"
    ].join("");
    if (typeof CSSStyleSheet === "undefined" || !CSSStyleSheet.prototype.insertRule) return;
    var sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    document.adoptedStyleSheets = document.adoptedStyleSheets.concat([sheet]);
})();
