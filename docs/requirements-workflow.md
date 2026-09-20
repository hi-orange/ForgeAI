# 需求整理、批准与工程派工

## 这一步能做什么

用户描述要做的应用，Product Manager 整理并保存 PRD（`app_spec`，条目带稳定 id）。
页面展示可勾选、编辑和新增的清单。用户明确批准后，系统保存不可变的批准版本，再由
Manager 安排 Architect 任务：固定引用批准后的 `app_spec`，主结果为 `system_design`。
Architect 完成后，Manager 才能安排 Code Engineer；编码任务固定引用该 `system_design`，
并沿来源关系读取准确的批准 PRD。批准这一步只保存 Architect 任务，不调用设计或编码模型。

旧的“`app_spec` 直接交给 Code Engineer”和“未批准即创建 Architect”运行不再兼容；
数据迁移会删除这些旧 BuildRun 及其计划、任务、执行和成果，项目与用户消息保留。
Architect 计划和任务保持 `pending` 时，BuildRun 仍为 `running / pm`。页面以
`design_pending` 表示等待 Architect，`design_running` 表示 Architect 正在产出设计。
若批准已保存但 Architect 派工失败，页面显示 `ready_for_design`，可重试且不会重跑 PRD 模型。

## 完整流程

1. 保存用户消息，并由 Manager 分类为 `product_change`。
2. 创建或复用同一项目的 queued BuildRun，针对指定消息建立初始 Plan 和需求 Task。
3. 领取 Task，再登记唯一有效的执行编号。模型调用期间不持有数据库事务。
4. Product Manager 生成经过结构校验的 `app_spec`（含稳定条目 id），先保存执行草稿。
5. 在同一事务中保存正式成果、TaskResult，完成 Task、Plan 和本次执行。
6. 有可执行功能建议时进入 `awaiting_approval`；仅当无法理解应用目标时进入
   `needs_user_input`。
7. 用户可补充说明，生成新的建议 `app_spec`（沿用未改条目的 id）。
8. 用户按条目 id 勾选、编辑或新增后提交批准；删除/未勾选的条目不会进入批准版本。
   验收条件通过 `source_ids` 关联功能 id，不能只靠正文相等猜测来源。
   只有未修改且仍选中的功能可以沿用原验收条件；新增、修改或不再有有效验收条件的功能，
   需在清单内填写“怎样算完成”。批准接口通过对应功能条目的 `acceptance` 字段接收正文，
   不调用模型猜测，也不复制过期的验收标准。
9. 批准保存成功后，安排 Architect，输入为批准后的 `app_spec`。
10. 用户继续构建时，Architect 产出完整 `system_design`；只有成果登记成功后才安排
    Code Engineer，且编码任务只接收该 `system_design`。
11. 页面刷新只读取进度，不会再次触发模型或重复派工。

## 接口补充

| 方法与路径 | 用途 |
| --- | --- |
| `POST /build-runs/{run_id}/requirements/{item_id}/approval` | 按稳定 id 提交批准清单，保存批准版本并安排 Architect |
| `POST /build-runs/{run_id}/engineering` | 执行/恢复 Architect，完成后交接并启动 Code Engineer |

`GET /requirements` 中，`design_pending` 表示 Architect 已派工并等待执行，
`design_running` 表示系统设计正在生成，`engineering_running` 表示 Code Engineer 正在实现。
`ready_for_design` 表示已批准但 Architect 派工尚未成功。

历史 v1 字符串列表通过共享读取函数转换成带稳定 ID 的结构，不覆盖原产物、内容哈希和来源。
新批准与澄清结果写入 v2 并引用旧产物；未发布的 v1 执行草稿可恢复为 v2。未知版本继续拒绝。
附属产物必须属于任务所在的项目和生产运行；其他任务的主产物也不能被登记成附属产物。
