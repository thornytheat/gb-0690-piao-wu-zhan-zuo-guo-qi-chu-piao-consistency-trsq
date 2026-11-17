# 深度登记 票务占座过期与出票一致性

- 规模：中型
- 核心规则：占座租约使用事件时间，付款时刻必须早于到期点，同版本冲突拒绝，释放后旧支付不能复活席位
- 使用者与工作流：演出票务渠道对账员；回放多渠道占座付款出票日志，找出售卖冲突与超时悬单

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 渠道接收 | 接收各渠道事件 | `/api/m1` | `app/modules/m1_receive/rules.py` `app/modules/m1_receive/service.py` | `tests/test_m1_receive.py` |
| M2 座席映射 | 映射座席并裁决版本 | `/api/m2` | `app/modules/m2_seats/rules.py` `app/modules/m2_seats/service.py` | `tests/test_m2_seats.py` |
| M3 租约回放 | 回放占座释放租约 | `/api/m3` | `app/modules/m3_leases/rules.py` `app/modules/m3_leases/service.py` | `tests/test_m3_leases.py` |
| M4 支付归属 | 按有效租约归属支付 | `/api/m4` | `app/modules/m4_payments/rules.py` `app/modules/m4_payments/service.py` | `tests/test_m4_payments.py` |
| M5 出票核验 | 检查支付及票号一致 | `/api/m5` | `app/modules/m5_tickets/rules.py` `app/modules/m5_tickets/service.py` | `tests/test_m5_tickets.py` |
| M6 渠道对账 | 核对渠道金额与悬单 | `/api/m6` | `app/modules/m6_reconcile/rules.py` `app/modules/m6_reconcile/service.py` | `tests/test_m6_reconcile.py` |
| M7 补传复核 | 补传回放和版本复核 | `/api/m7` `POST /api/supplement/<批次>` | `app/modules/m7_replay/rules.py` `app/revision.py` `app/store.py` | `tests/test_m7_replay.py` `tests/test_m7_supplement.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | BOM | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m1_receive/rules.py` | `tests/test_m1_receive.py::test_bom` |
| TRAP-08 | 席号订单前导零 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m2_seats/rules.py` | `tests/test_m2_seats.py::test_seat_alias` |
| TRAP-09 | 超大事件ID | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m1_receive/rules.py` | `tests/test_m1_receive.py::test_large_id` |
| TRAP-11 | 时区混写 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m2_seats/rules.py` | `tests/test_m2_seats.py::test_time_equivalence` |
| TRAP-13 | 补传迟到 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m7_replay/rules.py` | `tests/test_m7_replay.py::test_late_kept` |
| TRAP-14 | 同版冲突 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m2_seats/rules.py` | `tests/test_m2_seats.py::test_real_conflict` |
| TRAP-23 | 过期端点 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/modules/m3_leases/rules.py` | `tests/test_m3_leases.py::test_expiration_endpoint_reusable` |
| TRAP-26 | 陈旧复核 | 覆盖或重复售卖 | 事件时间回放并保留隔离证据 | `app/store.py` | `tests/test_m7_replay.py::test_stale_review` |
| TRAP-31 | 补传覆盖原回执 | 原地改写原批次，破坏历史租约/付款/票据证据 | 补传追加为新当前版本，原批次只读保留 | `app/revision.py` `app/store.py` | `tests/test_m7_supplement.py::test_supplement_creates_linked_current_version` |
| TRAP-32 | 只存副本不切当前 | 另存副本而当前状态不变，失效票据仍可售 | 新版本行与谱系指针单事务切换席态/票据/悬单 | `app/store.py` `app/snapshot.py` | `tests/test_m7_supplement.py::test_supplement_switches_seats_tickets_and_pending_together` |
| TRAP-33 | 重复补传增生版本 | 每次重试都新增 revision | 补传ID含谱系与内容，重试直接返回原批次 | `app/manifest.py` `app/revision.py` | `tests/test_m7_supplement.py::test_retrying_supplement_does_not_add_a_version` |
| TRAP-34 | 切换失败半生效 | 新版本可读但席态/票据仍指旧版 | 单事务回滚，失败保留原状态并返回 STORE | `app/store.py` | `tests/test_m7_supplement.py::test_failed_switch_keeps_original_state` |
| TRAP-35 | 落库中断失去当前指针 | 恢复后无当前版本或版本号冲突 | 读取时补建谱系、重排重复 revision | `app/store.py` | `tests/test_m7_supplement.py::test_interrupted_lineage_recovers` |

## 对账规则

输入逻辑行数 = 采信行数 + 隔离行数；空行单列。每条重复、冲突、超限、缺失均保留原始行号与原因码。计算阶段的输出数量可聚合，原始采信records始终保留，业务平衡另按模块给出的重量、时长、电量或座席数量核对。
相同输入摘要、规则版本、参数产生相同批次ID。运行原子插入SQLite；复核使用整数版本乐观锁。
补传批次ID另含谱系根与补传内容，不含父版本，故同一补传重试归一到原批次。批次谱系以首次入库为 `root`、直接父批次为 `base`、整数 `revision` 递增；席态、票据有效性与渠道悬单由同一批次的回放结果派生，随当前指针一次性切换。切换失败不写批次行。

## 已知不覆盖

单场演出16个座席，不处理退票退款。占座租约释放或到期后，迟到付款不能恢复租约；出票必须已有有效付款且座席不可重复出票。
