# ForgeAI 工程 Agent：怎样把需求做成经过检查的真实应用

本文件补充主方案里过于粗略的“生成代码/自动修复”部分。它是拟实现设计，不代表现有能力；不修改产品架构不变量，也不授权自动发布。新增函数名为建议命名。当前前端存在其他本地修改，本次只补文档，不改动那些代码。

## 1. 先回答：现在究竟用了什么

**LangGraph：已经用了，但用于需求阶段。** 现有 [product_manager.py](F:/project/ForgeAI/apps/backend/app/orchestration/product_manager.py) 的 `build_product_manager_workflow()` 使用 `StateGraph`，串起创建计划、领取需求任务、调用模型、登记需求、等待批准和派工程任务。它没有实现工程编码工具循环。

**数据库状态机：已经有。** BuildRun、Plan、Task、TaskExecution记录任务状态、执行租约和恢复资格。现有图通过这些SQL服务恢复业务边界；`builder.compile()`没有传checkpointer，不能称为已接入LangGraph持久检查点。

**Agent：已有两个模型角色入口，但工程Agent仍待建。** ProjectManager做消息分类；ProductManager的`generate_app_spec()`明确是一次模型调用加结构校验。名字叫Agent不代表已经能自己反复调用读文件、改代码、测试工具。

**工具调用：现有LLM客户端还没支持。** [llm.py](F:/project/ForgeAI/apps/backend/app/core/llm.py) 的`chat_completion()`接收文本消息、可请求JSON结果，最终只返回content字符串；没有处理`tools/tool_calls/tool_call_id`。这一层必须先补。

**RAG：没有发现面向工程代码/文档的检索增强模块或向量检索实现。** 按确切ID读取已批准需求是已有业务数据加载，不等于已经建设了工程知识检索系统。

## 2. 这次把技术方案定清楚

采用：**LangGraph组织工程流程 + 现有SQL业务状态机 + 一个有工具的工程Agent循环 + 按需检索代码/文档 + 独立验收执行器。**

这里是职责组合：

- LangGraph决定下一步走模型决策、工具执行、验证、修复还是结束。
- SQL服务决定这个执行是否还有资格改文件、提交结果、更新当前版本。
- 工程Agent根据任务和真实工具结果选择动作，负责实现和修复。
- 检索模块把相关代码、依赖接口和已有错误证据交给Agent。
- 验收执行器运行受保护测试和浏览器流程，交付判定函数读真实证据决定能否完成。

首版不新增一套自制通用工作流引擎，不上向量数据库，不固定六个Agent。工程实现和独立评审可以使用同一个模型，但调用上下文和工具权限不同；这能减少直接自说自话，**不能保证两个调用不会犯相同错误**。最终仍要依靠实际检查。LangGraph区分固定工作流和动态工具Agent，本设计结合两种控制方式。[官方说明](https://docs.langchain.com/oss/python/langgraph/workflows-agents)

## 3. 第一步：让模型能提出工具调用

修改`apps/backend/app/core/llm.py`，新增`chat_with_tools()`，保留现有`chat_completion()`供需求阶段使用。

输入：system指令、当前任务、上下文、历史工具结果、允许工具的JSON Schema。输出用明确结构表示`content/tool_calls/usage/finish_reason/model`，不能再只返回一个字符串。

工具调用示意：

```json
{
  "tool_calls": [
    {
      "id": "call_17",
      "name": "read_file",
      "arguments": {"path": "backend/app/api/jobs.py", "start_line": 1, "end_line": 180}
    }
  ]
}
```

模型只是提出请求。平台解析参数、检查工具权限/路径/执行资格，然后真的读文件，将结果和同一个tool_call_id送回模型。

要处理：JSON不合法、重复字段、未知工具、缺参数、响应截断、模型输出为空、只返回工具调用而content为空、工具出错、429/超时、使用量缺失。不能沿用当前“content为空就失败”的规则，因为合法工具响应可以没有文本正文。

先针对配置的供应商做小型合同测试，确认工具协议、消息格式、usage、并行调用行为。不假定名字兼容就完全支持。若供应商不支持原生tool calling，备用方案是严格JSON动作协议；同样通过schema验证和工具执行器，不能从普通文本里正则抽shell直接执行。首版每次只执行一个写操作；并行读取不是首发必要条件。

## 4. 第二步：给Agent一份具体工程任务，而不是只说“做个网站”

新增`build_engineering_context()`和`plan_delivery()`。

每次执行的固定输入包括：获批app_spec ID、基础Revision或模板digest、应用栈、工具策略版本、验收要求和调用预算。它们写入输入快照，恢复时不能换成最新消息。

`plan_delivery()`按业务闭环拆工作单元。例如JobHub的“职位浏览”：

```text
work_item: jobs_browse
对应需求: feat_jobs_browse
需要交付:
  job_posting模型和迁移
  GET /api/v1/jobs与详情接口
  首页搜索、筛选、列表和详情页
验收:
  游客可以访问
  城市/类别/关键词筛选结果正确
  无结果有空态
  前端调用真实接口，刷新后内容来自数据库
```

工程工作单元是一个SoftwareEngineer任务内部的进度，不默认给每个页面再建一套顶层Plan/Task。它可放在Execution草稿中，工程任务的固定定义仍不可修改。执行中可以调整“先改哪个文件”，不能删除或改变已批准功能。

设计先确定数据库字段、接口请求响应、前端消费者和权限。建立需求ID→业务场景→API/UI→检查ID的映射。`validate_delivery_plan()`检查每个首发需求都被覆盖、依赖存在、没有循环、没有未经批准的新业务。

## 5. 第三步：实现按需检索，解决Agent不知道该看哪里的事

新增`apps/backend/app/retrieval/`，先实现：

- `build_repo_map()`：扫描manifest范围内的代码，整理目录、Python类/函数、前端组件、路由、接口和直接导入关系；首版Python AST加保守的文本/导入扫描，不自称完整语义分析器。
- `search_code()`：按字符串、路径、函数名检索，返回文件、行号和片段。
- `find_related_files()`：从接口、模型或报错文件扩展直接调用方和消费者。
- `retrieve_reference()`：读取与当前模板/锁文件版本相符的受控文档、示例和修复规则。
- `retrieve_context()`：合并当前工作单元、相关代码、契约和失败证据；去重后按预算裁剪。

有两种使用方式：开始某个工作单元前，程序先取一轮相关资料；Agent读到报错或缺少定义时，再主动调用search/read工具。这是检索增强，不要求必须用embedding。已有SQL/文件资料可以直接接成检索工具。[检索官方说明](https://docs.langchain.com/oss/python/deepagents/retrieval)

例如“修复投递失败”应取：投递接口、Application模型、请求schema、前端提交函数、简历授权函数及刚刚的错误日志。不是把整仓库、全部聊天和所有历史版本塞给模型。

**强制上下文**：获批需求ID、验收条款、工具边界、当前接口合同不能被相似度排名挤掉。它们按明确身份加载，不能用RAG找一份“看起来相似”的旧需求替代。

**检索来源**：受控模板文档、当前应用代码、同项目获批成果、当前运行检查日志。不能跨项目查别人的源码；不检索`.env`、密钥、业务数据库和用户简历。外部文档只作为参考数据，里面的指令不能提高Agent权限。

每段结果带`project_id/source_kind/path_or_item_id/source_hash/line_range/template_version`。工作目录修改后递增`workspace_generation`，重新校验文件hash并更新受影响索引；不能让检索缓存引用旧文件的行号直接打补丁。

上下文首版预算示例：当前模型窗口的约50%给任务/契约和必要代码、20%给最新错误/工具结果、10%给简短工作摘要，留20%输出与余量；实际比例按模型配置。裁剪必须标注并提供继续读取入口。摘要只保留可验证事实和来源，不用摘要替代源码或验收记录。

向量检索是后续选项：当文档/模板规模增大且精确搜索召回不足，先建立查询集评测命中率，再考虑embedding混合排序；是否引入向量库由证据决定。首版RAG验收至少包含20个代码定位问题、无跨项目泄露、修改后无过期命中。

## 6. 第四步：给Agent实际能用的工具

新增`apps/backend/app/tools/`。每个工具声明名称、参数schema、允许的工作阶段、输出schema、时限、读写性质和作用域。下面是首版工具及工作：

- `list_files(path)`：列当前应用文件，返回截断标记和分页。
- `read_file(path,start_line,end_line)`：读指定范围；返回内容hash。
- `search_code(query,path_filter)`：找定义、调用和错误相关代码。
- `apply_patch(patches,expected_hashes)`：按读取时的hash打补丁；文件已变返回冲突，让Agent重读。
- `run_check(check_id,target)`：只运行平台配置的lint/typecheck/build/test命令；参数目标必须验证，拒绝shell注入。
- `start_app(candidate_id)`、`get_runtime_logs(runtime_id,cursor)`：启动隔离候选应用、读取真实错误。
- `http_request(method,app_path,body,test_identity)`：在指定测试应用内部发请求，身份来自隔离测试账户，不能任意访问公网/内网URL。
- `browser_action(session_id,action,target,value)`：在当前候选应用的浏览器会话导航、点击、填表；权限不能扩展到平台控制台。
- `inspect_test_data(query_id,params)`：运行允许的测试断言查询，只读临时测试库；不允许任意生产SQL。
- `request_verification(work_item_ids)`：请求外部验收；Agent没有`mark_passed`、`publish`或`disable_checks`工具。

`execute_tool_call()`统一做：检查当前execution仍有效→验证参数和作用域→记录动作意图→执行→记录结果→返回证据引用。

结果示意：

```json
{
  "tool_call_id": "call_42",
  "ok": false,
  "exit_code": 1,
  "error_code": "API_CONTRACT_MISMATCH",
  "summary": "POST applications期望201，实际422；缺少resume_id",
  "evidence_ref": "evidence/check-042.json",
  "workspace_hash": "sha256:...",
  "duration_ms": 380,
  "truncated": false
}
```

失败结果要保留可诊断信息，不能全部压成“执行失败”。大日志和截图放制品存储，模型先看到精简摘要，按需读原文；日志先脱敏。不要求保存或展示模型内部思考过程。

生成应用和工具程序都在受限环境中运行。代码、安装脚本、测试都可能不可信，单靠允许`npm test`不保证安全，仍需容器/网络/磁盘/时间限制。

## 7. 第五步：用LangGraph把工程循环串起来

新增`apps/backend/app/orchestration/software_engineer.py`，实现`build_engineering_workflow()`。图节点按普通Python函数组装，使用已有httpx模型适配与自写的领域工具执行器，不必为此整体切换LangChain高层Agent框架。

建议状态只保存可序列化字段：

```text
project_id/run_id/task_id/execution_id
approved_spec_id/base_revision_id/template_digest
work_items/current_work_item_id
workspace_ref/workspace_generation/source_hash
design_item_id/acceptance_suite_hash
context_refs/pending_action_id/last_tool_result_ref
observations_summary/issues/check_result_refs
model_turns/tool_calls/repair_rounds/tokens_used/deadline
next_node/checkpoint_version
```

SQL Session、进程句柄、浏览器对象、完整大日志不能存进图state；只存引用。Graph state也不是项目最终状态的权威来源。

```text
prepare_execution
  → load_fixed_context
  → plan_delivery
  → prepare_acceptance
  → retrieve_context
  → decide_next_action（模型）
      ├─ read/search/patch/check → execute_tool → record_observation
      │                                      → retrieve_context → decide_next_action
      ├─ request_verification → run_acceptance（真实执行器）
      │                          ├─ 失败 → triage_issue → repair_context → decide_next_action
      │                          ├─ 当前单元通过 → next_work_item → retrieve_context
      │                          └─ 全单元完成 → final_clean_validation
      └─ 需求无法满足/预算耗尽/取消 → save_incomplete → END

final_clean_validation
  ├─ 失败且仍可修复 → triage_issue → decide_next_action
  ├─ 无法继续 → save_incomplete → END
  └─ 全部通过 → evaluate_delivery_gate（确定性规则）→ commit_revision → END
```

`decide_next_action()`必须返回受schema约束的动作：调用工具、请求验收、报告阻断。模型没有直接返回`succeeded`的权限。`route_next_step()`由程序根据动作类别、工具结果、验收结果、预算和租约决定图边；自然语言“我已经完成”不能直接进入commit。

各单元循环是“读取现场→实现一段完整业务→局部检查→继续”，不是让模型一口气输出整个工程后才查错。首版同工作目录一个工程写者；设计与评审采用不同调用上下文即可。将来拆多个工程Agent时必须分别在隔离分支工作、统一集成后重跑验收，不让多个Agent同时覆盖同一目录。

LangGraph图只是控制结构，不自动提供业务状态和工具副作用的一次执行保证。其checkpointer与store各有用途，内存检查点重启即丢失。[官方持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)

## 8. 第六步：把“自检”做成实际执行，不让AI自己打分就通过

### 8.1 写代码之前，先建立验收清单

新增`prepare_acceptance_suite()`，根据获批spec和设计合同生成检查：每条带criterion_id、前置数据、用户身份、步骤、预期响应/页面结果、数据库断言、所需证据。

通用检查由平台固定提供：启动、类型检查、迁移、身份隔离、文件私有、跨用户越权、持久化和无秘密导出。业务检查可以由独立评审调用生成草稿，再由schema/覆盖规则校验；它不能扩大或改写用户验收目标。

清单存放在平台控制的验收目录/制品中，工程Agent只读；真实用例由外部runner执行。选择器等实现适配可以调整，但必须保留语义断言；变更记录差异并重新校验，不能为了变绿删assert、skip测试、放宽预期。

### 8.2 第一层：代码结构和构建检查

`run_static_checks()`执行真实依赖安装、Python编译/lint、TypeScript检查、Vue build、路由导入、未定义引用检查。检查SQLAlchemy模型和Alembic升级是否一致。

这一层只证明工程基本可构建，不证明功能完成。检测到空函数、TODO、模拟数据可作为线索；不能把关键词扫描当业务正确性证明。

### 8.3 第二层：接口、数据库与权限检查

`run_api_checks()`启动真实FastAPI，在空SQLite测试库迁移，创建两个用户和职位，再调用HTTP接口。

例如投递：匿名401；登录成功投递201；数据库新增一条；重复投递409且仍一条；别人简历404；他人职位投递列表404；职位关闭不可投递；服务重启后投递还在。

不能用mock函数替代这个边界。测试库与运行实例的身份必须一一对应，不能读另一份fixture库当“写入成功”。

### 8.4 第三层：真实浏览器业务流程

`run_browser_checks()`通过Playwright在候选应用里实际点击和填写：注册→发布职位→另一个浏览器用户搜索→打开详情→上传安全测试PDF→确认投递→我的投递→招聘者查看投递。

采集DOM、按钮状态、控制台错误、失败网络请求、实际响应和关键截图。断言页面动作确实发出预期HTTP请求，并用测试数据库检查结果，防止UI只改内存列表就假装成功。

刷新和重启应用后再检查数据。验证空态、加载、错误提示、手机宽度。截图用于记录和辅助检查，不能代替点击和断言。

现有模型适配是文本，**不能声称它已经会看截图做视觉审查**。首版用DOM、布局尺寸和浏览器断言；若加视觉模型，单独验证多模态协议和费用，图像审查结果只作为补充证据。复杂审美/可访问性仍允许标记需要人工检查。

### 8.5 第四层：独立语义评审

`review_implementation()`用独立上下文读取获批要求、最终diff、接口合同、测试证据和权限代码，指出“已写但未覆盖”的地方。例如发布者校验遗漏、错误分支吞异常、页面按钮没有接后端、SQL迁移没有唯一约束。

评审只返回结构化问题：requirement_id、severity、file/line或证据引用、重现步骤、预期、实际。猜测标为待验证；新增的问题尽量变成可重现测试再交给工程Agent。

它没有修改最终验收记录的权限。另一个模型说“没问题”也不是完成证据；同模型不同角色调用不是形式验证或绝对独立审计。

### 8.6 最后一层：确定性交付门槛

`evaluate_delivery_gate()`由普通Python规则执行。必须同时满足：

1. 所有必选需求都有检查映射，不能仅因“测试全部绿”就忽略没写测试的功能。
2. 必需检查在**最终同一source_hash、同一验收清单版本**上执行，状态是passed；skipped/not_run/unknown不能算pass。
3. 构建、空库迁移、关键API、核心浏览器流程、权限和持久化检查通过。
4. 无未解决的阻断问题；证据失效或实际功能未测不能降级隐藏。
5. 源码/报告/模板/输入需求来源完整，当前execution有效、未取消、未超预算。
6. 用干净环境重新安装/启动通过，不能依赖Agent临时手工装在workspace之外的文件或包。

通过后才登记正式code主结果和test_report附属成果、提升Revision并返回“应用可预览”。自动发布仍不属于此门槛，正式Publish是另一个用户动作。

这是对明确需求和受控支持范围的交付判定，不是保证任意应用绝无bug。没有足够证据就保留未完成状态，显示具体缺口。

## 9. 第七步：发现错误后，Agent怎样修

新增`triage_failure()`和`build_repair_context()`。

失败先分类：

- 代码错误：编译失败、请求字段不一致、错误业务结果 → 定位相关代码，生成小补丁。
- 环境错误：依赖仓库不可达、模型429、浏览器启动失败 → 有界重试环境，不乱改业务代码。
- 测试适配错误：选择器过期、fixture非法 → 经独立验收层核实后修适配，不能删除断言。
- 产品目标冲突：获批要求互斥、必须凭据缺失 → 保存证据并提出一个具体待确认问题，不伪造功能。
- 安全/策略拒绝：工具越权、试图联网禁区、预算耗尽 → 停止对应动作，不能让Agent无限换命令绕过。

repair context只包含相关验收项、错误原文、复现步骤、失败请求/响应、有关文件片段及hash、最近补丁。每次修复后先跑受影响检查；最终交付前再跑所有必需检查。任何写文件都使旧验证对新source_hash失效，不能把前一版pass和后一版pass拼成一份“全通过”。

首版可配置预算示例：每个工作单元最多12次模型决策、30次工具调用；每个同类失败最多3次修复尝试；整个run最多60次模型调用、120次工具调用和30分钟墙钟时间。模型/工具token和磁盘预算另有限额。小样本评测后调节，不能把这些假设写死成产品保证；本补充取代主文档“整个构建15分钟/修复2轮”的粗略默认建议。

以`error_code+check_id+关键报错位置+source_hash变化`识别无进展循环；连续两次相同失败且相关文件未变化，强制换诊断方法或结束，不再照抄同一补丁。全run预算包含工具调用、重试、检索与模型输出；Graph recursion_limit只是意外死循环保护，不能拿它当计费/调用预算。

## 10. 第八步：程序中途崩了，怎样继续

首版沿用现有SQL恢复思路，不为LangGraph额外引入PostgreSQL，也不假装已有完整checkpointer。

`save_execution_checkpoint()`在`TaskExecution.draft`保存schema_version、next_node、当前工作单元、预算、workspace引用/hash、模型响应引用、pending_action_id和检查证据引用。只允许当前有效execution更新自己的草稿；恢复新execution时保留来源并重新验证输入和workspace。大内容放制品存储。

工程draft使用独立`kind=engineering_checkpoint`和schema_version，由工程工作流解析；现有PM的ProductManagerResult草稿格式不改，不让PM恢复代码误读工程checkpoint。验收清单作为system_design派生的有hash制品引用保存，模型/工具记录是执行证据，不另造第五种正式成果语义。

工具副作用比图节点更细，需要一份动作账本。建议新增`execution_action`（当前不存在），这是对主方案数据库设计的补充：

- action_id VARCHAR(40) PK，平台生成。
- execution_id VARCHAR(40) FK task_execution，非空；operation_key VARCHAR(100)非空，联合唯一(execution_id,operation_key)。
- model_tool_call_id VARCHAR(100)可空，仅关联模型请求，不作为权限。
- tool_name VARCHAR(64)、arguments_hash CHAR(64)、request_object_key VARCHAR(512)，均非空。
- status VARCHAR(16)：requested/running/succeeded/failed/unknown，默认requested。
- before_workspace_hash CHAR(64)、after_workspace_hash CHAR(64)、result_object_key VARCHAR(512)、error_code VARCHAR(80)，可空。
- started_at/finished_at DATETIME(6)可空；created_at DATETIME(6)非空默认DB当前UTC；索引(execution_id,status)。

执行规则：先保存已解析模型响应和工具动作→执行工具→保存结果→更新checkpoint。若工具已完成但checkpoint没更新，恢复时读取动作结果继续，不再调用同一步模型并重复写文件。

如果数据库没有收到完成记录，但外部动作可能已发生，标unknown并`reconcile_action()`检查现场：

- apply_patch比对前后hash；已应用返回原结果，不再叠加补丁。
- start_app用稳定operation_key查询runner现有实例，不重复启动。
- 检查任务在一次性测试库/进程中可重建；不对正式库重复跑任意迁移。
- 无法确认的动作不盲重放，保留失败与人工处理信息。

这里不承诺文件系统/数据库/容器之间恰好执行一次。实现目标是幂等、可对账和旧执行无法提交。恢复先让runner确认旧进程已经停止；否则只换execution_id不足以阻止旧进程继续写目录。

恢复到新execution时在checkpoint显式记录`recovered_from_execution_id`及`pending_action_id`，动作账本保留原归属、不迁移历史行。先核对原动作的输入快照、工作目录hash及已发生副作用，再决定复用结果或创建新动作；不能仅因为execution_id变了就把上一执行的未知操作重新做一遍。对旧证据的读取不恢复旧执行提交资格。

checkpoint可以帮助恢复步骤，但最终Project/Run/Task状态、所有权和成果登记仍通过现有SQL业务服务。后续若引入LangGraph持久checkpointer，应规定它只存执行快照，不建立另一套冲突的业务完成状态。

## 11. 用“简历投递”看一遍完整过程

1. 用户批准“登录后上传PDF投递职位；不能重复投递；求职者看自己的投递，招聘者看自己的职位收到的投递”。
2. 系统固定app_spec，设计Application/Resume表和API，独立验收清单加入匿名、成功、重复、越权、刷新持久化几个场景。
3. 工程Agent调用search_code查现有登录和存储模块；read_file读API和前端请求。
4. Agent调用apply_patch补投递表、迁移、事务服务、router和Vue弹窗；数据库唯一(job_id,applicant_id)。
5. run_check发现前端字段是resumeId、后端要求resume_id。工具返回真实422证据。
6. triage取回前后端相关文件；Agent修字段并重跑，201通过。
7. 并发投递测试发现一请求500。检查日志是IntegrityError未转换；Agent捕获唯一约束冲突，返回业务409，重跑确认只一条记录。
8. 浏览器流程发现弹窗成功后“我的投递”仍为空。检索发现前端缓存没失效；修刷新逻辑，真实浏览器重复完整流程。
9. 两用户检查发现招聘者能看另一职位投递。独立评审定位owner过滤缺失，加入拒绝用例，Agent修查询。
10. 重启应用，再查投递仍存在；干净环境重建通过；在最终同一源码hash上跑完所有必需检查。
11. `evaluate_delivery_gate()`确认覆盖与证据齐全，提交Revision和报告。用户看到可运行应用、版本及具体通过的场景。

第5–9项为说明机制的假设示例，不是声称当前仓库出现了这些JobHub缺陷；当前仓库没有该生成应用的业务实现。

## 12. 接下来具体新增什么文件、按什么顺序写

路径相对于`F:/project/ForgeAI`，均为建议新增或明确改造点。

1. **先补协议**：改`apps/backend/app/core/llm.py`，新增`schemas/agent_action.py`；实现`chat_with_tools()`、`parse_agent_action()`，写合法tool-only/非法JSON/超时/usage缺失合同测试。
2. **再补工具**：新增`tools/registry.py`、`tools/files.py`、`tools/checks.py`；实现`execute_tool_call()`、安全read/search/patch/run_check；先用脚本模型fixture验证循环。
3. **补恢复边界**：新增`models/execution_action.py`、Alembic迁移、`services/execution_checkpoint.py`；实现`record_action()`、`save_execution_checkpoint()`、`reconcile_action()`；测试每个副作用前后kill。
4. **补检索上下文**：新增`retrieval/repo_map.py`、`retrieval/context.py`；实现代码定位、版本过滤、上下文预算、写后索引失效；不买向量数据库。
5. **补工程Agent**：新增`agents/software_engineer.py`和对应prompt；实现`plan_delivery()`、`decide_next_action()`、`triage_failure()`。不能在prompt里硬编码“所有任务成功”。
6. **补图**：新增`orchestration/software_engineer.py`；实现LangGraph循环和`route_next_step()`，接已批准工程任务/Worker。图只管流程，业务事务走services。
7. **补验收层**：新增`validation/acceptance.py`、`validation/api.py`、`validation/browser.py`、`validation/gate.py`；实现受保护清单、真实测试、证据汇总和完成门槛。
8. **补独立评审**：新增`agents/reviewer.py`；实现`review_implementation()`，问题必须带来源或复现；评审不可自己改源码或写pass记录。
9. **接版本与预览**：使用主方案Revision/Runtime服务，把最终合格源码保存并启动；检查失败留在诊断/修复路径，不更新当前可用版本。
10. **验证闭环和泛化**：先用很小的“新增事项、查询事项、刷新仍在”的应用跑通真实循环，再跑JobHub和第二种业务应用。fixture验证编排，真实模型评测生成能力，两套都保留。

前述函数每个都应有输入schema、可重复测试的结果和异常合同，不能再只有一个`generate_app()`空壳调用。工程整体是一项顶层交付任务，内部工作单元和循环可变；不把当前建议节点/Agent数量固化成永久架构。

## 13. 对主方案与工时的修正

本补充细化原T14–T25、T30、T52–T56，并增加原来漏写的模型工具协议、检索失效、动作恢复、受保护验收和独立语义评审要求。**原106人日只能视为粗估，不能在此详细要求下继续当作已核定工期。**

先做一个验证增量：模型工具协议→安全read/patch/check→小应用工程循环→失败修复→受保护验收→kill后恢复。每项按0.5～2人日拆分后重估；用这个增量的工具次数、耗时、token、失败分布，再核定完整JobHub交付和上线工时。不把RAG/状态机/验证器当成几个函数名加上去就宣称已实现。

这份补充本次只做静态核对和设计，没有调用真实模型、运行工程Agent或执行浏览器/沙箱测试。它定义完成依据：在明确批准的功能范围里，真实程序和测试产生足够证据；AI负责发现问题和修复，程序控制是否具备交付资格。
