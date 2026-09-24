# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 暴露清单：导入带坐标和分区编码的建筑与关键设施记录，逐行校验、按来源批次留痕、跨批次去重，并依据烈度格网输出稳定排序的分区高风险摘要与数据质量统计。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 暴露清单流程

拿到烈度格网后，可按"导入设施 → 上传/生成格网 → 阈值汇总"的顺序重复执行，全部能力同时提供 API 与命令行入口。

记录字段：`external_id`（设施业务编码，可选但强烈建议提供，作为跨批次去重键）、`district_code`（分区编码）、`name`、`feature_type`（`building/hospital/school/government/shelter/lifeline/other`）、`population`（可选，默认 0）、`latitude`、`longitude`、`updated_at`（可选 ISO-8601）。

导入保证：

- 同一 `batch_ref` 再次导入直接返回首次结果（含拒绝行号），不产生重复数据。
- 坏行（缺分区、类型非法、经纬度越界、人口为负、时间格式错误等）被拒绝并返回 `row`（从 1 开始的输入行号）和原因，同批其他行照常确认入库。
- 同一批次内重复行计入 `duplicate_count` 并记录首次行号；跨批次的同一设施（优先按 `external_id`，缺失时按类型+分区+名称+坐标指纹）在汇总时只计最新版本，避免手工拼表重复计数；各历史版本的原始报文始终保留，可追溯到来源批次与原行号。

### 命令行

```bash
# JSON（顶层可带 batch_ref/source/received_at，也可用参数覆盖）或 CSV（表头列同字段名）
python -m app.cli exposure-import --file records.json
python -m app.cli exposure-grid --file grid.json          # 列：latitude,longitude,intensity
python -m app.cli exposure-summary --intensity-min 7      # 稳定排序的分区摘要 + 数据质量统计
```

### API

```bash
# 导入批次（返回 accepted/rejected/duplicates，rejected 含行号与原因）
curl -sS -X POST http://127.0.0.1:8432/api/seismic/exposure/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_ref":"b-2026-0924","source":"县住建局","received_at":"2026-09-24T09:30:00+00:00","records":[
        {"external_id":"H001","district_code":"510121","name":"县人民医院","feature_type":"hospital","population":410,"latitude":30.85,"longitude":104.40},
        {"district_code":"510122","feature_type":"building","latitude":999,"longitude":104.6}]}'

# 上传烈度格网（按 scope 替换，可从已完成的计算任务生成：POST /api/seismic/exposure/computations/{task_id}/grid?scope=default）
curl -sS -X PUT http://127.0.0.1:8432/api/seismic/exposure/grids/default \
  -H 'Content-Type: application/json' \
  -d '{"cells":[{"latitude":30.85,"longitude":104.40,"intensity":8.1},{"latitude":30.90,"longitude":104.60,"intensity":4.5}]}'

# 分区摘要 + 高风险明细 + 质量统计（districts 按 district_code、明细按 district_code,record_key 稳定排序）
curl -sS 'http://127.0.0.1:8432/api/seismic/exposure/summary?intensity_min=7&tolerance_km=15'

# 批次查询与单设施版本追溯
curl -sS http://127.0.0.1:8432/api/seismic/exposure/batches
curl -sS http://127.0.0.1:8432/api/seismic/exposure/batches/b-2026-0924
curl -sS http://127.0.0.1:8432/api/seismic/exposure/records/ext:H001/history
```

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取、暴露清单导入去重与分区汇总，以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测、科学计算与暴露清单流程
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
