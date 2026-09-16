# ForgeAI 具体操作步骤（合并版）

说明：本文只保留具体操作步骤，不包含目标与范围、总体架构与关键规则、测试方案、迁移操作、API 定义、前端方案、权限安全清单、任务天数和排期。内容合并需求整理流程、工程 Agent 循环、生成/版本/预览/发布/导出步骤。

---

## 一、需求整理、批准与派工

1. 保存用户消息。
2. ProjectManager 把消息分类为 `product_change`。
3. 创建或复用同一项目的 queued BuildRun。
4. 针对指定消息建立初始 Plan 和需求 Task。
5. 领取 Task，登记唯一有效执行编号。
6. 模型调用期间不持有数据库事务。
7. ProductManager 生成经过结构校验的 `app_spec`，条目带稳定 ID。
8. 先保存执行草稿。
9. 在同一事务中保存正式成果、TaskResult，完成 Task、Plan 和本次执行。
10. 有可执行功能建议时，进入 `awaiting_approval`。
11. 仅当无法理解应用目标时，进入 `needs_user_input`。
12. 用户可补充说明，系统生成新的建议 `app_spec`，未改条目沿用原 ID。
13. 用户按条目 ID 勾选、编辑或新增后提交批准。
14. 删除或未勾选的条目不会进入批准版本。
15. 验收条件通过 `source_ids` 关联功能 ID，不能只靠正文相等猜测来源。
16. 只有未修改且仍选中的功能可以沿用原验收条件。
17. 新增、修改或不再有有效验收条件的功能，用户必须在清单内填写“怎样算完成”。
18. 批准接口通过对应功能条目的 `acceptance` 字段接收正文，不调用模型猜测，也不复制过期的验收标准。
19. 批准保存成功后，安排 SoftwareEngineer 的工程交付任务，输入为批准后的 `app_spec`。
20. 页面刷新只读取进度，不会再次触发模型或重复派工。

状态含义：

- `awaiting_approval`：等用户批准。
- `needs_user_input`：无法理解目标，需要用户补充。
- `design_pending`：工程交付任务已创建，等待处理。
- `ready_for_design`：已批准但派工未成功，可重试。

边界：

- 这一步只保存任务，不调用工程师模型、不生成代码、不代表应用完成。
- 未开始的旧 SolutionArchitect / `system_design` 待处理任务可幂等转换为工程交付任务。
- 尚无用户批准记录的旧任务先回到批准清单。
- 批准或补充需求时，取消旧待执行任务并在同一事务保存后续计划。
- 历史自动派工不视为用户同意。
- 工程计划和任务保持 `pending`，BuildRun 仍为 `running / pm`。
- 重复批准或派工只返回原结果。
- 若批准已保存但派工失败，页面显示 `ready_for_design`，可重试派工，不会重新调用需求模型。
- 历史 v1 字符串列表通过共享读取函数转换成带稳定 ID 的结构，不覆盖原产物、内容哈希和来源。
- 新批准与澄清结果写入 v2 并引用旧产物；未发布的 v1 执行草稿可恢复为 v2。未知版本继续拒绝。
- 附属产物必须属于任务所在的项目和生产运行；其他任务的主产物也不能被登记成附属产物。

---

## 二、工程 Agent 执行步骤

### 2.1 让模型能提出工具调用

1. 修改 `apps/backend/app/core/llm.py`。
2. 新增 `chat_with_tools()`，保留现有 `chat_completion()` 供需求阶段使用。
3. 输入：system 指令、当前任务、上下文、历史工具结果、允许工具的 JSON Schema。
4. 输出：明确结构，包含 `content / tool_calls / usage / finish_reason / model`，不能只返回一个字符串。
5. 模型只提出工具调用请求。
6. 平台解析参数、检查工具权限、路径、执行资格。
7. 平台真的执行工具，将结果和同一个 `tool_call_id` 送回模型。
8. 必须处理：JSON 不合法、重复字段、未知工具、缺参数、响应截断、模型输出为空、只返回工具调用而 content 为空、工具出错、429、超时、使用量缺失。
9. 不能沿用“content 为空就失败”的规则，因为合法工具响应可以没有文本正文。
10. 先针对配置的供应商做小型合同测试，确认工具协议、消息格式、usage、并行调用行为。
11. 不假定名字兼容就完全支持。
12. 若供应商不支持原生 tool calling，备用方案是严格 JSON 动作协议。
13. 备用方案同样通过 schema 验证和工具执行器，不能从普通文本里正则抽 shell 直接执行。
14. 首版每次只执行一个写操作；并行读取不是首发必要条件。

### 2.2 建立工程上下文与交付计划

1. 新增 `build_engineering_context()` 和 `plan_delivery()`。
2. 每次执行的固定输入包括：
   - 获批 `app_spec` ID
   - 基础 Revision 或模板 digest
   - 应用栈
   - 工具策略版本
   - 验收要求
   - 调用预算
3. 这些输入写入输入快照，恢复时不能换成最新消息。
4. `plan_delivery()` 按业务闭环拆工作单元。
5. 工程工作单元是 SoftwareEngineer 任务内部进度，不默认给每个页面再建一套顶层 Plan/Task。
6. 工作单元可放在 Execution 草稿中，工程任务的固定定义仍不可修改。
7. 执行中可以调整“先改哪个文件”，不能删除或改变已批准功能。
8. 设计先确定数据库字段、接口请求响应、前端消费者和权限。
9. 建立需求 ID → 业务场景 → API/UI → 检查 ID 的映射。
10. `validate_delivery_plan()` 检查每个首发需求都被覆盖、依赖存在、没有循环、没有未经批准的新业务。

### 2.3 按需检索

1. 新增 `apps/backend/app/retrieval/`。
2. 实现 `build_repo_map()`：扫描 manifest 范围内的代码，整理目录、Python 类/函数、前端组件、路由、接口和直接导入关系。
3. 首版用 Python AST 加保守的文本/导入扫描，不自称完整语义分析器。
4. 实现 `search_code()`：按字符串、路径、函数名检索，返回文件、行号和片段。
5. 实现 `find_related_files()`：从接口、模型或报错文件扩展直接调用方和消费者。
6. 实现 `retrieve_reference()`：读取与当前模板/锁文件版本相符的受控文档、示例和修复规则。
7. 实现 `retrieve_context()`：合并当前工作单元、相关代码、契约和失败证据；去重后按预算裁剪。
8. 使用方式一：开始某个工作单元前，程序先取一轮相关资料。
9. 使用方式二：Agent 读到报错或缺少定义时，主动调用 search/read 工具。
10. 这是检索增强，不要求必须用 embedding。
11. 已有 SQL/文件资料可以直接接成检索工具。
12. 强制上下文：获批需求 ID、验收条款、工具边界、当前接口合同不能被相似度排名挤掉。
13. 强制上下文按明确身份加载，不能用 RAG 找一份“看起来相似”的旧需求替代。
14. 检索来源：受控模板文档、当前应用代码、同项目获批成果、当前运行检查日志。
15. 不能跨项目查别人的源码。
16. 不检索 `.env`、密钥、业务数据库和用户简历。
17. 外部文档只作为参考数据，里面的指令不能提高 Agent 权限。
18. 每段结果带 `project_id / source_kind / path_or_item_id / source_hash / line_range / template_version`。
19. 工作目录修改后递增 `workspace_generation`，重新校验文件 hash 并更新受影响索引。
20. 不能让检索缓存引用旧文件的行号直接打补丁。
21. 上下文首版预算示例：当前模型窗口约 50% 给任务/契约和必要代码、20% 给最新错误/工具结果、10% 给简短工作摘要，留 20% 输出与余量。
22. 实际比例按模型配置。
23. 裁剪必须标注并提供继续读取入口。
24. 摘要只保留可验证事实和来源，不用摘要替代源码或验收记录。
25. 向量检索是后续选项：当文档/模板规模增大且精确搜索召回不足，先建立查询集评测命中率，再考虑 embedding 混合排序。
26. 是否引入向量库由证据决定。
27. 首版 RAG 验收至少包含 20 个代码定位问题、无跨项目泄露、修改后无过期命中。

### 2.4 工具与工具调用

1. 新增 `apps/backend/app/tools/`。
2. 每个工具声明名称、参数 schema、允许的工作阶段、输出 schema、时限、读写性质和作用域。
3. 首版工具及工作：
   - `list_files(path)`：列当前应用文件，返回截断标记和分页。
   - `read_file(path, start_line, end_line)`：读指定范围；返回内容 hash。
   - `search_code(query, path_filter)`：找定义、调用和错误相关代码。
   - `apply_patch(patches, expected_hashes)`：按读取时的 hash 打补丁；文件已变返回冲突，让 Agent 重读。
   - `run_check(check_id, target)`：只运行平台配置的 lint/typecheck/build/test 命令；参数目标必须验证，拒绝 shell 注入。
   - `start_app(candidate_id)`：启动隔离候选应用。
   - `get_runtime_logs(runtime_id, cursor)`：读取真实错误。
   - `http_request(method, app_path, body, test_identity)`：在指定测试应用内部发请求，身份来自隔离测试账户，不能任意访问公网/内网 URL。
   - `browser_action(session_id, action, target, value)`：在当前候选应用的浏览器会话导航、点击、填表；权限不能扩展到平台控制台。
   - `inspect_test_data(query_id, params)`：运行允许的测试断言查询，只读临时测试库；不允许任意生产 SQL。
   - `request_verification(work_item_ids)`：请求外部验收。
4. Agent 没有 `mark_passed`、`publish` 或 `disable_checks` 工具。
5. `execute_tool_call()` 统一做：
   - 检查当前 execution 仍有效。
   - 验证参数和作用域。
   - 记录动作意图。
   - 执行。
   - 记录结果。
   - 返回证据引用。
6. 失败结果要保留可诊断信息，不能全部压成“执行失败”。
7. 大日志和截图放制品存储，模型先看到精简摘要，按需读原文；日志先脱敏。
8. 不要求保存或展示模型内部思考过程。
9. 生成应用和工具程序都在受限环境中运行。
10. 代码、安装脚本、测试都可能不可信，单靠允许 `npm test` 不保证安全，仍需容器、网络、磁盘、时间限制。

### 2.5 LangGraph 工程循环

1. 新增 `apps/backend/app/orchestration/software_engineer.py`。
2. 实现 `build_engineering_workflow()`。
3. 图节点按普通 Python 函数组装，使用已有 httpx 模型适配与自写的领域工具执行器，不必整体切换 LangChain 高层 Agent 框架。
4. 建议状态只保存可序列化字段：
   - `project_id/run_id/task_id/execution_id`
   - `approved_spec_id/base_revision_id/template_digest`
   - `work_items/current_work_item_id`
   - `workspace_ref/workspace_generation/source_hash`
   - `design_item_id/acceptance_suite_hash`
   - `context_refs/pending_action_id/last_tool_result_ref`
   - `observations_summary/issues/check_result_refs`
   - `model_turns/tool_calls/repair_rounds/tokens_used/deadline`
   - `next_node/checkpoint_version`
5. SQL Session、进程句柄、浏览器对象、完整大日志不能存进图 state；只存引用。
6. Graph state 也不是项目最终状态的权威来源。
7. 流程：
   - `prepare_execution`
   - `load_fixed_context`
   - `plan_delivery`
   - `prepare_acceptance`
   - `retrieve_context`
   - `decide_next_action`（模型）
     - 读/搜/打补丁/检查 → 执行工具 → 记录观察 → 再检索 → 再决策
     - 请求验收 → 跑真实验收
       - 失败 → 分类 → 修复上下文 → 再决策
       - 当前单元通过 → 下一个工作单元 → 再检索
       - 全单元完成 → 最终干净验证
     - 需求无法满足/预算耗尽/取消 → 保存未完成 → 结束
   - `final_clean_validation`
     - 失败且仍可修复 → 分类 → 再决策
     - 无法继续 → 保存未完成 → 结束
     - 全部通过 → 确定性交付门槛 → 提交 Revision → 结束
8. `decide_next_action()` 必须返回受 schema 约束的动作：调用工具、请求验收、报告阻断。
9. 模型没有直接返回 `succeeded` 的权限。
10. `route_next_step()` 由程序根据动作类别、工具结果、验收结果、预算和租约决定图边。
11. 自然语言“我已经完成”不能直接进入 commit。
12. 各单元循环是“读取现场 → 实现一段完整业务 → 局部检查 → 继续”，不是让模型一口气输出整个工程后才查错。
13. 首版同工作目录一个工程写者；设计与评审采用不同调用上下文即可。
14. 将来拆多个工程 Agent 时必须分别在隔离分支工作、统一集成后重跑验收，不让多个 Agent 同时覆盖同一目录。
15. LangGraph 图只是控制结构，不自动提供业务状态和工具副作用的一次执行保证。
16. 其 checkpointer 与 store 各有用途，内存检查点重启即丢失。

### 2.6 验收清单准备

1. 新增 `prepare_acceptance_suite()`。
2. 根据获批 spec 和设计合同生成检查。
3. 每条带：criterion_id、前置数据、用户身份、步骤、预期响应/页面结果、数据库断言、所需证据。
4. 通用检查由平台固定提供：启动、类型检查、迁移、身份隔离、文件私有、跨用户越权、持久化、无秘密导出。
5. 业务检查可以由独立评审调用生成草稿，再由 schema/覆盖规则校验。
6. 业务检查不能扩大或改写用户验收目标。
7. 清单存放在平台控制的验收目录/制品中，工程 Agent 只读。
8. 真实用例由外部 runner 执行。
9. 选择器等实现适配可以调整，但必须保留语义断言。
10. 变更记录差异并重新校验，不能为了变绿删 assert、skip 测试、放宽预期。

### 2.7 验收执行

#### 第一层：代码结构和构建检查

1. `run_static_checks()` 执行真实依赖安装、Python 编译/lint、TypeScript 检查、Vue build、路由导入、未定义引用检查。
2. 检查 SQLAlchemy 模型和 Alembic 升级是否一致。
3. 这一层只证明工程基本可构建，不证明功能完成。
4. 检测到空函数、TODO、模拟数据可作为线索；不能把关键词扫描当业务正确性证明。

#### 第二层：接口、数据库与权限检查

1. `run_api_checks()` 启动真实 FastAPI。
2. 在空 SQLite 测试库迁移。
3. 创建两个用户和职位。
4. 调用 HTTP 接口。
5. 例如投递：
   - 匿名 401。
   - 登录成功投递 201。
   - 数据库新增一条。
   - 重复投递 409 且仍一条。
   - 别人简历 404。
   - 他人职位投递列表 404。
   - 职位关闭不可投递。
   - 服务重启后投递还在。
6. 不能用 mock 函数替代这个边界。
7. 测试库与运行实例的身份必须一一对应，不能读另一份 fixture 库当“写入成功”。

#### 第三层：真实浏览器业务流程

1. `run_browser_checks()` 通过 Playwright 在候选应用里实际点击和填写。
2. 流程：注册 → 发布职位 → 另一个浏览器用户搜索 → 打开详情 → 上传安全测试 PDF → 确认投递 → 我的投递 → 招聘者查看投递。
3. 采集 DOM、按钮状态、控制台错误、失败网络请求、实际响应和关键截图。
4. 断言页面动作确实发出预期 HTTP 请求，并用测试数据库检查结果，防止 UI 只改内存列表就假装成功。
5. 刷新和重启应用后再检查数据。
6. 验证空态、加载、错误提示、手机宽度。
7. 截图用于记录和辅助检查，不能代替点击和断言。
8. 现有模型适配是文本，不能声称它已经会看截图做视觉审查。
9. 首版用 DOM、布局尺寸和浏览器断言。
10. 若加视觉模型，单独验证多模态协议和费用，图像审查结果只作为补充证据。
11. 复杂审美/可访问性仍允许标记需要人工检查。

#### 第四层：独立语义评审

1. `review_implementation()` 用独立上下文读取获批要求、最终 diff、接口合同、测试证据和权限代码。
2. 指出“已写但未覆盖”的地方。
3. 例如发布者校验遗漏、错误分支吞异常、页面按钮没有接后端、SQL 迁移没有唯一约束。
4. 评审只返回结构化问题：requirement_id、severity、file/line 或证据引用、重现步骤、预期、实际。
5. 猜测标为待验证。
6. 新增的问题尽量变成可重现测试再交给工程 Agent。
7. 它没有修改最终验收记录的权限。
8. 另一个模型说“没问题”也不是完成证据；同模型不同角色调用不是形式验证或绝对独立审计。

#### 最后一层：确定性交付门槛

`evaluate_delivery_gate()` 由普通 Python 规则执行。必须同时满足：

1. 所有必选需求都有检查映射，不能仅因“测试全部绿”就忽略没写测试的功能。
2. 必需检查在最终同一 `source_hash`、同一验收清单版本上执行，状态是 passed；skipped/not_run/unknown 不能算 pass。
3. 构建、空库迁移、关键 API、核心浏览器流程、权限和持久化检查通过。
4. 无未解决的阻断问题；证据失效或实际功能未测不能降级隐藏。
5. 源码/报告/模板/输入需求来源完整，当前 execution 有效、未取消、未超预算。
6. 用干净环境重新安装/启动通过，不能依赖 Agent 临时手工装在 workspace 之外的文件或包。

通过后才登记正式 code 主结果和 test_report 附属成果、提升 Revision 并返回“应用可预览”。自动发布仍不属于此门槛，正式 Publish 是另一个用户动作。

这是对明确需求和受控支持范围的交付判定，不是保证任意应用绝无 bug。没有足够证据就保留未完成状态，显示具体缺口。

### 2.8 失败修复

1. 新增 `triage_failure()` 和 `build_repair_context()`。
2. 失败先分类：
   - 代码错误：编译失败、请求字段不一致、错误业务结果 → 定位相关代码，生成小补丁。
   - 环境错误：依赖仓库不可达、模型 429、浏览器启动失败 → 有界重试环境，不乱改业务代码。
   - 测试适配错误：选择器过期、fixture 非法 → 经独立验收层核实后修适配，不能删除断言。
   - 产品目标冲突：获批要求互斥、必须凭据缺失 → 保存证据并提出一个具体待确认问题，不伪造功能。
   - 安全/策略拒绝：工具越权、试图联网禁区、预算耗尽 → 停止对应动作，不能让 Agent 无限换命令绕过。
3. repair context 只包含相关验收项、错误原文、复现步骤、失败请求/响应、有关文件片段及 hash、最近补丁。
4. 每次修复后先跑受影响检查；最终交付前再跑所有必需检查。
5. 任何写文件都使旧验证对新 `source_hash` 失效，不能把前一版 pass 和后一版 pass 拼成一份“全通过”。
6. 首版可配置预算示例：
   - 每个工作单元最多 12 次模型决策、30 次工具调用。
   - 每个同类失败最多 3 次修复尝试。
   - 整个 run 最多 60 次模型调用、120 次工具调用和 30 分钟墙钟时间。
   - 模型/工具 token 和磁盘预算另有限额。
7. 小样本评测后调节，不能把这些假设写死成产品保证。
8. 以 `error_code + check_id + 关键报错位置 + source_hash 变化` 识别无进展循环。
9. 连续两次相同失败且相关文件未变化，强制换诊断方法或结束，不再照抄同一补丁。
10. 全 run 预算包含工具调用、重试、检索与模型输出。
11. Graph recursion_limit 只是意外死循环保护，不能拿它当计费/调用预算。

### 2.9 崩溃恢复与动作账本

1. 首版沿用现有 SQL 恢复思路，不为 LangGraph 额外引入 PostgreSQL，也不假装已有完整 checkpointer。
2. `save_execution_checkpoint()` 在 `TaskExecution.draft` 保存：
   - schema_version
   - next_node
   - 当前工作单元
   - 预算
   - workspace 引用/hash
   - 模型响应引用
   - pending_action_id
   - 检查证据引用
3. 只允许当前有效 execution 更新自己的草稿。
4. 恢复新 execution 时保留来源并重新验证输入和 workspace。
5. 大内容放制品存储。
6. 工程 draft 使用独立 `kind=engineering_checkpoint` 和 schema_version，由工程工作流解析。
7. 现有 PM 的 ProductManagerResult 草稿格式不改，不让 PM 恢复代码误读工程 checkpoint。
8. 验收清单作为 system_design 派生的有 hash 制品引用保存。
9. 模型/工具记录是执行证据，不另造第五种正式成果语义。
10. 工具副作用比图节点更细，需要一份动作账本。
11. 建议新增 `execution_action`，这是对主方案数据库设计的补充：
    - action_id VARCHAR(40) PK，平台生成。
    - execution_id VARCHAR(40) FK task_execution，非空。
    - operation_key VARCHAR(100) 非空，联合唯一 `(execution_id, operation_key)`。
    - model_tool_call_id VARCHAR(100) 可空，仅关联模型请求，不作为权限。
    - tool_name VARCHAR(64)、arguments_hash CHAR(64)、request_object_key VARCHAR(512)，均非空。
    - status VARCHAR(16)：requested/running/succeeded/failed/unknown，默认 requested。
    - before_workspace_hash CHAR(64)、after_workspace_hash CHAR(64)、result_object_key VARCHAR(512)、error_code VARCHAR(80)，可空。
    - started_at/finished_at DATETIME(6) 可空；created_at DATETIME(6) 非空默认 DB 当前 UTC；索引 `(execution_id, status)`。
12. 执行规则：先保存已解析模型响应和工具动作 → 执行工具 → 保存结果 → 更新 checkpoint。
13. 若工具已完成但 checkpoint 没更新，恢复时读取动作结果继续，不再调用同一步模型并重复写文件。
14. 如果数据库没有收到完成记录，但外部动作可能已发生，标 unknown 并 `reconcile_action()` 检查现场：
    - `apply_patch` 比对前后 hash；已应用返回原结果，不再叠加补丁。
    - `start_app` 用稳定 operation_key 查询 runner 现有实例，不重复启动。
    - 检查任务在一次性测试库/进程中可重建；不对正式库重复跑任意迁移。
    - 无法确认的动作不盲重放，保留失败与人工处理信息。
15. 这里不承诺文件系统/数据库/容器之间恰好执行一次。
16. 实现目标是幂等、可对账和旧执行无法提交。
17. 恢复先让 runner 确认旧进程已经停止；否则只换 execution_id 不足以阻止旧进程继续写目录。
18. 恢复到新 execution 时在 checkpoint 显式记录 `recovered_from_execution_id` 及 `pending_action_id`。
19. 动作账本保留原归属、不迁移历史行。
20. 先核对原动作的输入快照、工作目录 hash 及已发生副作用，再决定复用结果或创建新动作。
21. 不能仅因为 execution_id 变了就把上一执行的未知操作重新做一遍。
22. 对旧证据的读取不恢复旧执行提交资格。
23. checkpoint 可以帮助恢复步骤，但最终 Project/Run/Task 状态、所有权和成果登记仍通过现有 SQL 业务服务。
24. 后续若引入 LangGraph 持久 checkpointer，应规定它只存执行快照，不建立另一套冲突的业务完成状态。

---

## 三、生成、版本、预览、发布、导出

### 3.1 工程交付处理流程

```text
短事务 claim：
  校验项目、run、plan、task 的关联与状态
  校验 task.recipient == SoftwareEngineer 且 input 为已批准 app_spec
  冻结 base_revision/template/输入 item IDs；创建 execution_id 和租约
  设置 running；提交事务

事务外执行：
  复制明确 base_revision 至隔离工作目录（首次则使用固定模板）
  从 approved app_spec 派生 system_design；登记附属成果
  在策略内生成/修改源码；计算不可变 manifest
  登记精确 code identity（还未标用户可用）
  静态/接口/业务检查 → 保存绑定该 code 的 test_report
  失败需要修复时：新候选 code/report，不修改旧报告或已发布 code
  通过静态/API检查后先登记候选 Revision（满足 runtime_instance 的外键）
  候选尚不标为用户可用；启动候选 runtime 并做健康与浏览器 smoke

短事务 commit：
  重验 execution 未过期、未 superseded、未取消、仍拥有当前 generation
  校验制品、上下游 item IDs、测试 code hash 一致
  登记正式 TaskResult；确认候选 Revision 可用并更新环境预览指针
  结束 task/plan/run；Project.available；释放 active_slot
```

说明：

1. TaskResult 仍只记录一次正式主结果 code。
2. design/test_report 经 TaskArtifact 关联。
3. 中间失败候选保留为不可用成果或单独候选制品，不能覆写一份可用成果。
4. 如果当前任务完成函数会在 code 登记时结束工程任务，应新增“候选登记/最终提交”边界并同步测试，不能绕过它直接多次写 TaskResult。
5. 候选 Revision 的可用性从 `producer_run.status=succeeded`、正式 TaskResult 和报告关联共同判定，不能只看表中有一行。
6. 对外版本列表默认只显示通过最终提交的版本；失败候选仅在该运行的诊断中显示。
7. Revision 指向不可变静态/API 检查报告。
8. 随后 runtime 健康与浏览器 smoke 以同 code hash 的附属检查证据/RunEvent 记录。
9. 需要汇总报告时新建报告成果，禁止改写已保存报告。
10. 发布服务必须检查最终交付资格，不能发布未完成的候选。

### 3.2 版本与预览

1. 短事务领取任务，冻结输入，创建执行和租约。
2. 事务外复制基础版本到隔离目录。
3. 派生设计，登记设计成果。
4. 生成或修改源码，计算不可变清单。
5. 登记代码身份，但还不标记用户可用。
6. 运行静态、接口、业务检查，保存绑定该代码的报告。
7. 失败则生成新候选代码和报告，不修改旧报告或已发布代码。
8. 通过检查后登记候选版本。
9. 启动候选运行实例，做健康检查和浏览器冒烟。
10. 短事务提交：重新校验执行有效、未过期、未取消。
11. 校验制品、上下游成果、测试代码哈希一致。
12. 登记正式任务结果，确认候选版本可用，更新环境预览指针。
13. 结束任务、计划、运行，项目标记可用，释放构建名额。

### 3.3 发布与回滚

1. 用户选择通过报告的 Revision 和 `expected_generation`。
2. 写 Deployment(queued)，唯一 active_slot 保证同环境一个发布。
3. 部署 worker 取得租约。
4. 确认制品 hash、迁移兼容、可用容量。
5. 用测试库副本做启动验证。
6. 无数据迁移：短时维护，停旧实例 → 转移 writer_slot → 启新实例 → 检查 → 切路由。P0 不承诺零停机。
7. 有数据迁移：暂停业务写 → SQLite 在线备份 → 停止旧应用 → 单次迁移 → 启新应用 → 验证 → CAS 更新 current_revision 和 generation。
8. 健康检查失败：若旧代码兼容当前 schema，回启旧 Revision 并记录失败；若不兼容，保持维护态并选择经过演练的数据恢复或前向修复。
9. 不能自动恢复旧数据库覆盖上线后的新投递。
10. 恢复备份是独立数据操作，必须说明丢失窗口并取得该操作批准；普通代码回滚无权隐式恢复数据。
11. 每次路由切换与 runner 命令携带 deployment_id/lease_generation。
12. 断线后 reconcile 读取实际状态并补账，不重复跑迁移。

### 3.4 导出

1. 用户对指定 Revision 发起导出。
2. 服务端按白名单打包 ZIP。
3. 必须包含：
   - `frontend/`
   - `backend/app/`
   - `backend/alembic/`
   - 依赖声明与锁文件
   - 模板/运行版本信息
   - 无秘密的配置示例
   - 启动说明
   - 只含演示数据的可选 seed
4. 明确排除：
   - `.env`
   - 平台配置
   - token
   - 真实 SQLite 库
   - 上传的简历
   - 对象签名 URL
   - 缓存
   - `node_modules`
   - 内部任务目录
5. 允许导出的公共图片按 manifest 随包携带，并保留来源/许可信息。
6. 生成应用的文件接口使用可替换 storage adapter：
   - 平台运行选择限定 app scope 的文件网关。
   - 独立导出默认使用本地私有存储 adapter。
   - 生产自托管可配置 S3。
7. 应用认证/ORM/迁移在导出包内，不依赖平台登录、平台数据库或专有 Web SDK。
8. README 说明扫描器和私有存储的生产配置。
9. 安全检查未配置时不能把文件上传宣称为生产就绪。
10. 必须在没有平台凭据的干净环境验证登录、发职位和投递，不能只验证前端页面能打开。

---

## 四、整体主流程

```text
用户描述
→ 系统分类消息
→ 创建运行、计划、需求任务
→ 生成需求清单
→ 用户批准
→ 创建工程交付任务
→ 工程 Agent 领取任务
→ 冻结输入
→ 建立工程上下文和交付计划
→ 准备验收清单
→ 检索相关代码和文档
→ 循环：决策 → 工具调用 → 观察 → 再决策
→ 运行静态检查
→ 运行接口、数据库、权限检查
→ 运行真实浏览器流程
→ 独立语义评审
→ 确定性交付门槛
→ 失败则分类修复，重跑受影响检查
→ 通过则登记正式成果和版本
→ 启动预览
→ 用户选择版本发布
→ 发布健康检查
→ 切换线上指针
→ 用户可导出完整工程
```

异常：任何一步失败，保存错误和证据，保留最后可用版本，显式重试。

更新：新消息先分类；产品变更先批准新意图，实现修复沿用原意图；基于指定旧版本构建新版本；验证后预览；不自动替换线上。

