# A 股周度复盘与推荐逻辑迭代

## 目标

周度复盘用于回看上一周进入推荐闭环的股票，比较推荐时的证据、模型分桶、信维公式闸门与后续纸面走势，形成可追踪的逻辑改进项。

定位是“研究质量与推荐逻辑改进”，不是自动交易系统。量化收益、胜率、回撤和赔率只作为参考指标，不直接升级为买入触发条件。

## 运行命令

生成上一自然周复盘：

```powershell
python scripts\a_stock_weekly_review.py run --report
```

按指定日期推导上一自然周：

```powershell
python scripts\a_stock_weekly_review.py run --as-of 2026-06-23 --report
```

查看最近一次复盘：

```powershell
python scripts\a_stock_weekly_review.py show
```

输出 JSON：

```powershell
python scripts\a_stock_weekly_review.py show --json
```

本地网站接口：

```text
http://127.0.0.1:8765/api/weekly-review
```

首页总览接口 `/api/summary` 会附带最近一次周度复盘摘要字段。

## 数据表

周度复盘写入以下本地表：

| 表 | 用途 |
| --- | --- |
| `weekly_review_runs` | 每次周度复盘的总体结果、覆盖率、胜率、平均收益和告警摘要 |
| `weekly_review_stocks` | 个股级复盘，保留推荐批次、模型分桶、信维闸门、纸面收益、回撤与教训标签 |
| `weekly_logic_insights` | 面向后续推荐逻辑的改进项，按严重程度和优先级排序 |

复盘依赖已有本地证据链：

| 表 | 用途 |
| --- | --- |
| `recommendation_runs` / `recommendation_candidates` | 上周推荐候选来源 |
| `paper_replay_results` | 推荐后纸面走势、收益、回撤、止盈止损触发 |
| `stock_model_scores` | 推荐时或接近日期的模型优先级与 action bucket |
| `xinwei_gate_snapshots` | 信维公式闸门状态与证据阻塞原因 |

## 核心评估口径

复盘会统计：

| 指标 | 含义 |
| --- | --- |
| `candidate_count` | 上周进入候选闭环的去重股票数量 |
| `candidate_event_count` | 上周所有推荐候选事件数量，同一股票多次出现会重复计数 |
| `replay_coverage_rate` | 有纸面回放结果的覆盖率 |
| `avg_latest_return_pct` | 最近回放收益均值 |
| `median_latest_return_pct` | 最近回放收益中位数 |
| `positive_rate` | 纸面收益为正的比例 |
| `stop_loss_5_rate` | 5% 止损触发比例 |
| `take_profit_5_rate` | 5% 止盈触发比例 |
| `avg_max_drawdown_pct` | 平均最大回撤 |

系统会按 action bucket、行业和个股生成复盘视角，重点识别：

| 教训标签 | 含义 |
| --- | --- |
| `data_gap` | 推荐后缺少足够回放数据，优先补数据 |
| `market_right_evidence_weak` | 走势验证较好，但 S/A 级证据不足，只能推动补证据，不能直接买入 |
| `validated_momentum` | 证据闸门通过且纸面走势较好，可纳入深度跟踪 |
| `thin_evidence_drawdown` | 证据不足且后续回撤，说明需降低市场热度权重 |
| `risk_control_needed` | 回撤较大，应强化止损、仓位或不得追高规则 |
| `wait_evidence_underperformed` | 等证据标的表现弱，保留观察但降低优先级 |

## 告警与治理

当前 MVP 采用以下默认阈值：

| 条件 | 动作 |
| --- | --- |
| 回放覆盖率低于 90% | 生成数据补齐告警 |
| 5% 止损触发率超过 25% | 生成风险过滤告警 |
| 弱证据标的平均收益低于 -3% | 生成证据权重告警 |
| 弱证据标的平均收益超过 5% | 生成补证据机会清单，但不自动升级买入 |

问题项超过 7 天未处理时，应在人工周报中升级为“需人工核验”。

## 与每日评测组的关系

每日 QA 会检查 `/api/weekly-review` 是否可访问，确保周度复盘结果能在网站侧稳定展示。

周度复盘建议节奏：

| 频率 | 内容 |
| --- | --- |
| 每日 | QA 检查接口可用性和数据链路健康 |
| 每周 | 运行 `a_stock_weekly_review.py run --report` 生成完整复盘 |
| 每月 | 汇总教训标签，调整模型权重或证据采集优先级 |

## 使用原则

- 不把 `needs_review` 当作通过。
- 不把纯市场分或短期涨幅当作买入理由。
- 不用复盘收益反向证明某只股票必然正确。
- 证据不足但走势较好的股票，进入“补证据/观察”，不直接进入明确买入。
- 证据充分但回撤扩大的股票，优先检查产业命题是否失效、催化是否落空、风险项是否低估。
