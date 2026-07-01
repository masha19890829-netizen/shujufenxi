# A 股本地数据底座

本工作区已经接入 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) 的数据源思想和接口纪律，并落地为一个本地 SQLite 数据库。

## 已落地内容

- 数据库路径：`D:\spacex\data\a_stock.db`
- 主脚本：`D:\spacex\scripts\a_stock_daily.py`
- 模型评分脚本：`D:\spacex\scripts\a_stock_model.py`
- 外部 Skill 源码：`D:\spacex\vendor\a-stock-data\SKILL.md`
- 第一版数据源：东财全市场行情快照，用于每日候选池种子表
- 第一版运行依赖：推荐安装 `requests`；如果未安装，脚本会退回 Python 标准库请求方式
- 第一版策略：规则打分，输出“买入候选/观察”，不是收益承诺

## 为什么先用这个架构

`a-stock-data` 不是现成历史数据仓库，而是一个 A 股数据访问 Skill。它的核心价值是：

- 数据源优先级：行情、估值优先用通达信/腾讯，东财只用于独有数据。
- 风控纪律：东财请求必须串行、限流、带正常 UA 和 Referer。
- 数据范围：行情、研报、题材、北向、龙虎榜、解禁、两融、大宗交易、股东户数、分红、新闻、公告、基础财务。

本地第一版先把全市场快照和候选结果存起来，后续再逐步加 K 线、公告、龙虎榜、研报和资金流。

## 表结构

- `data_sources`：已接入或计划接入的数据源说明。
- `market_snapshot`：每日全市场行情快照，主键为 `trade_date + code`。
- `recommendation_runs`：每次候选生成记录。
- `recommendation_candidates`：每次候选结果、得分、理由、风险标签和关键指标。
- `ingestion_log`：数据更新日志。

## 使用命令

初始化数据库：

```powershell
python D:\spacex\scripts\a_stock_daily.py init
```

安装轻量请求依赖：

```powershell
python -m pip install requests
```

抓取当天全市场快照：

```powershell
python D:\spacex\scripts\a_stock_daily.py update
```

基于已保存快照生成候选：

```powershell
python D:\spacex\scripts\a_stock_daily.py recommend --top 20
```

每天一键更新并生成候选：

```powershell
python D:\spacex\scripts\a_stock_daily.py run-daily --top 20
```

查看最近一次候选：

```powershell
python D:\spacex\scripts\a_stock_daily.py show-latest --top 20
```

## 第一版筛选逻辑

注意：第一版脚本只是“市场候选池初筛”。真正进入推荐前，必须继续按 `D:\spacex\system_prompt.md` 的“信维公式”做产业命题、客户绑定、订单产能、扣非利润和预期差验证。

候选需要先通过基础过滤：

- 排除 ST、退市风险名称。
- 成交额不低于 3 亿。
- 流通市值不低于 20 亿。
- 当日涨幅在 0.8% 到 8.8% 之间，避免过弱和明显追高。
- 主力净流入为正。
- PE(TTM) 为正。

得分拆成六类：

- 流动性：成交额越充足越好。
- 趋势：温和上涨优于涨停附近追高。
- 资金：主力净流入占成交额比例越高越好。
- 换手：1.5% 到 10% 之间更健康。
- 量比：放量但不过热。
- 估值和规模：PE、PB、流通市值适中更优。

输出中的风险标签包括追高、PB 偏高、换手过高、成交额不足、PE 缺失或为负等。

## 后续增强路线

1. 加入腾讯实时估值校验，减少单一东财字段偏差。
2. 加入 K 线和技术指标表：MA20、MA60、RSI、MACD、近 20 日波动率。
3. 加入题材热度表：同花顺热点、行业轮动、概念归属。
4. 加入事件风险表：公告、解禁、龙虎榜、大宗交易、股东户数。
5. 加入研报和一致预期：机构覆盖数、EPS 预期、评级变化。
6. 把策略拆成稳健型、趋势型、事件型三个画像。

## 风险边界

这个数据库用于形成研究候选清单，不构成个性化投资建议。A 股有涨跌停、流动性、停牌、公告黑天鹅和政策风险；实盘前仍需要结合仓位、止损、交易计划和个人风险承受能力。

## 2026-06-17 迭代：观察池与本地网站

本地库已经增加长期跟踪层：

- `stock_watchlist`：保存每只被推荐/观察股票的首次入池日期、入池价格、首次排名、首次评分和初筛理由。
- `watchlist_daily_metrics`：保存观察池股票每天的入池后收益、最大回撤、持有天数、最新评分和行情估值指标。
- `stock_research_notes`：预留给产业命题、公告、研报、客户订单等证据记录。
- `stock_xinwei_reviews`：为每只观察池股票自动建立信维公式六项验证清单，默认待验证，避免把市场初筛误当成产业确认。
- `stock_evidence_items`：保存公告、研报等逐条证据，按 S/A/B/C 分级并保留原始 JSON。
- `stock_research_coverage`：保存机构研报覆盖度，用于辅助判断“预期差”。
- `stock_factor_daily`：保存全市场日度因子层，包含均线、动量、波动率、20 日回撤、资金流/成交额比例、流动性分、趋势分、资金分、估值分、因子质量分。
- `provider_health_checks`：保存行情快照与K线/后续备用行情源的交叉校验结果，记录缺源、盘中价落区间、收盘价偏差和失败原因。
- `paper_replay_results`：保存每次日报候选在推荐后第1/3/5/10/20个交易日的纸面回放表现、最大浮盈、最大回撤和止盈/止损触发标记。
- `stock_model_scores`：把市场初筛、S/A 证据、因子层、入池后表现、风险扣分合成研究优先级，并分成 `deep_research`、`wait_evidence`、`track`、`risk_watch`、`archive_watch` 五类。
- `external_tool_registry`：保存 GitHub 外部工具的能力层、复用决策、许可证/风险说明和来源链接。
- `capability_roadmap`：保存本地平台的下一步能力路线图，例如行情源交叉校验、因子表、纸面回测、多 Agent 审稿。

本地观察台脚本：

```powershell
python D:\spacex\scripts\a_stock_web.py --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765` 即可查看每日推荐、观察池走势和策略复盘数据。

证据采集示例：

```powershell
python D:\spacex\scripts\a_stock_evidence.py enrich 300136 --announcements 30 --report-pages 2
```

采集到的线索只会把信维公式维度更新为 `needs_review`，不会自动判定“通过”。真正可买入前仍要人工核验 S/A 级证据是否严格满足六项公式。

刷新全市场因子层：

```powershell
python D:\spacex\scripts\a_stock_factors.py refresh
```

查看当前因子 Top：

```powershell
python D:\spacex\scripts\a_stock_factors.py show --top 20
```

刷新研究优先级模型：

```powershell
python D:\spacex\scripts\a_stock_model.py refresh
```

查看当前排名：

```powershell
python D:\spacex\scripts\a_stock_model.py show --top 20
```

当前模型版本 `xinwei-research-priority-v0.4-formula-gate` 的权重为：市场分 30%、证据分 30%、因子分 15%、入池后行为 10%、风险分 15%。模型里的因子分是质量加权分：当历史样本短时会向 50 分中性收敛，避免两三天行情把股票误抬成强信号。其中 `wait_evidence` 代表“市场信号强但信维证据不足”，`deep_research` 代表“值得人工深挖”，二者都不是自动买入结论。

`score_json.formula_gate` 是独立买入闸门：只有六个信维维度全部为 `supported/verified/pass`，且没有 `pending/needs_review/failed`，`eligible_for_buy` 才会变为 `true`。否则即便模型分很高，也只能输出“待验证/深挖”。

## 2026-06-18 迭代：GitHub 能力库

新增维护脚本：

```powershell
python D:\spacex\scripts\a_stock_tool_registry.py refresh
python D:\spacex\scripts\a_stock_tool_registry.py show --limit 20
```

调研清单见：

```powershell
D:\spacex\docs\github_a_stock_tool_survey.md
```

本轮纳入能力库的重点项目包括 `mootdx/mootdx`、`akfamily/akshare`、`waditu/tushare`、`microsoft/qlib`、`ricequant/rqalpha`、`vnpy/vnpy`、`zvtvz/zvt`、`TauricResearch/TradingAgents`、`AI4Finance-Foundation/FinRL-Trading` 等。

第二轮补充纳入 `waditu/czsc`、`jealous/stockstats`、`bukosabino/ta`、`mementum/backtrader`、`kernc/backtesting.py`、`polakowo/vectorbt`。这些项目主要增强因子公式、技术指标、策略复盘和参数实验能力，均不直接替代信维公式。

第三轮补充纳入：

- A股纸面回测：`zsrl/qka`
- MCP/数据桥：`HuggingAGI/mcp-baostock-server`、`openstockdata/stock-data-mcp`、`huweihua123/stock-mcp`、`jm12138/qmt-mcp-server`
- AI投研流程：`HKUDS/Vibe-Trading`、`UFund-Me/Qbot`、`hugo2046/QuantsPlaybook`
- QMT/执行沙盒：`lotey/lite-qmt-executor`、`guangxiangdebizi/QMT-MCP`

执行层项目全部标记为 `sandbox_only`，当前平台只允许借鉴订单状态、风控、WAL、重复下单防护和纸面执行思路，不连接真实账户。

接入原则仍然是：数据工具增强证据采集，回测工具增强复盘能力，Agent 工具增强审稿流程；任何工具都不能直接越过信维公式给出买入结论。

## 2026-06-18 迭代：历史K线与技术因子层

本轮已把观察池历史K线落到本地库：

- 新增脚本：`D:\spacex\scripts\a_stock_kline.py`
- 新增表：`stock_kline_daily`
- 当前数据：162只观察池股票、37,855条日K记录，主要来源为 `tencent-kline`
- 数据源纪律：自动链路为 `mootdx -> tencent-kline -> eastmoney-kline`，东财历史K线只做最后兜底并继续串行限流
- 北交所腾讯前缀已支持 `bj`，但部分北交所个股历史深度仍可能很短，模型会用质量分自然降权

常用命令：

```powershell
python D:\spacex\scripts\a_stock_kline.py refresh-code 300136 --days 120 --provider auto
python D:\spacex\scripts\a_stock_kline.py refresh-watchlist --scope model --limit 50 --days 120 --provider auto
python D:\spacex\scripts\a_stock_kline.py refresh-replay-missing --limit 0 --days 240
python D:\spacex\scripts\a_stock_kline.py show 300136 --top 10
```

因子层已升级到 `stock-factor-v0.3-indicators`：

- 原有：均线、动量、20日回撤、波动率、资金流、流动性、估值
- 新增：RSI14、MACD DIF/DEA/HIST、布林带中轨/上下轨/位置、ATR14%
- 新增分项：`technical_score`
- 综合因子权重：趋势25%、流动性20%、资金15%、波动15%、估值10%、技术15%
- 模型版本：`xinwei-research-priority-v0.4-formula-gate`

关键边界：技术指标只刻画走势斜率、波动和拥挤度，不升级任何信维公式维度；买入前仍必须用S/A级证据验证产业命题、客户绑定、订单/产能和扣非利润拐点。

本轮校验结果：

- `stock_factor_daily` 最新完整快照日为 `2026-06-17`
- 信维通信 `300136` 样本天数60、因子质量100、技术分60.10、RSI14为45.62、ATR14%为8.95
- 研究优先级队列保持克制：`deep_research=1`、`wait_evidence=96`、`archive_watch=65`

## 2026-06-18 迭代：数据源健康校验

本轮新增数据源健康检查层，先解决“不能默默相信单一行情源”的问题：

- 新增脚本：`D:\spacex\scripts\a_stock_provider_health.py`
- 新增表：`provider_health_checks`
- 当前校验逻辑：把 `market_snapshot` 的行情快照与 `stock_kline_daily` 的日K数据交叉比较
- 盘中快照纪律：如果行情快照采集于收盘前，则不直接拿盘中价和收盘价做失败判断，而是检查价格是否落在当日日K高低价区间内
- 状态含义：`range_pass` 表示盘中价落在当日K线区间；`pass` 表示收盘价偏差可接受；`warn`/`fail` 表示偏差过大；`comparison_missing` 表示缺K线；`primary_missing` 表示行情快照缺价格

常用命令：

```powershell
python D:\spacex\scripts\a_stock_provider_health.py refresh --scope model --limit 50
python D:\spacex\scripts\a_stock_provider_health.py show --top 20
```

本轮校验结果：

- 校验日：`2026-06-17`
- 覆盖观察池模型队列前50只：`range_pass=48`、`comparison_missing=1`、`primary_missing=1`
- 本地网站已显示“源校验 49/50”，缺K线0只，仍有1只行情快照缺主价格
- 能力路线图中的 `provider_redundancy` 已从 `planned` 更新为 `started`

后续扩展方向：把实时腾讯行情、mootdx和可选Tushare纳入同一张健康表，日报生成前先输出“数据源可信度摘要”，再进入信维公式研究队列。

## 2026-06-18 迭代：日报候选纸面回放

本轮新增推荐后复盘层，用来回答“每天选出来的候选，后面市场到底怎么验证”：

- 新增脚本：`D:\spacex\scripts\a_stock_replay.py`
- 新增表：`paper_replay_results`
- 当前口径：日报候选在推荐日之后的下一根可用日K开盘价作为纸面观察入口
- 观察指标：T+1/T+3/T+5/T+10/T+20 收盘收益、最新收益、最大收盘收益、最大盘中浮盈、最差盘中收益、最大回撤
- 风险标记：是否触发5%浮盈、5%回撤、8%回撤
- 数据纪律：如果推荐后缺少次日日K，不用几天后的K线冒充入场价，而是标记 `no_entry_kline` 或 `stale_entry_gap`

常用命令：

```powershell
python D:\spacex\scripts\a_stock_replay.py refresh
python D:\spacex\scripts\a_stock_replay.py show --top 20
python D:\spacex\scripts\a_stock_replay.py summary
```

本轮回放结果：

- 已回放候选：417条
- 可用次日开盘回放：417条
- 缺次日日K：0条
- 最新可回放日：`2026-06-18`
- 可回放样本平均最新收益：约2.56%，T+1均值约2.08%，正收益率约69.1%

这些数字只用于复盘模型和数据覆盖，不构成买入结论。下一步进入组合级纸面回测：T+1、涨跌停、滑点、手续费、单票15%仓位上限。

## 2026-06-19 迭代：休市日防误抓与证据补全

2026-06-19 为端午节休市日，交易所安排为 6 月 19 日至 6 月 21 日休市、6 月 22 日起照常开市。本地日报链路已增加交易日判断：

- `a_stock_daily.py` 内置 2026 年 A 股休市日历。
- 休市日运行 `run-daily` 会写入 `ingestion_log.source=market-calendar`、`status=skipped`。
- 休市日跳过东财全市场快照和候选生成，避免把“无交易”误判为源失败，或生成空候选日报。

今天仍完成了观察池追踪层刷新：

- `stock_kline_daily`：162只观察池股票，37,855条日K，最新日为 `2026-06-18`。
- `paper_replay_results`：417条候选全覆盖；平均最新收益约2.59%，T+1均值约2.11%，5%止盈命中199条，5%止损命中67条。
- `stock_factor_daily`：观察池因子刷新到 `2026-06-18`。

证据工作台对前排研究队列补采公告和研报线索：

- `stock_evidence_items`：10只股票共324条S/A级线索，其中S级210条、A级114条。
- `stock_xinwei_reviews`：33个维度为 `needs_review`，939个维度仍为 `pending`。
- 模型队列刷新后：`deep_research=10`、`wait_evidence=87`、`archive_watch=65`。
- 新增信维公式买入闸门：`eligible_for_buy=0`、`needs_manual_review=10`、`missing_evidence=152`。

重要边界：`deep_research` 表示“值得人工深挖”，不表示信维公式六要素已经通过。只要任一维度仍为 `pending` 或 `needs_review`，尤其是“稀缺卡位”和“龙头客户绑定”，日报只能给待验证/深挖，不得给买入结论。
## 2026-06-20 v0.5 Evidence Ledger

本轮把“信维公式是否通过”从模型 JSON 摘要升级为可查询的结构化数据库层：

- 新增 `xinwei_evidence_links`：把 `stock_evidence_items` 中的 S/A 级公告、研报、调研线索连接到六项信维维度，并记录匹配关键词、证据状态和人工确认状态。
- 新增 `xinwei_gate_snapshots`：按日期保存每只股票的六项闸门状态、阻塞维度、买入资格和证据链 JSON。
- 新增 `research_tasks`：自动生成待核验任务，优先处理“稀缺卡位”和“双龙头客户绑定”。
- `stock_model_scores` 新增 `evidence_availability_score` 与 `formula_verification_score`，前者表示有无 S/A 线索，后者表示六项是否已验证。
- 模型版本升级为 `xinwei-research-priority-v0.5-evidence-ledger`；`needs_review` 不计入通过，只有人工验证后的 S/A 证据才打开买入闸门。

常用命令：

```powershell
python D:\spacex\scripts\a_stock_evidence_gate.py refresh
python D:\spacex\scripts\a_stock_evidence_gate.py show --code 300136
python D:\spacex\scripts\a_stock_evidence_gate.py report --top 30
python D:\spacex\scripts\a_stock_model.py --date 2026-06-18 refresh
```

最新快照验证结果：`xinwei_gate_snapshots=162`、`xinwei_evidence_links=151`、`research_tasks=972`；买入资格仍为 0，只能输出深挖/待验证。全表会保留多日期历史快照，因此总行数会高于单日快照数。
