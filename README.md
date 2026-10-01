# 灾害转移家庭与药品补给账本

面向洪灾转移场景的后端服务：把家庭关系、待核实身份、撤离批次、安置容量、
健康需求、药品批次和寻亲线索连成**可回放的事件记录**。无第三方依赖，
仅使用 Python 3.11+ 标准库。

## 核心原则

- **事件溯源**：每次操作追加一条不可变事件（JSONL）。状态随时可由日志完整回放，
  值班人员可回答"一个家庭在任一时点的去向、领用记录和团聚依据"。
- **最初接收事实不可篡改**：无证件人员先进入 `pending_verification`；
  后续档案合并把被合并档案的首接事实（时间、地点）原样带入存活档案，
  合并不代替核验。
- **只影响尚未完成的安排**：转运/出院/返家/调剂为 `pending → confirmed →
  executed` 状态机；已执行的安排不能取消或修改。
- **物资守恒**：已发出的物资不能抹除，只能通过 `退回`（冲减库存、保留原记录）
  或 `补发`（新批次再出、关联原记录）调整；重复回执直接返回原决定，不二次扣减。
- **冲突转人工复核**：证件号跨档案冲突、合并时姓名/家庭冲突均开复核单，
  由协调员给出结论后执行，全程留痕。
- **履职可见**：健康信息仅医疗岗可见；儿童年龄信息仅儿童福利岗与医疗岗可见。

## 岗位（X-Role）

| 岗位 | 职责 |
|---|---|
| `coordinator` | 跨区调剂、容量调整、档案合并、人工复核裁决、恢复总览 |
| `rescue` | 现场接收、撤离批次、普通转运发起与执行 |
| `shelter` | 安置点登记入住、普通物资收发 |
| `medical` | 健康记录、透析等医疗转运、药品收发 |
| `social_welfare` | 儿童信息、家庭关系、寻亲线索与团聚确认 |

## 启动

```bash
python3 -m src --journal data/relief.log --host 127.0.0.1 --port 8080
```

所有接口为 `POST /<资源>/<动作>`，请求体 JSON，岗位取 `X-Role` 头，
幂等键取 `X-Idempotency-Key` 头（也可放 body 的 `idem_key`）。

### 主要接口

| 路径 | 说明 |
|---|---|
| `/shelters/register` `/shelters/capacity` | 安置点登记、容量调整 |
| `/people/receive` `/people/verify` `/people/get` | 接收（无证件即待核实）、核验、查询（脱敏） |
| `/families/declare` `/families/link` `/families/timeline` | 家庭关系与任一时点去向视图 |
| `/profiles/merge` | 档案合并（保留首接事实；冲突转复核） |
| `/batches/register` `/batches/assign` | 撤离批次 |
| `/arrangements/request` `/confirm` `/execute` `/cancel` | 转运/医疗转运/出院/返家/跨区调剂 |
| `/health/record` | 健康需求（仅医疗岗） |
| `/medication/receive` `/dispense` | 药品批次与发放（低存量自动补给待办） |
| `/supplies/receive` `/issue` `/return` `/reissue` | 物资收发、退回、补发（回执幂等） |
| `/traces/open` `/clue` `/confirm` `/close` | 寻亲线索、自动候选、人工确认团聚 |
| `/reviews/resolve` | 人工复核裁决 |
| `/ops/tick` `/ops/recover` | 巡检与重启接续总览 |

### 示例

```bash
# 登记安置点
curl -X POST localhost:8080/shelters/register -H 'X-Role: coordinator' \
  -d '{"shelter_id":"SH1","name":"廊曼安置点","capacity":100}'
# 无证件接收 → pending_verification
curl -X POST localhost:8080/people/receive -H 'X-Role: rescue' \
  -d '{"shelter_id":"SH1","name":"玛妮"}'
# 家庭任一时点视图
curl -X POST localhost:8080/families/timeline -H 'X-Role: coordinator' \
  -d '{"family_id":"F-001","as_of":"2026-10-01T12:00:00+00:00"}'
```

## 自动派生与重启接续

每次写操作及 `/ops/tick` 会维护以下派生状态（同样以事件落盘）：

- **容量预警**：入住率 ≥ 90% 开预警（`near_full`/`full`），回落自动解除；
  普通转运不能进入满员点，医疗转运可确认但执行仍受硬容量限制。
- **失联匹配**：对开放寻亲记录，按姓名、同家庭关系/称谓、线索文本给候选与依据；
  最终团聚必须由福利/协调岗人工确认并记录依据。
- **药品补给**：某点某药品可用存量低于阈值（默认 5）自动开补给待办，
  到货自动了结，不重复开单。
- **待确认转运**：pending 超过 24 小时进入巡检报告。

进程重启后，服务从日志回放全部事件，`/ops/recover` 返回上述所有开放待办，
值班人员可无缝接续。日志若在崩溃中留下半截末行，重新打开时会自动截断修复。

## 代码结构

| 文件 | 职责 |
|---|---|
| `src/journal.py` | 追加式 JSONL 事件日志、崩溃尾部修复、时钟注入 |
| `src/projection.py` | 事件回放状态投影、合并索引、时点重放 `state_at` |
| `src/service.py` | 命令服务：权限/规则校验、幂等、冲突复核、派生检查、查询 |
| `src/access.py` | 岗位权限矩阵与字段脱敏 |
| `src/errors.py` | 稳定错误码与 HTTP 状态 |
| `src/api.py` `src/__main__.py` | 标准库 HTTP 层与启动入口 |

## 开发命令

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

示例数据不得包含真实个人信息、账号或凭据。
