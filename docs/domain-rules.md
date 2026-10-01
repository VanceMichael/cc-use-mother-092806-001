# 领域规则与事件约定

管理洪灾撤离、安置容量、家庭团聚、敏感需求和救助物资。

现有领域资料围绕以下边界组织：

- 家庭与人员核验
- 撤离和安置容量
- 寻亲团聚
- 物资批次守恒
- 隐私与恢复待办

资料版本只能递增。参与方、事实和约束均不能为空，示例不得包含真实个人信息、账号或凭据。

## 履职岗位

| 岗位标识 | 职责 |
| --- | --- |
| `rescue_worker` | 现场接收、撤离批次、入住登记 |
| `site_worker` | 入住/出院、普通物资发放退回、证件初核 |
| `provincial_coordinator` | 安置点容量、跨区调剂、转运改派、全局可见 |
| `medical_worker` | 医疗档案、透析转运、药品补给 |
| `social_worker` | 儿童保护、家庭关系、寻亲线索与团聚确认 |
| `supervisor` | 人工复核裁决 |

## 不可变规则

1. **待核实先行**：无证件人员进入 `unverified`；核实后才能参与需要身份确认的操作。
2. **合并保真**：档案合并后，主档案的"最初接收事实"取全部谱系中最早的一条，被并档案标记为 `merged`，所有接收事实保留可回放。
3. **终态不可变**：转运/安排只有 `pending/confirmed/in_progress` 可改；`completed/cancelled` 后只能新建。返家、出院、跨区调剂只影响尚未完成的安排。
4. **物资守恒**：已发放物资只能通过退回（恢复库存）或补发（新建挂接单据）调整；任何调整都是事件，不允许改写原单。
5. **重复回执幂等**：带同一 `command_id` 的重复命令返回首次决定（`replayed: true`），不产生新事件。
6. **内容冲突转人工**：重复接收、证件号冲突、跨家庭合并冲突、竞争性匹配等不自动裁决，写入 `review.opened`（必要时同时 `trace.conflict`）。
7. **敏感信息最小可见**：儿童字段（`is_minor/approx_age`）与医疗信息（病情、透析、药品）只在履职岗位的读模型中出现，其他岗位得到 `【受限】` 占位。
8. **可回放**：全部决定是只追加事件，序号 + 前序哈希构成哈希链；重启重放即恢复状态、计数器水位和预警去重键。

## 事件类型

- 人员与家庭：`family.registered` `person.received` `person.verified` `person.merged` `family.linked` `family.relinked` `person.flagged`
- 安置点与撤离：`site.registered` `site.capacity_changed` `site.closed` `evac.batch_created` `evac.batch_status`
- 位置：`person.admitted` `person.discharged` `family.returned_home`
- 转运：`transport.created` `transport.revised` `transport.confirmed` `transport.status`
- 医疗与药品：`medical.profile_updated` `med.supply_registered` `med.refilled` `med.batch_registered` `med.batch_received` `med.batch_depleted`
- 物资：`supply.item_registered` `supply.allocated` `supplies.distributed` `supplies.returned` `supplies.reissued`
- 寻亲：`trace.opened` `trace.conflict` `match.proposed` `match.confirmed` `match.rejected` `trace.closed`
- 治理：`review.opened` `review.resolved` `alert.raised` `alert.acknowledged` `alert.resolved`

## 恢复巡检（启动与周期执行同一套逻辑）

- **容量预警**：占用率 ≥90% 预警、≥100% 危急；回落自动解除。
- **失联匹配**：对开放线索重跑评分，达阈值生成 `match.proposed`（仅建议，团聚必须人工确认）。
- **药品补给**：预计 2 天内断药、在途批次超过预计到货 6 小时仍未到达，分别预警；补给/到货后解除。
- **待确认转运**：12 小时窗口内仍 `pending` 的转运预警，透析患者提级为 `critical`。
- **家庭分离**：活跃家庭成员分布在多个安置点时预警，团聚后解除。
