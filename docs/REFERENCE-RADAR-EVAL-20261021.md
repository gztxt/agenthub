# 参考情报检索评测 · 基线（P2 ①，2026-10-21）

> 目的：把「top5 相关率」从**单评审结论**变成**可复现的数值**，此后每次改检索式/权重
> 都能回答「有没有变好、好多少」。基线本身**不是**上线门槛，是后续对照点。

## 1. 怎么测

```
venv/bin/python tests/eval_refscout.py --json work/eval.json
```

- 标注集：`tests/refscout_eval/annotations.json`（5 模块 / 13 个目标仓，version 1）。
- 标注规则：只收**能一句话说清解决同一问题**的仓库，`why` 直接引其官方 description；
  13 条在 2026-10-21 逐条打过 `/repos/{owner}/{repo}` 核验存在与描述。
- 隔离：`DATA_DIR` 指向 `work/refscout-eval-*` 临时目录，**冷缓存**（否则测的是缓存命中率），
  跑完即删；不碰生产库、不写 `ref_cards`。
- 默认 **不跑重排**（`do_rerank=False`）：词法基线可重复、零 LLM 成本。要评重排加 `--rerank`。
- 指标：
  - `hit@5`（主）＝ top5 命中的标注仓 / 该模块标注仓总数 ⇒ 衡量**找不找得到**；
  - `mrr` ＝ 第一个命中仓名次倒数 ⇒ 衡量**排得够不够前**；
  - `prec@5`（**只作下界**）＝ top5 中标注仓占比。**未标注 ≠ 不相关**，
    这个数必然偏悲观，报告里一律标「下界」。

## 2. 基线结果（2026-10-21，112~116s／5 模块，0 次 403/429）

| 模块 | 池 | hit@5 | mrr | 命中 | 未命中的标注仓（真实名次） |
|---|---:|---:|---:|---|---|
| 终端 WebSocket 与端口转发 | 37 | 0.667 | 0.333 | termpair(#3) / gotty(#5) | `tsl0922/ttyd`（池内 #8） |
| LLM 多模型网关与故障转移 | 41 | 0.000 | 0.000 | — | `Portkey-AI/gateway`(#8)、`BerriAI/litellm`(#13) |
| 服务健康探针与自动看护 | 36 | 0.500 | 1.000 | gatus(#1) | `louislam/uptime-kuma`（**未入池**） |
| 定时任务调度（cron 表达式） | 34 | 0.333 | 0.500 | apscheduler(#2) | `robfig/cron`、`celery/celery`（**均未入池**） |
| 自建轻量全文检索服务 | 42 | 0.000 | 0.000 | — | `meilisearch`、`typesense`、`quickwit`（**均未入池**） |
| **汇总** | — | **0.300** | **0.367** | 4/13 | — |

两次独立冷跑（不同临时缓存目录）汇总均为 `hit@5=0.300 / mrr=0.367 / 4/13`，
仅终端模块第 1、2 名互换 ⇒ **词法模式可重复**，差异不是随机抖动。

## 3. 两类失败必须分开处置（评测的主要产出）

`target_ranks` 已把「命中但靠后」与「根本没进候选池」分开，两者修法完全不同：

| 类型 | 数量 | 例子 | 该改什么 |
|---|---:|---|---|
| **排序**（入池但出 top5） | 3 | ttyd #8、Portkey #8、litellm #13 | 调权重：当前 `0.70*cov + 0.20*log(stars) + 0.10*lang + name_bonus`，**覆盖度占比过高**，关键词堆砌的小仓压过权威实现 |
| **召回**（未入池） | 6 | uptime-kuma、robfig/cron、celery、meilisearch、typesense、quickwit | 改检索式：多词 AND 要求**每个词都出现**，而 `robfig/cron` 的描述是「a cron library for go」、meilisearch 是「lightning-fast search engine API」——都缺 `scheduler`/`full-text` |

反例证据（同轮）：LLM 网关模块 top5 全是 `maximhq/bifrost`、`lidge-jun/opencodex`、
`ENH...GoModel`、`9router-go` 这类描述里把 gateway/proxy/failover/router 全塞满的小仓，
而 61k 星的 litellm 在 #13 —— **词面全覆盖 ≠ 功能最接近**，这正是 P0 已经踩过一次的坑的另一种形态。

## 4. 这组数不覆盖什么

1. **未跑重排**：上线默认 `allow_paid=false`，免费档方差大（40~82s）；重排对 hit@5 的影响未知，
   需要单独预算与单独报告。
2. **未标注 ≠ 不相关**：`prec@5=0.16` 只说明「top5 里确定相关的占 16%」，
   `xerj-org/xerj`、`manticoresearch` 这类未标注项未必不相关。
3. **单语料时点**：GitHub 搜索结果随时间变化，本基线绑定 2026-10-21；跨日对比前先重跑确认
   环境未变（同缓存目录二次跑可区分「代码改动」与「上游变化」）。

## 5. 下一步（有基线才敢做的两件事）

1. **排序**：降 `cov` 权重、抬 stars 先验（建议试 `0.55 / 0.30 / 0.10` 与 `0.50 / 0.35 / 0.10` 两档），
   以 `hit@5` 与 `mrr` 同时不降为准；防止再次被关键词堆砌仓霸榜。
2. **召回**：`core` 臂从「全部词元 AND」改为「至少 2 个词元 + 至少 1 个来自模块名」，
   并对 4 词以上查询自动降级为 2 词组合；目标是让 `robfig/cron`、`meilisearch` 这类
   **描述短但精准**的库能进池。

两项都改完后用同一标注集复跑，`hit@5` 应从 0.300 上升；若上升但 `prec@5` 下降，
说明只是把噪声一起捞进来了，需回退。
