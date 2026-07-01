# GitHub A 股数据与交易研究工具调研

调研日期：2026-06-18

本清单用于增强本地 A 股投研平台的“数据底座、模型验证、回测风控、Agent 复盘”能力。原则是：优先复用数据接口思想和架构边界，不盲目安装荐股机器人；任何工具都不能绕过信维公式的 S/A 级证据验证。

## 结论分层

### 核心接入

| 项目 | 能力 | 复用方式 | 风险边界 |
|---|---|---|---|
| [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) | A 股七层数据源地图：行情、研报、题材、资金、公告、财务等 | 作为主数据 Skill 和接口纪律 | 只是数据工具，不是自动推荐模型 |
| [mootdx/mootdx](https://github.com/mootdx/mootdx/blob/master/README.md) | 通达信 K 线、行情、离线读数、财务数据 | 后续作为 K 线/F10/财务的优先底层源 | README 声明学习交流、不得商用；TDX 服务器稳定性需监控 |

### 可选适配

| 项目 | 能力 | 复用方式 | 风险边界 |
|---|---|---|---|
| [akfamily/akshare](https://github.com/akfamily/akshare/blob/main/README.md) | 广谱金融数据接口，A 股历史行情示例完整 | 作为兜底和交叉校验源 | 包装层方便但端点变化会隐藏失败 |
| [waditu/tushare](https://github.com/waditu/tushare/blob/master/README.md) | 历史行情、日历、财务、两融、龙虎榜等 | Token 可用时接入交易日历、长期财务和历史表 | Pro token 与限频是外部依赖 |
| [shidenggui/easyquotation](https://github.com/shidenggui/easyquotation/blob/master/README.md) | 新浪/腾讯全市场实时行情 | 用于轻量实时行情兜底 | 免费端点字段和限频可能变化 |
| [mpquant/Ashare](https://github.com/mpquant/Ashare/blob/main/README.md) | 单文件 A 股行情/K 线封装，新浪+腾讯双核 | 借鉴双源热备和最小行情接口设计 | 功能轻量，不是完整数据仓库 |
| [mpquant/MyTT](https://github.com/mpquant/MyTT/blob/main/README.md) | 通达信/同花顺常用技术指标 Python 实现 | 只参考公式，后续自行实现本地因子 | README 为 GPL 标识，直接代码复用需谨慎 |
| [jealous/stockstats](https://github.com/jealous/stockstats/blob/master/README.md) | Pandas DataFrame 技术指标包装，覆盖 RSI、MACD、KDJ、布林、ATR 等 | 参考指标命名、懒计算和交叉信号设计 | 指标只做描述性因子，不作为买入依据 |
| [bukosabino/ta](https://github.com/bukosabino/ta/blob/master/README.md) | 基于 Pandas/Numpy 的技术分析特征工程 | 参考 volume、volatility、trend、momentum 四类特征组织 | 通用 OHLCV 公式需适配 A 股涨跌停、停牌、复权口径 |

### 架构吸收

| 项目 | 可学内容 | 本地落地方向 |
|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib/blob/main/README.md) | 因子工程、模型训练、回测流程、Alpha158/Alpha360 思路 | 把本地 SQLite 导出为可复现实验面板，避免未来函数 |
| [zvtvz/zvt](https://github.com/zvtvz/zvt/blob/master/README.md) | provider/schema/tag/factor/trader 分层 | 建立 provider 健康度、表级来源、动态标签 |
| [ricequant/rqalpha](https://github.com/ricequant/rqalpha/blob/master/README.rst) | 回测、模拟交易、风控、分析器、调度器 Mod | 做 A 股 T+1、涨跌停、费用、滑点的纸面回测 |
| [vnpy/vnpy](https://github.com/vnpy/vnpy/blob/master/README.md) | 事件引擎、纸账户、风控、策略实验室、网关分层 | 先学 paper/risk/event 设计，不接实盘网关 |
| [yutiansut/QUANTAXIS](https://github.com/yutiansut/QUANTAXIS/blob/master/README.md) | 账户模型、回测协议、因子层、行情存储层 | 只借鉴协议与审计链路，不引入重依赖 |
| [waditu/czsc](https://github.com/waditu/czsc/blob/master/README.md) | 缠论分型/笔/中枢、信号-事件-仓位体系、可视化 | 只借鉴“信号-事件-仓位”和图表复盘结构，不把技术信号当产业证据 |
| [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading/blob/master/README.md) | 权重向量接口、模块化策略、回测与执行一致性、风险 overlay | 后续把推荐输出转成目标权重而非“买/不买” |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL/blob/master/README.md) | train-test-trade 工作流、DRL 环境设计 | 用于学习环境，不作为现阶段主框架 |
| [mementum/backtrader](https://github.com/mementum/backtrader/blob/master/README.rst) | feed/strategy/analyser 模型、事件式回测结构 | 学习纸面回测模块边界，不接 live trading |
| [kernc/backtesting.py](https://github.com/kernc/backtesting.py/blob/master/README.md) | 简洁策略 API、指标挂载、回测统计输出 | 借鉴结果统计和复盘报告表达 |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt/blob/master/README.md) | 向量化参数实验、walk-forward、组合统计、可视化 | 借鉴参数实验与抗过拟合流程；许可证需复核 |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents/blob/main/README.md) | 基本面/情绪/新闻/技术/风险/组合经理多 Agent 辩论与记忆 | 做“多角色审稿”，但不能自动下单 |
| [shidenggui/easyquant](https://github.com/shidenggui/easyquant/blob/master/README.md) | 事件引擎、策略模板、多行情源推送 | 借鉴时钟事件和多源行情推送结构 |

### 人工复核或排除

| 项目 | 决策 | 原因 |
|---|---|---|
| [hsliuping/TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN/blob/main/README.md) | 人工复核 | 中文化和 A 股支持有参考价值，但 README 声明 app/frontend 为专有组件 |
| [WindRiders/a-stock-agent](https://github.com/WindRiders/a-stock-agent/blob/main/README.md) | 人工复核 | A 股 T+1、涨跌停、费用和风险指标可参考，但评分框架不是信维公式 |
| [shidenggui/easytrader](https://github.com/shidenggui/easytrader/blob/master/README.md) | 沙盒观察 | 涉及券商/客户端交易自动化，当前不接实盘 |
| [rainx/pytdx](https://github.com/rainx/pytdx/blob/archive/README.md) | 排除 | README 明确归档、停止维护；TDX 功能优先使用 mootdx |

## 已落地到本地库

新增表：

- `external_tool_registry`：保存 GitHub 工具的能力层、复用决策、许可证/风险说明、来源 URL。
- `capability_roadmap`：保存本地平台下一步能力路线图。

维护命令：

```powershell
python D:\spacex\scripts\a_stock_tool_registry.py refresh
python D:\spacex\scripts\a_stock_tool_registry.py show --limit 20
```

本地网站 `http://127.0.0.1:8765` 已增加“GitHub 能力库”区块，展示可复用工具与能力路线图。

## 下一步优先级

1. Provider 交叉校验：东财、腾讯、TDX 至少两源一致才提升行情可信度。
2. 因子表：`stock_factor_daily` 已建立第一版，当前沉淀均线、动量、波动率、20 日回撤、资金流、流动性、估值和因子质量；下一步补 RSI、MACD、ATR、布林和更长 K 线历史。
3. 纸面回测：新增推荐后表现回放，纳入 T+1、涨跌停、交易费用、单票 15% 仓位上限。
4. 多角色审稿：每只深度研究票生成“多头、空头、风险经理、证据官”四段报告，但信维维度仍只能由 S/A 证据升级。

## 2026-06-18 第三轮补充调研

本轮继续补充A股数据、回测、AI投研和QMT沙盒项目，并已写入 `external_tool_registry`：

| 项目 | 分类 | 复用决策 | 本地吸收方向 |
|---|---|---|---|
| [zsrl/qka](https://github.com/zsrl/qka) | A股回测 | architecture_only | 借鉴A股数据、策略、回测、报告边界，后续用于纸面回放 |
| [1nchaos/adata](https://github.com/1nchaos/adata) | 数据源 | manual_review | 复核多源数据和本地存储思路，暂不直接依赖 |
| [myhhub/stock](https://github.com/myhhub/stock) | 分析平台 | manual_review | 参考筹码、形态、回测、UI组织方式，排除自动交易部分 |
| [HuggingAGI/mcp-baostock-server](https://github.com/HuggingAGI/mcp-baostock-server) | 数据MCP | optional_adapter | 候选历史K线和季度财务MCP兜底 |
| [openstockdata/stock-data-mcp](https://github.com/openstockdata/stock-data-mcp) | 数据MCP | manual_review | 参考MCP工具注册和多源失败兜底 |
| [huweihua123/stock-mcp](https://github.com/huweihua123/stock-mcp) | 数据MCP | manual_review | 参考面向Agent的数据标准化与导出 |
| [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | AI投研流程 | architecture_only | 借鉴研究目标、MCP、向量化回测和多角色审查流程 |
| [UFund-Me/Qbot](https://github.com/UFund-Me/Qbot) | 投研/回测平台 | architecture_only | 借鉴纸面仿真、报告和闭环编排，不连接实盘 |
| [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook) | 量化研究资料 | manual_review | 作为因子验证、泄漏检查、研报复现的阅读清单 |
| [lotey/lite-qmt-executor](https://github.com/lotey/lite-qmt-executor) | QMT执行 | sandbox_only | 只借鉴WAL、订单状态、防重复、急停等纸面执行设计 |
| [guangxiangdebizi/QMT-MCP](https://github.com/guangxiangdebizi/QMT-MCP) | QMT MCP | sandbox_only | 只参考MCP边界和风控层，不连接真实账户 |
| [jm12138/qmt-mcp-server](https://github.com/jm12138/qmt-mcp-server) | QMT数据MCP | sandbox_only | 只作为未来QMT行情下载/查询桥的参考 |

本轮落地状态：

- `external_tool_registry` 已扩展到38个项目。
- `capability_roadmap` 已扩展到9项，新增 `mcp_data_bridge` 与 `execution_sandbox`。
- `execution_sandbox` 状态为 `blocked_for_live_trading`：没有独立风控清单和明确授权前，不做任何真实下单。
- 本地网站 `http://127.0.0.1:8765` 的“GitHub 能力库”区块已展示完整38个项目。

## 2026-06-18 本地因子层同步

GitHub调研中的技术指标/回测思想已经先落到一个轻量版本：

- 新增 `stock_kline_daily`，当前162只观察池股票、37,855条日K。
- 新增 `a_stock_kline.py`，自动数据源顺序为 `mootdx -> tencent-kline -> eastmoney-kline`。
- `stock_factor_daily` 升级到 `stock-factor-v0.3-indicators`，新增 RSI14、MACD、布林带、ATR14% 和 `technical_score`。
- `stock_model_scores` 升级到 `xinwei-research-priority-v0.4-formula-gate`，新增六要素买入闸门；当前买入资格0只、待人工核验10只、缺证据152只。
- 技术因子仍只做走势画像，不替代信维公式的S/A证据门槛。

## 2026-06-18 数据源健康校验同步

GitHub工具库中的 `provider_redundancy` 能力已经从规划推进到本地脚本：

- 新增 `provider_health_checks` 表，记录行情快照与K线/备用源的交叉校验。
- 新增 `a_stock_provider_health.py`，当前先校验 `market_snapshot` 与 `stock_kline_daily`。
- 当前结果：`2026-06-17` 模型队列前50只中48只 `range_pass`，1只缺K线，1只快照缺主价格。
- 本地观察台新增“源校验”KPI；下一步把腾讯实时行情、mootdx和可选Tushare并入同一健康表。

这个能力来自对 ZVT provider分层、AKShare/Tushare/mootdx多源思路和若干MCP数据桥项目的吸收，但本地执行仍遵守 `a-stock-data` 的数据源纪律：能用通达信/腾讯就不用东财高频接口，东财只做独有数据并串行限流。

## 2026-06-18 纸面回放同步

GitHub工具库中的 `backtest_risk_engine` 能力已经从规划推进到单票候选回放：

- 新增 `paper_replay_results` 表，记录每次日报候选的推荐后表现。
- 新增 `a_stock_replay.py`，当前按推荐日后下一交易日开盘价做纸面观察入口。
- 当前结果：417条候选全部可回放；可回放样本最新均值约2.56%，T+1均值约2.08%。
- 本地观察台新增“纸面回放”KPI，日报候选条目显示T+1收益。

这一层吸收了 `qka`、`rqalpha`、`backtrader`、`backtesting.py`、`vectorbt` 等项目的“先复盘再优化”思想，但目前只做单票研究队列回放；完整组合回测仍需补齐涨跌停、滑点、手续费、T+1和单票15%仓位限制后再启用。
