# 灾害转移家庭与药品补给账本

管理洪灾撤离、安置容量、家庭团聚、敏感需求和救助物资的后端服务。

服务把**家庭关系、待核实身份、撤离批次、安置容量、健康需求、药品批次和寻亲线索**连成一条只追加、可回放的记录：

- 没有证件的人先进入**待核实**状态；档案合并时保留**最初接收事实**
- 儿童与医疗信息对非履职岗位**字段级脱敏**
- 转运、出院、返家、跨区调剂只影响**尚未完成**的安排
- 已发出的物资只能通过**退回或补发**调整，不做原地改写
- 重复回执返回**原决定**（幂等）；内容冲突**转人工复核**
- 重启后自动接续**容量预警、失联匹配、药品补给、待确认转运**
- 值班人员可回答一个家庭在**任一时点**的去向、领用记录与重新团聚依据

## 架构

```
HTTP API (stdlib)  ──► CommandService ──► EventStore (JSONL 哈希链, fsync)
                            │                   │
                     授权/校验/幂等        重放 ▼
                            └──────────────► State（折叠的当前状态）
                                                  │
RecoverySweeper（启动 + 每 60s）──────────────────┤
QueryService（含 at_seq/at_ts 历史回放、脱敏）◄───┘
```

- **零第三方依赖**，仅需 Python ≥ 3.11。
- 每条记录包含 `seq / ts / prev / hash`，任何删除、改写、乱序在重放时都会被检测为 `ledger_integrity`。
- 命令在单把可重入锁内完成"追加事件 + 折叠状态"，多事件命令对查询不暴露中间态。

## 启动

```bash
python3 -m src.disaster_relief --ledger data/ledger.jsonl --host 0.0.0.0 --port 8080
```

启动时重放账本并立即执行一次恢复巡检，随后后台周期巡检（可通过 `--sweep-interval-ms` 调整）。

## API 约定

- 所有请求需带头 `X-Actor-Role: <岗位>`（见下）；可选 `X-Actor-Id`。
- 写命令：`POST /commands/<命令名>`，JSON 对象为命令负载。
- 幂等：写命令带 `X-Command-Id: <回执标识>`；重复提交返回 `200` 与首次决定（`"replayed": true`），不产生新事件。
- 冲突转人工时返回 `202 {"status":"manual_review","review_id":...}`。

岗位：`rescue_worker`、`site_worker`、`provincial_coordinator`、`medical_worker`、`social_worker`、`supervisor`。

### 命令一览

| 主题 | 命令 |
| --- | --- |
| 家庭/人员 | `register_family` `receive_person` `verify_person` `merge_person` `link_family_member` `relink_family` `flag_person` |
| 安置/撤离 | `register_site` `change_site_capacity` `close_site` `create_evac_batch` `set_evac_batch_status` |
| 位置 | `admit_person` `discharge_person` `return_family_home` |
| 转运 | `create_transport` `revise_transport` `confirm_transport` `set_transport_status` |
| 医疗药品 | `update_medical_profile` `register_medication_supply` `refill_medication` `register_med_batch` `receive_med_batch` `deplete_med_batch` |
| 物资 | `register_supply_item` `allocate_supplies` `distribute_supplies` `return_supplies` `reissue_supplies` |
| 寻亲 | `open_trace` `propose_match` `confirm_match` `reject_match` `close_trace` |
| 治理 | `resolve_review` `acknowledge_alert` `resolve_alert` |

### 查询一览

| 路径 | 说明 |
| --- | --- |
| `GET /queries/families` | 全部家庭与分离标记 |
| `GET /queries/families/{id}` | 家庭成员、按安置点分布 |
| `GET /queries/families/{id}/timeline?at_seq=&at_ts=` | **任一时点**去向、领用、团聚依据 |
| `GET /queries/persons?person_id=` | 人员视图（按岗位脱敏） |
| `GET /queries/persons/{id}/distributions` | 领用记录 |
| `GET /queries/sites` / `/queries/sites/{id}` | 容量占用与物资余量 |
| `GET /queries/transports?status=` | 转运安排 |
| `GET /queries/traces?status=` / `/queries/traces/{id}` | 寻亲线索与匹配 |
| `GET /queries/reviews?status=` | 人工复核单 |
| `GET /queries/alerts?status=` | 预警 |
| `GET /queries/med-batches?status=` | 药品批次 |
| `GET /queries/medication-due?lead_ms=` | 窗口内断药人员（药品名对非医疗岗脱敏） |
| `GET /queries/events?after_seq=&limit=` | 只读检视原始事件流 |

### 快速示例

```bash
# 省级协调员建安置点
curl -X POST localhost:8080/commands/register_site \
  -H 'X-Actor-Role: provincial_coordinator' -H 'Content-Type: application/json' \
  -d '{"site_id":"BKK-01","capacity":500,"dialysis_capable":true}'

# 救援队接收无证件儿童 → unverified
curl -X POST localhost:8080/commands/receive_person \
  -H 'X-Actor-Role: rescue_worker' -H 'Content-Type: application/json' \
  -d '{"person_id":"P-0007","site_id":"BKK-01","family_id":"F-001","is_minor":true}'

# 带回执标识的物资发放（重复提交安全）
curl -X POST localhost:8080/commands/distribute_supplies \
  -H 'X-Actor-Role: site_worker' -H 'X-Command-Id: RCPT-2026-0001' \
  -H 'Content-Type: application/json' \
  -d '{"site_id":"BKK-01","person_id":"P-0007","item_id":"RICE","quantity":1}'
```

完整业务规则与事件清单见 [`docs/domain-rules.md`](docs/domain-rules.md)。

## 代码结构

```
src/disaster_relief/
  store.py       # 只追加 JSONL 事件账本与哈希链校验
  state.py       # 事件折叠出的领域状态（位置历史、合并谱系、守恒计算）
  service.py     # 命令服务：授权、校验、幂等、冲突转复核
  matching.py    # 寻亲候选评分（可解释信号）
  recovery.py    # 容量/失联/药品/转运/分离 巡检与后台循环
  queries.py     # 读模型、脱敏、任一时点时间线
  permissions.py # 岗位授权与字段级脱敏
  api.py         # HTTP API（http.server）
  app.py         # 装配
```

`src/news_context_001.py` 继续负责读取和校验 `fixtures/context.json` 领域资料；
`contracts/context.schema.json` 描述资料结构。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读写仓库内文件（测试使用临时目录账本），不需要连接外部业务系统。
