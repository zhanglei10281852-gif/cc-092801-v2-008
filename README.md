# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。供应商可信度与隔离接口位于 `/api/trust`。

## 供应商可信度与隔离

红事与白事团队共享调度池，可信度评分按场次类型分别累计，避免白事异常连累红事接单，反之亦然：

- 事件类型 `late`（迟到）、`substitute`（临时换人）、`complaint`（投诉）、`no_show`（爽约）、`praise`（表扬），严重度 1-3，证据来源按可信度加权（`gps`/`dispatch_system` 高于 `online_review`）。
- 评分在 30/90 天滚动窗口内累计；每条事件冻结当时的策略版本（`trust-policy-2026.10`）、权重与扣分，评分由事件台账顺序折叠得到，可随时重放且结果稳定。
- 同一供应商的同一 `occurrence_key` 只入账一次，重复上报返回原事件并记一条 `denied` 审计，不会重复扣分。
- 窗口扣分达到 15 分进入**降温**（仍可接单但排序靠后），达到 30 分或窗口内 2 起严重投诉则**自动隔离**该场次类型；隔离只限制对应能力。窗口滚动后偶发异常自然衰减。
- 人工覆盖（`manual_block`/`manual_cooling`）必须写明原因和到期时间（停用最长 180 天、降温最长 90 天），到期自动失效。
- 复核员可将隔离恢复为限期**观察期**（`probation`，最长 60 天，需满 7 天最短观察期才能正式 `restore`）；复核放行后旧账不再触发隔离，只保留历史贡献供解释。证据可被复核员**否决**（`dismiss`），事件不删除、只标记排除。

权限分级（初始化时自动创建 `dispatcher` 现场调度员与 `trust_reviewer` 可信度复核员角色）：

| 能力 | trust.read | trust.dispatch | trust.review | trust.write |
| --- | --- | --- | --- | --- |
| 查看评分/候选 | ✓ | ✓ | ✓ | ✓ |
| 完整联系电话 | – | ✓ | – | ✓ |
| 上报事件 | – | ✓ | – | ✓ |
| 投诉细节/否决证据 | – | – | ✓ | ✓ |
| 复核恢复 | – | – | ✓ | ✓ |
| 登记供应商/人工覆盖 | – | – | – | ✓ |

主要接口（均需 Bearer 会话令牌）：

```text
POST /api/trust/vendors                     登记供应商（trust.write）
POST /api/trust/events                      上报事件，幂等（trust.dispatch）
GET  /api/trust/vendors/{code}/scorecard    评分卡：两窗口逐项贡献与阈值解释
GET  /api/trust/vendors/{code}/events       事件明细（投诉细节按分级脱敏）
GET  /api/trust/candidates/{wedding|funeral} 候选供应商：稳定排序，隔离者不可见
POST /api/trust/overrides                   人工覆盖，必须带 reason 与 expires_at
POST /api/trust/reviews                     复核恢复（restore / probation）
POST /api/trust/events/dismiss              否决事件证据（trust.review）
GET  /api/trust/audit-summary               审计与处置摘要
```

可信度演示：

```bash
python -m app.cli trust-demo
```

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/trust/         供应商可信度事件台账、评分回放、隔离与复核
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
