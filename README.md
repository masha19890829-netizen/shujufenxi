# Shujufenxi - A 股研究与信维公式候选日报系统

## 项目简介

本项目是一个面向 A 股投资研究的本地化决策辅助系统，核心目标是把“信维公式”研究框架落地为可执行日报流程。项目通过统一的本地数据流水线与证据库，输出符合研究纪律的每日候选清单，并支持风险分级、证据追踪与仓位建议。

与短线技术面交易不同，项目强调：
- 产业趋势与财务逻辑优先
- 本地证据链完整性优先于单一技术信号
- 先做 evidence gating（证据门禁），再讨论仓位与执行

## 主要能力

- 全市场数据刷新与打分
  - `scripts/a_stock_daily.py`
- 信维公式候选评分与模型刷新
  - `scripts/a_stock_model.py`
- 研究证据链、门禁与复核
  - `scripts/a_stock_evidence.py`
  - `scripts/a_stock_evidence_gate.py`
- 候选机会与周报/日报生成
  - `scripts/a_stock_opportunity.py`
  - `scripts/a_stock_weekly_review.py`
- 本地 Web 页面与导出
  - `scripts/a_stock_web.py`（本地站点默认 `http://127.0.0.1:8765`）

## 目录结构

- `scripts/`：核心执行脚本
- `docs/`：方法说明、公式规则、数据说明与调研资料
- `data/`：本地运行数据目录（建议加入 `.gitignore` 不提交）
- `netlify.toml`：站点部署配置
- `requirements-a-stock.txt`：Python 依赖

## 快速开始

1. 安装依赖
```bash
pip install -r requirements-a-stock.txt
```

2. 刷新日常候选
```bash
python scripts\a_stock_daily.py run-daily --top 80 --quiet
```

3. 刷新模型并查看研究队列
```bash
python scripts\a_stock_model.py refresh
python scripts\a_stock_model.py show --top 20
```

4. 启动本地站点（可视化与调试）
```bash
python scripts\a_stock_web.py
```

## 使用约束（重要）

当前项目约定的风控规则中，**“needs_review”不得作为通过条件**，缺少 S/A 级证据时只能进入“等证据/观察”，不应输出明确买入结论。

## 备注

本项目以可追溯的本地证据为核心，强调研究质量治理而非纯买点博弈，适合用作投资研究日报、看板与复核协同入口。

