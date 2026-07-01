# A 股交易研究能力增强清单

## 已安装

### a-stock-data

- 来源：[simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data)
- 本地路径：`C:\Users\44277\.codex\skills\a-stock-data`
- 作用：A 股行情、估值、研报、题材、北向、资金流、龙虎榜、解禁、两融、大宗交易、股东户数、分红、新闻、公告和基础财务的数据源地图。
- 使用原则：作为 A 股主数据 Skill，后续优先围绕它扩展本地数据库。

### global-stock-data

- 来源：[simonlin1212/global-stock-data](https://github.com/simonlin1212/global-stock-data)
- 本地路径：`C:\Users\44277\.codex\skills\global-stock-data`
- 作用：补充美股、港股和全球市场视角，用于外围风险、中概映射、港股同业和隔夜市场观察。
- 使用原则：只作为辅助背景，不替代 A 股本地数据库。

### jupyter-notebook

- 来源：[openai/skills](https://github.com/openai/skills)
- 本地路径：`C:\Users\44277\.codex\skills\jupyter-notebook`
- 作用：后续做因子验证、策略回测、候选池表现追踪和可复现研究 notebook。
- 使用原则：用于验证策略，而不是直接凭主观判断改规则。

## 已有插件能力

当前 Codex 环境已经有 Data Analytics、Spreadsheets、PDF、GitHub 等插件技能。对 A 股工作流最有用的是：

- Data Analytics：指标设计、报告、仪表盘、数据验证。
- PDF：读取研报、公告 PDF。
- Spreadsheets：导出候选池、复盘表、组合跟踪表。
- GitHub：继续拉取和审查外部数据项目。

## 暂不自动安装的方向

我不会盲目安装陌生第三方“荐股/交易机器人” Skill，原因是：

- 交易建议类 Skill 容易混入未经验证的规则或营销话术。
- 安装后会影响后续回答风格，必须先审查来源、许可证、代码和提示词边界。
- 对真实交易而言，透明数据、可复现计算和风险约束比“玄学胜率”更重要。

后续如果要继续扩展，优先找这几类：

1. A 股公告/财报解析 Skill。
2. 回测和因子研究 Skill。
3. 新闻舆情和事件抽取 Skill。
4. 组合风控、仓位管理和交易日志 Skill。
5. 宏观、汇率、商品、海外指数数据 Skill。

## 需要重启

新安装的 Codex skills 通常需要重启 Codex 后，才会在新的会话技能列表里自动出现。

## 2026-06-18 GitHub 能力库扩展

已完成一轮 GitHub 工具调研，并把结果写入本地数据库：

- 调研文档：`D:\spacex\docs\github_a_stock_tool_survey.md`
- 维护脚本：`D:\spacex\scripts\a_stock_tool_registry.py`
- 数据表：`external_tool_registry`、`capability_roadmap`
- 本地网站：`http://127.0.0.1:8765` 的“GitHub 能力库”区块

本轮吸收的能力方向：

1. 数据源冗余：mootdx、AKShare、Tushare、easyquotation、Ashare。
2. 因子与模型：Qlib、MyTT、FinRL/FinRL-X。
3. 回测与风控：RQAlpha、vn.py、QUANTAXIS、a-stock-agent。
4. 多 Agent 研究流程：TradingAgents、TradingAgents-CN。
5. 架构分层：ZVT 的 provider/schema/tag 思路。

第二轮补充吸收：

1. 因子公式与指标库：CZSC、stockstats、ta。
2. 回测框架与复盘表达：backtrader、backtesting.py、vectorbt。
3. 本地落地：新增 `stock_factor_daily`，模型版本升级为 `xinwei-research-priority-v0.2-factor`，本地网站展示因子分和因子质量。

本轮明确排除或限制：

- `pytdx`：仓库归档且停止维护，优先使用 `mootdx`。
- `easytrader`：只作为沙盒参考，不接实盘交易。
- `TradingAgents-CN`：混合许可证，不能直接复用专有 app/frontend。
- `MyTT`：GPL 标识，先参考公式思想，直接代码复用前必须做许可证复核。

维护命令：

```powershell
python D:\spacex\scripts\a_stock_tool_registry.py refresh
python D:\spacex\scripts\a_stock_tool_registry.py show --limit 20
```

因子层维护命令：

```powershell
python D:\spacex\scripts\a_stock_factors.py refresh
python D:\spacex\scripts\a_stock_factors.py show --top 20
```

## 2026-06-18 第三轮能力增强

本轮没有安装未知“荐股Skill”，而是继续把外部GitHub项目沉淀为可审计的本地能力库：

- 数据底座：新增 `stock_kline_daily`，用腾讯K线作为当前可用兜底，保留 `mootdx` 优先和东财限流兜底纪律。
- 因子算法：`stock-factor-v0.3-indicators` 已加入 RSI14、MACD、布林带、ATR14% 和 `technical_score`。
- 模型排序：`xinwei-research-priority-v0.4-formula-gate` 继续保持“证据门槛优先”，技术因子不升级信维公式维度。
- GitHub工具库：已扩展到38个项目，新增 `qka`、BaoStock/stock MCP、`Vibe-Trading`、`Qbot`、QMT沙盒执行项目等。
- 执行红线：`lite-qmt-executor`、`QMT-MCP`、`easytrader` 等只作为 `sandbox_only` 参考，不连接真实账户。

当前可运行入口：

```powershell
python D:\spacex\scripts\a_stock_kline.py refresh-watchlist --scope model --limit 50 --days 120 --provider auto
python D:\spacex\scripts\a_stock_kline.py refresh-replay-missing --limit 0 --days 240
python D:\spacex\scripts\a_stock_provider_health.py refresh --scope model --limit 50
python D:\spacex\scripts\a_stock_replay.py refresh
python D:\spacex\scripts\a_stock_factors.py --date 2026-06-17 refresh
python D:\spacex\scripts\a_stock_model.py refresh
python D:\spacex\scripts\a_stock_tool_registry.py refresh
python D:\spacex\scripts\a_stock_web.py --host 127.0.0.1 --port 8765
```

## 2026-06-18 第四轮能力增强

本轮把“数据源冗余”从路线图推进到可运行状态：

- 新增 `provider_health_checks` 表，用于记录行情快照和K线/备用行情源的交叉校验。
- 新增 `a_stock_provider_health.py`，当前先做 `market_snapshot` vs `stock_kline_daily` 的日度校验。
- 盘中快照不会被错误地拿来和收盘价硬比；如果价格落在当日日K高低区间，标记为 `range_pass`。
- 本地观察台新增“源校验”KPI，当前结果为49/50通过、0只缺K线、1只快照缺主价格。
- `provider_redundancy` 路线图状态已改为 `started`，下一步接入实时腾讯、mootdx和可选Tushare交叉校验。

这一步的意义不是提高“荐股命中率”，而是先提高数据可信度：日报和模型排序必须先知道自己依赖的数据有没有缺口、错位或盘中/收盘口径混用。

## 2026-06-18 第五轮能力增强

本轮把“纸面回测/复盘”从路线图推进到单票候选回放层：

- 新增 `paper_replay_results` 表，按每次日报候选沉淀后续表现。
- 新增 `a_stock_replay.py`，用推荐日后的下一根可用日K开盘价作为纸面观察入口。
- 当前指标包括 T+1/T+3/T+5/T+10/T+20 收益、最新收益、最大浮盈、最差盘中收益、最大回撤、5%止盈/止损触发。
- 全量回放417条候选，当前417条均具备可用次日K线。
- 本地观察台新增“纸面回放”KPI，最新日报候选条目显示T+1纸面收益。
- `backtest_risk_engine` 路线图状态已改为 `started`。

这不是实盘交易，也不是完整组合回测；它只是让我们每天复盘“模型提出的研究队列有没有被后续行情验证”。下一步才加入涨跌停、滑点、费用、单票15%仓位上限和组合层风险账本。

后续最优先的增强方向：

1. 组合纸面回测：参考 `qka` / `rqalpha`，建立T+1、涨跌停、滑点、手续费、单票15%仓位限制。
2. Provider健康检查二期：接入实时腾讯、mootdx和可选Tushare，形成多源价格漂移表。
3. 证据工作台：把公告、财报附注、研报和机构调研纪要拆成S/A/B/C来源表，不让模型用传闻升级维度。
4. MCP数据桥：只读接入BaoStock/stock-data类MCP，所有输出先落SQLite再参与评分。

## 2026-06-19 第六轮能力增强

本轮处理休市日晨报和证据补全：

- 新增休市日防误抓：`a_stock_daily.py` 内置2026年A股休市日历，端午节等休市日直接记录 `market-calendar/skipped`，不再硬抓东财全市场快照。
- 证据补全：对研究队列前排10只股票采集公告与研报线索，新增/累计324条S/A级证据项。
- 模型重算：观察池最新队列为 `deep_research=10`、`wait_evidence=87`、`archive_watch=65`。
- 风控边界：`needs_review` 不等于公式通过；缺任一信维维度时只能深挖或待验证，不能写成买入。
- 数据追踪：观察池K线扩至37,855条，最新可回放日仍为2026-06-18；纸面回放417/417全覆盖。

这一步让系统更像真正的研究员：先识别“今天不开市”，再把精力转向证据补全和数据库训练，而不是机械生成没有交易意义的买入列表。

## 2026-06-19 第七轮能力增强

本轮把“信维公式是否真的通过”从叙述性边界固化成模型字段：

- 模型版本升级为 `xinwei-research-priority-v0.4-formula-gate`。
- `stock_model_scores.score_json.formula_gate` 新增 `eligible_for_buy`、`supported_dimensions`、`needs_review_dimensions`、`pending_dimensions`、`critical_unresolved_dimensions`。
- 当前闸门结果：买入资格0只，待人工核验10只，缺S/A证据152只。
- 命令行 `a_stock_model.py show` 会直接显示 `gate=... buy_eligible=no`。
- 本地观察台新增“买入资格”KPI，单票详情显示“公式闸门”和具体缺口。

这一步的目的很硬：模型可以积极排序研究精力，但不能把“抓到标题线索”偷换成“信维公式已验证”。只有六个维度全部人工确认通过，买入闸门才打开。
## 2026-06-20 第八轮能力增强：v0.5 Evidence Ledger

本轮完成“证据闭环”升级，不再把标题线索直接等同于信维公式通过：

- 新增 `a_stock_evidence_gate.py`，负责刷新结构化证据链接、公式闸门快照和待核验任务。
- 新增 `xinwei_evidence_links`、`xinwei_gate_snapshots`、`research_tasks` 三张表。
- 模型升级为 `xinwei-research-priority-v0.5-evidence-ledger`。
- `evidence_score` 拆分为 `evidence_availability_score` 与 `formula_verification_score`。
- 新增 `blocked_by_evidence` 队列：市场/因子/线索强，但稀缺卡位或双龙头客户绑定等关键维度未闭环时，明确阻塞，仓位为 0%。
- 本地网站新增 `/api/gate-matrix`、`/api/research-tasks`，首页展示六项公式矩阵。

最新快照状态：

- 结构化证据链接：151 条。
- 公式闸门快照：162 只。
- 待核验任务：972 条。
- 买入资格：0 只。
- v0.5 队列：`blocked_by_evidence=10`、`wait_evidence=87`、`archive_watch=65`。

下一轮优先级：

1. 给网站增加人工核验入口，把某条 S/A 证据确认成 `verified` 或打成 `failed`。
2. 把财报字段（扣非净利润、合同负债、在建工程、经营现金流）拆成结构化表，减少人工看摘要。
3. 将 `research_tasks` 按优先级驱动每日深挖，不再平均采集。
