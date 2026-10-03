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

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`；供应商可信度记录与隔离接口使用 `/api/trust` 前缀。

## 供应商可信度与隔离

红事（wedding）与白事（funeral）团队共享同一调度池，可信度按团队与场次类型独立累计：

- `POST /api/trust/events` 上报迟到、临时换人、投诉或正向记录；评分按场次类型、90 天滚动时间窗口和证据来源（调度、客户、督导、官方，权重不同）累计，初始 100 分，低于 60 分自动隔离对应场次能力，`both` 类型事件同时计入两类。
- 同一事件通过幂等键与事件指纹双重去重，重复上报返回 `duplicate=true` 且不重复扣分；评分台账可通过 `POST /api/trust/scores/replay` 从不可变事件表重放并返回前后校验和。
- `POST /api/trust/candidates/query` 返回未被隔离的候选团队，按评分降序、正向记录数降序、编码升序稳定排序；处于复核观察期（默认 30 天）的团队标记 `probation`，观察期结束后评分仍不达标会重新隔离，偶发一次异常不会永久出局。
- `POST /api/trust/recovery` 执行复核（驳回保持隔离，通过解除隔离并进入观察期）；`POST /api/trust/overrides` 供管理员人工覆盖，**必须填写期限 `effective_until` 与原因**。
- 分级可见性：`trust.read` 查询，`trust.event.write` 上报，`trust.recover` 复核，`trust.manage` 管理/覆盖；`trust.contact.read` 可见完整联系电话（否则脱敏），`trust.complaint.read` 可见投诉细节（否则 `detail`/证据/上报人联系方式隐藏）。clerk、auditor、trust_reviewer 系统角色分别预置了对应权限层级。
- `GET /api/trust/audit-summary` 汇总可信度相关审计动作（含被拒绝的操作）；所有状态与审计落盘 SQLite，重启后保持。


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
app/trust/          供应商可信度评分、能力隔离、复核恢复与分级可见性
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
