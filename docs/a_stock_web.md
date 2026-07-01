# A股信维公式本地观察台

这个本地网站用于沉淀每日推荐股票，并持续追踪入池后的走势、收益、回撤和候选分项得分。它读取同一个 SQLite 数据库：

```text
D:\spacex\data\a_stock.db
```

## 启动网站

```powershell
python D:\spacex\scripts\a_stock_web.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765
```

## 晨报网页化

2026-06-21 起，A股信维公式晨报不再通过 Codex 聊天定时推送，改为本地网页首页展示。首页“信维晨报”模块读取同一套结构化数据：

- `/api/morning-report`：数据源健康、纸面回放、公式闸门、Top候选、`blocked_by_evidence`、`deep_research`、`wait_evidence`、S/A证据缺口和仓位边界。
- 没有六项S/A证据闭环时，晨报固定显示“买入池为空、仓位0%”。
- 模型分、因子分和纸面回放只作为研究优先级，不提升买入资格。

## 数据表

- `market_snapshot`：每日全市场行情快照。
- `recommendation_runs`：每日推荐运行记录。
- `recommendation_candidates`：每次运行的候选股票、总分、理由、风险标签和分项得分。
- `stock_watchlist`：长期观察池，记录首次入池日期、入池价格、首次排名和初筛理由。
- `watchlist_daily_metrics`：观察池日度事实表，记录入池后每天的收益、最大回撤、持有天数、成交额、估值和最新评分。
- `stock_research_notes`：后续产业命题、公告、研报、客户订单等证据笔记。
- `stock_xinwei_reviews`：每只观察池股票的信维公式六项验证清单，默认 `pending`，等待 S/A 级证据填充。
- `stock_evidence_items`：公告、研报等逐条证据，保留来源、等级、标题、链接和原始 JSON。
- `stock_research_coverage`：机构研报覆盖度，辅助判断预期差。
- `stock_factor_daily`：全市场日度因子表，保存趋势、流动性、资金、波动率、估值和因子质量分。
- `stock_model_scores`：研究优先级评分表，汇总市场分、证据分、因子分、入池后行为分和风险分。
- `stock_kline_daily`：观察池历史日K表，保存开高低收、量、振幅、涨跌幅和来源。
- `provider_health_checks`：数据源健康校验表，保存行情快照和K线/备用源的价格偏差、缺源状态和校验说明。
- `paper_replay_results`：日报候选纸面回放表，按每次推荐记录T+1/T+3/T+5/T+10/T+20收益、浮盈、回撤和止损触发。
- `external_tool_registry`：GitHub能力库，记录外部工具的能力层、复用决策、许可/风险边界和来源链接。
- `capability_roadmap`：平台能力路线图，记录下一步数据桥、因子、回测、沙盒执行等方向。

## 日常流程

每天更新行情并生成候选：

```powershell
python D:\spacex\scripts\a_stock_daily.py run-daily --top 80
```

如果当天行情快照已经存在，只重新生成候选：

```powershell
python D:\spacex\scripts\a_stock_daily.py --date 2026-06-17 recommend --top 80
```

只刷新观察池指标：

```powershell
python D:\spacex\scripts\a_stock_daily.py init
```

采集单票 S/A 级证据并刷新信维公式六项清单：

```powershell
python D:\spacex\scripts\a_stock_evidence.py enrich 300136 --announcements 30 --report-pages 2
```

查看单票六项验证状态：

```powershell
python D:\spacex\scripts\a_stock_evidence.py show 300136
```

刷新研究优先级模型：

```powershell
python D:\spacex\scripts\a_stock_model.py refresh
```

单独刷新因子层：

```powershell
python D:\spacex\scripts\a_stock_factors.py refresh
```

刷新观察池历史K线：

```powershell
python D:\spacex\scripts\a_stock_kline.py refresh-watchlist --scope model --limit 50 --days 120 --provider auto
python D:\spacex\scripts\a_stock_kline.py refresh-replay-missing --limit 0 --days 240
```

刷新数据源健康校验：

```powershell
python D:\spacex\scripts\a_stock_provider_health.py refresh --scope model --limit 50
```

刷新日报候选纸面回放：

```powershell
python D:\spacex\scripts\a_stock_replay.py refresh
```

查看当前模型排名：

```powershell
python D:\spacex\scripts\a_stock_model.py show --top 20
```

## 页面功能

- 首页 KPI：最新数据日、观察池数量、能力库数量、因子层覆盖、深度研究数量、等证据数量、最新候选数量、模型 Top、平均收益、最佳收益、最大回撤。
- 观察池表：优先级、代码、名称、研究队列、首次入池日、入池价、最新价、收益、回撤、模型分、因子分、因子质量、证据分、风险分、市场分、行业、PE、PB。
- 单票走势：按入池后价格序列绘制趋势线，并显示研究优先级、因子层拆分、候选历史和风险标签。
- 单票证据：显示 S/A 级证据数量、最新证据、机构覆盖和信维公式六项验证状态。
- 最新日报候选：展示最近几次推荐运行及头部候选。

页面里的“因子分”是质量加权后的模型因子分；原始因子分、样本天数和分项拆解保存在 `stock_factor_daily` 与模型详情 JSON 中。

2026-06-18更新：

- KPI新增“历史K线库”，展示K线行数、覆盖股票数和最新K线日期。
- KPI新增“源校验”，展示观察池模型队列的行情源交叉通过数、校验日和缺K线数量。
- KPI新增“纸面回放”，展示已形成次日开盘观察口径的候选数、总候选数、最新回放日和平均最新收益。
- KPI中的“因子层”显示 `stock-factor-v0.3-indicators` 与技术分均值。
- 单票详情的因子说明新增技术分、RSI和ATR。
- 最新日报候选条目显示T+1纸面收益，便于复盘候选质量。
- GitHub能力库完整展示38个项目和9项路线图。
- 当前站点已验证：观察池162只，K线库37,855条，源校验49/50，纸面回放417/417，最新观察池因子层162条，工具区38项。
- KPI新增“买入资格”，展示真正通过信维六要素闸门的股票数量；当前为0，待人工核验10，缺证据152。
- 休市日纪律：`a_stock_daily.py` 已内置2026年A股休市日历。休市日会记录 `market-calendar/skipped`，并跳过全市场快照和候选生成。

## 投研边界

脚本推荐只是第一层市场筛选。真正进入“可以买入”前，仍必须按 `D:\spacex\system_prompt.md` 的信维公式补齐六项验证：

1. 产业拐点
2. 稀缺卡位
3. 龙头客户绑定
4. 产能/订单扩张
5. 业绩拐点确认
6. 巨大预期差

观察池负责记录“当时为什么被选中”和“之后市场怎么验证”，不是替代产业研究。

`needs_review` 只表示数据库已抓到相关线索，仍需要人工确认订单性质、客户真实性、扣非利润质量和产业拐点位置；它不是买入结论。

`deep_research` 只表示“市场信号 + 证据线索”已足够进入人工深挖；`wait_evidence` 表示市场分较高但信维证据不足。模型分用于排序研究精力，不用于直接下单。

单票详情中的“公式闸门”是买入资格边界：只要还有 `pending` 或 `needs_review` 维度，页面会明确显示“买入资格=否”。
## 2026-06-20 v0.5 Website/API Update

本地网站已接入结构化信维证据闸门：

- 首页新增“公式闸门工作台”，包含股票 × 六项维度矩阵。
- 新增 `/api/gate-matrix`，返回最新 `xinwei_gate_snapshots` 及每只股票的六项状态。
- 新增 `/api/research-tasks`，返回 `research_tasks` 中仍然 open 的待核验任务。
- `/api/stock?code=xxxxxx` 新增 `xinwei_gate_snapshot` 和 `evidence_links`，个股详情可以看到每个维度对应的证据链。
- `/api/summary` 新增 `formula_supported_count`、`blocked_by_evidence_count` 和结构化闸门统计。

当前本地服务：

```text
http://127.0.0.1:8765
```

验证项：

- `/api/summary` 显示 `blocked_by_evidence_count=10`、`formula_gate_eligible_count=0`。
- `/api/gate-matrix?limit=5` 返回 5 行矩阵数据。
- `/api/research-tasks?limit=5` 返回待核验任务。
- 首页脚本已通过语法解析检查。
