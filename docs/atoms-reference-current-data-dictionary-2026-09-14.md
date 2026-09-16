# ForgeAI 现有数据字典（源码基线）

核对日期：2026-09-14。基线提交：7ff0125e7446e61765ac13d079da08a823ecc4aa。

本文件由当前 ORM 的 Python AST 提取，不导入应用、不连接数据库。它描述源码模型，不证明部署数据库已执行迁移。类型和默认值保留源码表达式；未声明默认值不能视为数据库自动填充。主方案步骤 9 引用本文件作为现有全部表的逐字段字典。

## build_run

来源：[build_run.py](F:/project/ForgeAI/apps/backend/app/models/build_run.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键；对外请用 run_id |
| project_id | Integer | False | 未声明 / 未声明 | ForeignKey('project.id', ondelete='CASCADE') | 所属项目 ID |
| run_id | String(40) | False | 未声明 / 未声明 | 见表级约束 | 对外任务标识，形如 run_<uuid> |
| status | String(32) | False | BuildRunStatus.QUEUED.value / BuildRunStatus.QUEUED.value | 见表级约束 | queued/running/succeeded/failed |
| stage | String(32) | True | 未声明 / 未声明 | 见表级约束 | running 时的当前阶段；queued 时为空 |
| error | Text | True | 未声明 / 未声明 | 见表级约束 | 失败时的错误信息 |
| active_slot | SmallInteger | True | 1 / '1' | 见表级约束 | 占用项目构建名额时为 1；终态为 NULL |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |
| updated_at | DateTime | False | func.now() / func.now() | onupdate=func.now() | 最后更新时间 |

表级约束与索引（现有源码）：

- `CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_build_run_status')`
- `CheckConstraint("stage IS NULL OR stage IN ('pm', 'architect', 'developer', 'qa', 'runtime')", name='ck_build_run_stage')`
- `CheckConstraint("((status = 'queued' AND stage IS NULL) OR (status = 'running' AND stage IS NOT NULL) OR status IN ('succeeded', 'failed'))", name='ck_build_run_stage_by_status')`
- `CheckConstraint("((status IN ('queued', 'running') AND active_slot IS NOT NULL AND active_slot = 1) OR (status IN ('succeeded', 'failed') AND active_slot IS NULL))", name='ck_build_run_active_slot')`
- `UniqueConstraint('project_id', 'active_slot', name='uq_build_run_project_active_slot')`
- `UniqueConstraint('run_id', name='uq_build_run_run_id')`
- `Index('ix_build_run_project_status', 'project_id', 'status')`

## configuration_item

来源：[configuration_item.py](F:/project/ForgeAI/apps/backend/app/models/configuration_item.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键；对外请用 item_id |
| item_id | String(40) | False | 未声明 / 未声明 | 见表级约束 | 对外稳定身份证，形如 ci_<uuid>；上游引用也用此值 |
| project_id | Integer | False | 未声明 / 未声明 | ForeignKey('project.id', ondelete='CASCADE') | 所属项目 ID；删除项目时级联删除成果 |
| producer_run_id | String(40) | False | 未声明 / 未声明 | ForeignKey('build_run.run_id', ondelete='CASCADE'); index=True | 产出该成果的 BuildRun.run_id；用于追溯与晚到判定 |
| semantic_type | String(32) | False | 未声明 / 未声明 | 见表级约束 | 成果类型：app_spec/system_design/code/test_report |
| version | Integer | False | 未声明 / 未声明 | 见表级约束 | 同项目同类型内从 1 递增的版本号 |
| schema_version | Integer | False | 1 / '1' | 见表级约束 | payload 结构兼容版本 |
| payload | JSON | False | 未声明 / 未声明 | 见表级约束 | 正式成果正文 JSON；登记后不可原地修改 |
| content_hash | String(64) | False | 未声明 / 未声明 | 见表级约束 | 规范化 payload 的 SHA-256，用于内容比对 |
| upstream_item_ids | JSON | False | 未声明 / 未声明 | 见表级约束 | 直接上游成果的 item_id 列表 |
| state | String(16) | False | ConfigurationItemState.USABLE.value / ConfigurationItemState.USABLE.value | 见表级约束 | usable=可作后续输入；unusable=仅审计不可再用 |
| unusable_reason | String(500) | True | 未声明 / 未声明 | 见表级约束 | 变为 unusable 时的原因；usable 时为空 |
| unusable_at | DateTime | True | 未声明 / 未声明 | 见表级约束 | 变为 unusable 的时间；usable 时为空 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 登记时间 |

表级约束与索引（现有源码）：

- `CheckConstraint("semantic_type IN ('app_spec', 'system_design', 'code', 'test_report')", name='ck_configuration_item_semantic_type')`
- `CheckConstraint('version > 0', name='ck_configuration_item_positive_version')`
- `CheckConstraint('schema_version > 0', name='ck_configuration_item_positive_schema_version')`
- `CheckConstraint("state IN ('usable', 'unusable')", name='ck_configuration_item_state')`
- `CheckConstraint("((state = 'usable' AND unusable_reason IS NULL AND unusable_at IS NULL) OR (state = 'unusable' AND unusable_reason IS NOT NULL AND unusable_at IS NOT NULL))", name='ck_configuration_item_unusable_fields')`
- `UniqueConstraint('item_id', name='uq_configuration_item_item_id')`
- `UniqueConstraint('project_id', 'semantic_type', 'version', name='uq_configuration_item_project_type_version')`
- `Index('ix_configuration_item_project_type_state', 'project_id', 'semantic_type', 'state')`

## plan

来源：[plan.py](F:/project/ForgeAI/apps/backend/app/models/plan.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; autoincrement=True | 内部主键；对外请用 plan_id |
| plan_id | String(40) | False | 未声明 / 未声明 | 见表级约束 | 稳定计划标识，形如 plan_<uuid> |
| project_id | Integer | False | 未声明 / 未声明 | ForeignKey('project.id', ondelete='CASCADE') | 所属项目 ID |
| build_run_id | String(40) | False | 未声明 / 未声明 | ForeignKey('build_run.run_id', ondelete='CASCADE') | 所属 BuildRun.run_id；不是内部自增主键 |
| version | Integer | False | 未声明 / 未声明 | 见表级约束 | 调用方明确指定的计划版本；同一运行内唯一 |
| cause_message_id | Integer | False | 未声明 / 未声明 | ForeignKey('project_message.id', ondelete='CASCADE') | 触发计划的同项目用户消息 ID |
| definition_hash | String(64) | False | 未声明 / 未声明 | 见表级约束 | 规范化计划与全部任务的 SHA-256，用于幂等校验 |
| status | String(16) | False | PlanStatus.PENDING.value / PlanStatus.PENDING.value | 见表级约束 | pending/running/succeeded/failed/cancelled；保存时为 pending |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |
| updated_at | DateTime | False | func.now() / func.now() | onupdate=func.now() | 状态更新时间 |

表级约束与索引（现有源码）：

- `CheckConstraint('version > 0', name='ck_plan_positive_version')`
- `CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')", name='ck_plan_status')`
- `UniqueConstraint('plan_id', name='uq_plan_plan_id')`
- `UniqueConstraint('build_run_id', 'version', name='uq_plan_run_version')`
- `Index('ix_plan_project_run', 'project_id', 'build_run_id')`

## project

来源：[project.py](F:/project/ForgeAI/apps/backend/app/models/project.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键 |
| user_id | Integer | False | 未声明 / 未声明 | ForeignKey('user.id', ondelete='CASCADE'); index=True | 所属用户 ID |
| name | String(200) | False | 未声明 / 未声明 | 见表级约束 | 项目名称 |
| description | Text | True | 未声明 / 未声明 | 见表级约束 | 项目描述，可空 |
| prompt | Text | True | 未声明 / 未声明 | 见表级约束 | 创建时的初始需求文本 |
| status | String(32) | False | ProjectStatus.DRAFT.value / ProjectStatus.DRAFT.value | 见表级约束 | draft=尚无可用版本；available=已有可用版本 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |
| updated_at | DateTime | False | func.now() / func.now() | onupdate=func.now() | 最后更新时间 |

表级约束与索引（现有源码）：

- `CheckConstraint("status IN ('draft', 'available')", name='ck_project_status')`

## project_message

来源：[project_message.py](F:/project/ForgeAI/apps/backend/app/models/project_message.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键 |
| project_id | Integer | False | 未声明 / 未声明 | ForeignKey('project.id', ondelete='CASCADE') | 所属项目 ID |
| sequence | Integer | False | 未声明 / 未声明 | 见表级约束 | 项目内从 1 递增的对话序号，用于排序与增量拉取 |
| sender | String(16) | False | 未声明 / 未声明 | 见表级约束 | 发送方：user 或 assistant |
| content | Text | False | 未声明 / 未声明 | 见表级约束 | 消息正文 |
| client_message_id | String(100) | False | 未声明 / 未声明 | 见表级约束 | 前端幂等键；同项目内唯一，防止重试重复写入 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |

表级约束与索引（现有源码）：

- `CheckConstraint('sequence > 0', name='ck_project_message_positive_sequence')`
- `CheckConstraint("sender IN ('user', 'assistant')", name='ck_project_message_sender')`
- `UniqueConstraint('project_id', 'sequence', name='uq_project_message_project_sequence')`
- `UniqueConstraint('project_id', 'client_message_id', name='uq_project_message_project_client_message_id')`

## project_message_classification

来源：[project_message_classification.py](F:/project/ForgeAI/apps/backend/app/models/project_message_classification.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键 |
| message_id | Integer | False | 未声明 / 未声明 | ForeignKey('project_message.id', ondelete='CASCADE') | 对应的项目消息 ID；一条消息最多一条分类 |
| category | String(32) | False | 未声明 / 未声明 | 见表级约束 | inquiry/stop/product_change/implementation_repair |
| decision_summary | Text | False | 未声明 / 未声明 | 见表级约束 | 一句可审计的分类依据 |
| classifier_model | String(100) | False | 未声明 / 未声明 | 见表级约束 | 当时使用的模型名 |
| prompt_version | String(64) | False | 未声明 / 未声明 | 见表级约束 | 当时使用的 prompt 版本标识 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 分类时间 |

表级约束与索引（现有源码）：

- `CheckConstraint("category IN ('inquiry', 'stop', 'product_change', 'implementation_repair')", name='ck_project_message_classification_category')`
- `UniqueConstraint('message_id', name='uq_project_message_classification_message_id')`

## requirement_clarification

来源：[requirement_clarification.py](F:/project/ForgeAI/apps/backend/app/models/requirement_clarification.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| configuration_item_id | String(40) | False（主键） | 未声明 / 未声明 | ForeignKey('configuration_item.item_id', ondelete='CASCADE'); primary_key=True | 源码未声明列注释；用途见字段名及主方案 |
| task_id | String(40) | False | 未声明 / 未声明 | ForeignKey('task.task_id', ondelete='CASCADE') | 源码未声明列注释；用途见字段名及主方案 |
| answer_message_id | Integer | True（由 Optional 推导） | 未声明 / 未声明 | ForeignKey('project_message.id', ondelete='CASCADE') | 源码未声明列注释；用途见字段名及主方案 |
| followup_plan_id | String(40) | True（由 Optional 推导） | 未声明 / 未声明 | ForeignKey('plan.plan_id', ondelete='CASCADE') | 源码未声明列注释；用途见字段名及主方案 |

表级约束与索引（现有源码）：

- `UniqueConstraint('task_id', name='uq_clarification_task')`
- `UniqueConstraint('answer_message_id', name='uq_clarification_answer')`
- `UniqueConstraint('followup_plan_id', name='uq_clarification_plan')`
- `CheckConstraint('(answer_message_id IS NULL AND followup_plan_id IS NULL) OR (answer_message_id IS NOT NULL AND followup_plan_id IS NOT NULL)', name='ck_clarification_answer_plan')`

## task

来源：[task.py](F:/project/ForgeAI/apps/backend/app/models/task.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; autoincrement=True | 内部主键；对外请用 task_id |
| task_id | String(40) | False | 未声明 / 未声明 | 见表级约束 | 稳定任务标识，形如 task_<uuid> |
| plan_id | String(40) | False | 未声明 / 未声明 | ForeignKey('plan.plan_id', ondelete='CASCADE') | 所属计划的 plan_id；通过计划确定项目和运行 |
| task_key | String(64) | False | 未声明 / 未声明 | 见表级约束 | 调用方在计划内使用的唯一短名，保存时用于解析依赖 |
| position | Integer | False | 未声明 / 未声明 | 见表级约束 | 计划内从 1 开始的展示顺序；不是强制执行顺序 |
| recipient | String(32) | False | 未声明 / 未声明 | 见表级约束 | 唯一接收岗位：ProductManager 等 TaskRecipient 值 |
| title | String(200) | False | 未声明 / 未声明 | 见表级约束 | 任务标题 |
| instructions | Text | False | 未声明 / 未声明 | 见表级约束 | 本任务具体要完成的工作 |
| expected_output_type | String(32) | False | 未声明 / 未声明 | 见表级约束 | 预期 ConfigurationItem 语义类型；不是已生成结果 |
| input_configuration_item_ids | MutableList.as_mutable(JSON) | False | list / 未声明 | 见表级约束 | 已固定的准确输入 item_id；无输入时为空列表 |
| depends_on_task_ids | MutableList.as_mutable(JSON) | False | list / 未声明 | 见表级约束 | 同一计划内的直接依赖 task_id；不存模糊名称或最新版本引用 |
| status | String(16) | False | TaskStatus.PENDING.value / TaskStatus.PENDING.value | 见表级约束 | pending/running/succeeded/failed/cancelled；保存不会执行任务 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |
| updated_at | DateTime | False | func.now() / func.now() | onupdate=func.now() | 状态更新时间 |

表级约束与索引（现有源码）：

- `CheckConstraint('position > 0', name='ck_task_positive_position')`
- `CheckConstraint("recipient IN ('ProductManager', 'SolutionArchitect', 'SoftwareEngineer', 'QAEngineer')", name='ck_task_recipient')`
- `CheckConstraint("expected_output_type IN ('app_spec', 'system_design', 'code', 'test_report')", name='ck_task_expected_output_type')`
- `CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')", name='ck_task_status')`
- `UniqueConstraint('task_id', name='uq_task_task_id')`
- `UniqueConstraint('plan_id', 'task_key', name='uq_task_plan_key')`
- `UniqueConstraint('plan_id', 'position', name='uq_task_plan_position')`

## task_artifact

来源：[task_artifact.py](F:/project/ForgeAI/apps/backend/app/models/task_artifact.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; autoincrement=True | 内部主键 |
| task_id | String(40) | False | 未声明 / 未声明 | ForeignKey('task.task_id', ondelete='CASCADE'); index=True | 所属任务的稳定标识 |
| configuration_item_id | String(40) | False | 未声明 / 未声明 | ForeignKey('configuration_item.item_id', ondelete='CASCADE') | 附属产物的 ConfigurationItem 编号 |
| artifact_role | String(32) | False | 未声明 / 未声明 | 见表级约束 | 附属角色：system_design / test_report / other |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 登记时间 |

表级约束与索引（现有源码）：

- `CheckConstraint("artifact_role IN ('system_design', 'test_report', 'other')", name='ck_task_artifact_role')`
- `UniqueConstraint('configuration_item_id', name='uq_task_artifact_configuration_item')`
- `UniqueConstraint('task_id', 'artifact_role', 'configuration_item_id', name='uq_task_artifact')`

## task_execution

来源：[task_execution.py](F:/project/ForgeAI/apps/backend/app/models/task_execution.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| execution_id | String(40) | False（主键） | 未声明 / 未声明 | primary_key=True | 源码未声明列注释；用途见字段名及主方案 |
| task_id | String(40) | False | 未声明 / 未声明 | ForeignKey('task.task_id', ondelete='CASCADE') | 源码未声明列注释；用途见字段名及主方案 |
| attempt | Integer | False | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| status | String(16) | False | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| active_slot | Integer | True（由 Optional 推导） | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| started_at | DateTime | False | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| expires_at | DateTime | False | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| finished_at | DateTime | True（由 Optional 推导） | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| error | String(500) | True（由 Optional 推导） | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |
| draft | JSON(none_as_null=True) | True（由 Optional 推导） | 未声明 / 未声明 | 见表级约束 | 源码未声明列注释；用途见字段名及主方案 |

表级约束与索引（现有源码）：

- `UniqueConstraint('task_id', 'attempt', name='uq_task_execution_attempt')`
- `UniqueConstraint('task_id', 'active_slot', name='uq_task_execution_active')`
- `CheckConstraint('attempt > 0', name='ck_task_execution_attempt')`
- `CheckConstraint("(status = 'running' AND active_slot = 1 AND active_slot IS NOT NULL) OR (status IN ('failed', 'succeeded', 'superseded') AND active_slot IS NULL)", name='ck_task_execution_status')`

## task_result

来源：[task_result.py](F:/project/ForgeAI/apps/backend/app/models/task_result.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| task_id | String(40) | False（主键） | 未声明 / 未声明 | ForeignKey('task.task_id', ondelete='CASCADE'); primary_key=True | 产出任务的稳定标识；同时作为唯一键 |
| configuration_item_id | String(40) | False | 未声明 / 未声明 | ForeignKey('configuration_item.item_id', ondelete='CASCADE') | 本任务产出的正式成果编号 |
| result_hash | String(64) | False | 未声明 / 未声明 | 见表级约束 | 包含正文和来源的规范化提交 SHA-256；用于幂等校验 |
| source_message_ids | MutableList.as_mutable(JSON) | False | 未声明 / 未声明 | 见表级约束 | 模型实际使用的消息编号，按原顺序排列 |
| context_truncated | Boolean | False | 未声明 / 未声明 | 见表级约束 | 是否因上下文预算省略了较早历史 |
| model | String(100) | False | 未声明 / 未声明 | 见表级约束 | 生成结果使用的模型 |
| prompt_version | String(64) | False | 未声明 / 未声明 | 见表级约束 | 生成结果使用的提示词版本 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 登记时间 |

表级约束与索引（现有源码）：

- `UniqueConstraint('configuration_item_id', name='uq_task_result_configuration_item')`

## user

来源：[user.py](F:/project/ForgeAI/apps/backend/app/models/user.py)。复用现有表；拟修改字段以主方案为准。

| 字段 | 类型表达式 | 可空 | 默认值（应用 / DB） | 主键、索引、外键 | 说明 |
|---|---|---|---|---|---|
| id | Integer | False（主键） | 未声明 / 未声明 | primary_key=True; index=True; autoincrement=True | 内部自增主键 |
| username | String(100) | False | 未声明 / 未声明 | unique=True | 登录用户名，唯一 |
| hashed_password | String(255) | False | 未声明 / 未声明 | 见表级约束 | 密码哈希，不明文存储 |
| email | String(100) | False | 未声明 / 未声明 | index=True; unique=True | 邮箱，唯一 |
| avatar | String(255) | True | 未声明 / 未声明 | 见表级约束 | 头像 URL，可空 |
| created_at | DateTime | False | func.now() / func.now() | 见表级约束 | 创建时间 |
| updated_at | DateTime | False | func.now() / func.now() | onupdate=func.now() | 最后更新时间 |

表级约束与索引（现有源码）：


