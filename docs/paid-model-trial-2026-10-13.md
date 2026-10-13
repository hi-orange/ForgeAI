# 付费模型全链路现场观察（2026-10-13）

## 已批准的修复目标

用户随后要求全链条修复。保留以下原始试跑证据，新增实现与验证记录单独追加。
本次修复目标：安全执行模型提出的只读批次；有效的新上下文和计划推进不被当作空转；
PM/Architect 中间工具进度可恢复且累计预算不重置；模型响应即使解析失败也保留用量和诊断；
供应商失败使用服务端错误语义；恢复扫描不锁住无关活跃工作；暂停的原因与恢复入口在界面明确可见；
启动时服务尚未就绪不清除有效登录。修复不自动审批、不自动重开已停止的付费执行。

以下现场观察阶段由用户自行启动与操作，监测只读、不替用户审批、不触发额外模型调用。后续修复阶段已获用户授权，记录见文末。时间线采用北京时间；TaskExecution 的数据库时间为 UTC，展示时加 8 小时，其他表使用数据库本地时间，不能直接混排原始时间。

## 本次对象与证据

- 项目：2；运行：`run_c48eecd9e349474093766f564ea4e516`。
- 实际模型响应标识：`deepseek-flash`；未读取或记录 API 密钥。
- 来源：当前应用终端、`apps/backend/logs/forgeai.log`、数据库 SELECT 快照和对应代码。终端缓冲区可能截断，文件日志优先核对。
- 数据库只读快照助手：`apps/backend/output/monitor_paid_run.py`（本地忽略目录，不属于产品功能）。
- 未将此前自动化测试、smoke、截图或模型结论作为本次网站逐项验收证据。

## 时间线

| 北京时间           | 事实                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 22:13:47           | Vite 代理 `/api/v1/auth/me` 出现 ECONNREFUSED；后端尚未完成启动。                                                                                            |
| 22:13:54–55        | 后端、两个 embedded worker 启动完成；随后登录和项目请求成功。                                                                                                |
| 22:14:55           | 项目 2 开始本次运行，消息分类响应成功，907 tokens。                                                                                                          |
| 22:15:01、22:15:13 | PM 依次收到 search_product_context、edit_prd 模型响应。                                                                                                      |
| 22:15:18           | SSL UNEXPECTED_EOF_WHILE_READING；requirements/start 返回 HTTP 400。PM attempt 1 失败，draft 为 NULL。                                                       |
| 22:15:24–22:16:05  | 用户侧 continue 请求触发 PM attempt 2；重新搜索、编辑并最终 write_prd；请求成功。                                                                            |
| 22:17:02–28        | PRD 审批后 Leader 读取上下文、分类、读计划、建计划、派任务、结束回合；审批请求成功。                                                                         |
| 22:17:33           | Architect attempt 1 开始。                                                                                                                                   |
| 22:18:47–48        | Architect 因“工具参数不是合法 JSON”失败；保存 checkpoint。                                                                                                   |
| 22:18:50           | 同一个 architect job 自动开始 attempt 2；再次读取 artifact 和多个文件。                                                                                      |
| 22:19:30 快照      | Run 仍为 running/architect，job attempt 2 running；尚无 system_design/code/test_report 产物。                                                                |
| 22:20:13           | Architect attempt 2 因模型连接错误失败；22:20:17 自动开始 attempt 3。                                                                                        |
| 22:21:42–43        | 架构第三次执行成功，发布 system_design `ci_d6f544f1d49a4f4d818c0526bfff2d0a` 和 acceptance_test_plan `ci_ebde822dc1fa4bdb8acd4ef605f9b9fa`；进入 developer。 |
| 22:22:45           | Code Engineer attempt 1 因 workflow_event INSERT 死锁及 SAVEPOINT 回滚错误失败。                                                                             |
| 22:22:47           | Code Engineer attempt 2 恢复，继续读取文件。                                                                                                                 |
| 22:23:20–21        | 第二次工程执行触发 NO_SOURCE_PROGRESS，job/execution 失败，要求用户显式继续。                                                                                |
| 22:24:34 快照      | 用户反馈“好像卡住了”；Run 仍显示 running/developer，但工程 job 已失败，无继续执行的 worker job。                                                             |

## 已确认的问题与待核实项

### OBS-01：模型连接中断被映射为 HTTP 400

- 已确认运行事实：22:15:18 SSL EOF 后启动接口返回 400；恢复后 PM 完成。
- 代码：`app/core/exceptions.py` 的 ProviderException 继承 BusinessException，固定使用 400。
- 影响：基础设施/上游错误表现成客户端业务错误，不利于界面提示、故障分类和重试策略统一。
- SSL 中断的具体网络/供应商原因未知，不归因于用户需求或生成代码。

### OBS-02：PM 中间草稿没有持久化，恢复重复付费工作

- 已确认：首次 edit_prd 响应后遇到网络异常；对应 `exec_29eda8dbb5e34930b7d7ea78e1ab57e5` 的 draft 为 NULL。
- 代码：`app/tools/product_manager.py` 的 edit_prd 仅设置内存 state.draft，却返回“已保存 PRD 草稿”；`app/orchestration/product_manager.py` 仅在完整结果返回后调用 save_execution_draft。
- 下一次 `exec_b1c7e47940684b43ae68d6c3a77e8a8f` 重新 search_product_context 和 edit_prd。中间工作未形成可恢复检查点；“保存”表述容易误导。
- 首次失败执行的数据库 error 只保留通用“需求整理未完成…”提示，具体 SSL 原因需依赖文件日志。

### OBS-03：Architect 工具 JSON 解析失败触发整次执行恢复

- 已确认：`exec_f6bba2d1d55b41d28fd7f218dad98aa2` 因工具参数非法 JSON 失败，job `job_26587c5607a444c5ac47bf08e93efd76` 自动进入第二次执行 `exec_6e82102faf3547398ecb6834b8005f21`。
- 第二次又读取 artifact、terminal_list 和多个 editor_read；需继续核对 checkpoint 的恢复粒度及模型请求重复成本。
- 暂无原始失败响应证据，不能断言是模型截断、提示词或具体 schema 错误。

### OBS-04：计费与追踪完整性待核实

- 22:19:30 附近快照：PM 5 条使用记录共 34,404 tokens，Architect 10 条共 86,681 tokens；Leader 仅一条分类记录 907 tokens、build_run_id NULL。
- 文件日志另有 22:17:04–28 六条 Leader 工具响应；项目 2 的使用表未见对应记录。需核对调用上下文的统计入口，不能把已记录总和作为本次账单总额。
- estimated_cost_microusd 均为 NULL：无费用估算，不能解释为免费或零费用。
- 代码已确认：`app/services/leader.py:dispatch_approved_requirements` 直接调用 lead_project_turn，未建立 agent_telemetry_scope；无上下文时 record_model_turn 直接返回。审批后的六次 Leader 响应总计 53,715 tokens 未出现在项目使用表中。
- 另一个计量缺口：`app/core/llm.py:chat_with_tools` 先 parse_agent_action，再记录 usage 和成功日志；非法 JSON 响应在计量前抛出，无法从现有记录还原失败响应的 tokens/finish_reason。
- 22:24 快照已记录：PM 34,404 + Architect 178,989 + Code Engineer 226,659 + 初始分类 907 = 440,959 tokens；加上日志中遗漏的六次 Leader 响应为 494,674 tokens。这是观测到的累计用量，不是供应商账单，仍不包含无法计量的失败响应。

### OBS-05：启动可用性短暂竞态

- 前端先就绪，后端约 8 秒后才可用；auth/me 的代理请求短暂失败，随后正常。
- 记录为开发启动体验问题，目前未见它阻断本次模型生成。

### 观察纠正

- 终端快照一度在 `error=` 处截断；完整文件日志含“工具参数不是合法 JSON”，因此不将“日志为空”列为产品缺陷。

### OBS-06：工程检查点事件写入死锁，回滚错误覆盖原始原因

- 首次工程执行 `exec_efbce0072d41421cbc1d98bf2caeb057` 的完整 traceback 首先出现 MySQL 1213 Deadlock，SQL 是 workflow_event INSERT，事件为 `activity:exec_efbce0072d41421cbc1d98bf2caeb057:22:read_file_22`。
- 随后的 nested transaction 回滚触发 MySQL 1305 `SAVEPOINT sa_savepoint_1 does not exist`；数据库 execution.error 最终只保存后者。
- 调用链：CodeEngineerNodes._record_action → _save_snapshot → save_execution_draft → sync_checkpoint_activity_events → append_event → db.flush；append_event 使用 begin_nested，仅处理 IntegrityError。
- 影响：本来成功的文件读取因平台进度记录失败而中断，需要整个执行恢复。具体锁竞争的另一方尚未通过数据库死锁快照确认；不能仅凭 SAVEPOINT 错误推断根因。

### OBS-07：正常上下文读取被无源码进展阈值停止

- 第二次工程执行 `exec_5e21a5a74f044144b13f4dc1d448d5b6` checkpoint：outcome=blocked，blocked_reason_code=NO_SOURCE_PROGRESS，no_progress_steps=12；原因原文“连续 12 个工具动作没有源码进展，停止本次执行”。
- work_items=[]、modified_files=[]、needs_delivery_plan=true，writer_model_calls=0；尚未提交工程依赖计划或写业务代码。累计 model_turns=13、tool_calls=13，预算为 80/120，**不是总预算用尽**。
- 最近读取均成功，涉及后端入口、数据库、配置、API、前端路由/样式、依赖与迁移文件。需评估首次规划前的有效信息获取与重复空转是否被同一阈值混为一谈。
- 22:21:52 和 22:21:59 模型分别返回 8、10 个 read_file 工具调用。代码已确认：`app/agents/code_engineer.py:decide_next_action` 无论读写都使用 `result.tool_calls[:1]`，其余调用被丢弃。后续模型重新请求读取原批次中的文件，增加模型轮次和无进展计数。
- `app/orchestration/code_engineer.py:_record_action` 只有 changed 文件才清零 no_progress_steps；成功读到新文件同样递增，达到 12 即阻断。只读批量被截断与首次规划阶段固定阈值共同放大了本次停顿，不能简单靠加重试次数处理。

### OBS-08：执行暂停与运行状态表达不一致

- 22:24:34：engineering job failed，error 为“Code Engineer 已暂停（blocked），等待用户显式继续”；最新 execution 亦 failed；BuildRun 仍 running/developer、error=NULL。
- 用户同时反馈“好像卡住了”。这是已确认的后台状态差异；尚未直接检查浏览器渲染，不能声称前端一定没有暂停提示。
- 这次不会继续自动前进；是否恢复由用户操作，监测不触发付费重试。

## 当前验收边界

截至 22:24，需求与架构产物已经发布，工程执行暂停，完整网站尚未生成。代码生成、依赖安装、Docker 真实启动、逐项需求用例、缺陷修复后复测、回归、预览交付均未完成，不能标记通过。本轮监测没有运行额外测试或付费模型调用，也没有修改业务代码。

观察阶段曾创建每两分钟的监测（automation id: forgeai）。进入本次修复后已暂停，避免只读监测继续与修改任务交错；未自动恢复原付费 run。

## 全链条修复与验证

以下为用户授权修改后的实现事实，不改写上面的原始运行结果。

| 观察   | 修复及对应验证                                                                                                                                                                                                                              |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OBS-01 | ProviderException 保留现有捕获兼容性，但临时上游失败返回 503，永久上游失败及协议错误返回 502。Code Engineer 将供应商异常交回现有 worker 的重试策略，不误标为产品阻断；仍保留已消耗预算。错误映射测试通过；未声称已解决供应商 SSL 网络根因。 |
| OBS-02 | 复用共享工具循环，PM 每次调用前保留预算，工具完成后保存冻结输入、历史、草稿、已读链接及最终输出。失败保存具体原因；恢复读取检查点。SQLite 测试验证 edit_prd 后网络中断仍能接着提交，不重做编辑。                                            |
| OBS-03 | Architect 使用同一检查点机制，保存草稿、检查证据与累计轮次。非法 JSON 在剩余预算内收到纠正反馈；网络重试恢复已有历史；完成输出可重放。对应恢复与协议纠错测试通过。                                                                          |
| OBS-04 | 审批后的 Leader 调用建立准确 project/run 计量上下文；模型响应在工具解析之前记录 usage。测试验证非法工具响应仍计量、Leader 两次模拟响应持久化到准确 run。历史遗漏无法凭空补齐，未修改实际使用记录。                                          |
| OBS-05 | 开发前端等待后端就绪；认证初始化只在 401 清理凭据，临时 5xx 保留凭据并允许重试。前端回归通过。                                                                                                                                              |
| OBS-06 | 移除孤立恢复扫描的 JOIN FOR UPDATE，改为先选候选再锁准确过期 execution / pending task，并跳过正在被锁的行。真实 MySQL 隔离库验证健康检查点不被扫描阻塞、过期锁定执行跳过后只恢复一次。                                                      |
| OBS-07 | Code Engineer 最多一次执行 16 个只读工具，不再静默取第一个；混合/多写批次明确拒绝。新读取结果和有效计划推进算作进展，重复读取仍触发有界停止；原模型/工具预算保留。测试验证 18 次不同读取可进入实现、重复读取仍停止、批量写不执行。          |
| OBS-08 | 状态接口增加 stage、job_status、next_retry_at；优先显示活跃 job，自动恢复持续轮询并隐藏竞争性手动恢复按钮；真正停止显示原因及继续入口。批准后尚无源码也保留构建阶段语义。前后端回归通过。                                                   |

### 死锁根因的补充证据

修复阶段只读检查 `SHOW ENGINE INNODB STATUS`，确认原 1213 的另一方为
`recover_orphaned_work` 的联表加锁扫描：扫描持有 Project 排他锁并等待健康 TaskExecution，
检查点持有 TaskExecution 锁并在 workflow_event 外键检查中等待 Project，共同形成环。
因此修复锁范围，不增加笼统重试或吞掉 SAVEPOINT 错误。两个真实 MySQL 用例使用随机命名的
`forgeai_test_*` 临时数据库，结束后删除；没有对开发数据库执行写入。

### 已实际运行

- 后端全量：`pytest tests -q --tb=short`，**545 passed、307 subtests passed、6 skipped**，527.67 秒。
  跳过的是 4 个 Docker 和 2 个 MySQL 环境用例，补跑情况另列。
  日志：`apps/backend/output/paid-fix-backend-full.log`。
- 本次问题定向回归：117 passed、59 subtests passed；最终状态排序与计量回归新增后，
  `test_requirements_loop.py` **32 passed**。日志：`paid-fix-targeted-elevated.log`、`paid-fix-status-final.log`。
- 最后补充的工程供应商异常分流与 durable worker 回归：**61 passed**，152.82 秒。
  验证失败请求保留已消耗轮次、继续使用已有检查点，供应商重试属性交回 worker。
  日志：`apps/backend/output/paid-fix-provider-final.log`。后续生产代码变更均已跑对应定向回归；
  上述 545 项全量运行发生在最后的状态排序和异常分流补充之前，未重复计为新的全量结果。
- 真实 MySQL：`FORGEAI_TEST_MYSQL=1 pytest tests/test_mysql_workflow_concurrency.py`，
  **2 passed**，9.89 秒。日志：`apps/backend/output/paid-fix-mysql.log`。
- Docker 工程检查与 QA：`FORGEAI_TEST_DOCKER=1 pytest tests/test_engineering_checks.py tests/test_qa_execution.py`，
  **35 passed**，403.96 秒。日志：`apps/backend/output/paid-fix-docker.log`。
  独立登录/私有记录示例中，首次 **4 passed / 1 failed**，准确发现跨账号泄漏；修复源码后同一 suite
  **5 passed**。保留原测试 hash 和新 source hash，记录缺陷、复测及全套回归。
  逐项证据为 `apps/backend/output/qa-integration/failure.json`、`retest.json`；
  `failed-report.json`、`accepted-report.json` 是这些执行结果的汇总。
  同次 run_check / 截图只是补充工程与视觉检查，不替代这 5 个用例。
- 前端需求与认证：**35 passed**；类型检查及生产构建通过；ESLint / Oxlint 通过。
  构建仍提示 auth 同时动态/静态导入不能拆成独立 chunk，不影响本次状态修复。
- Docker 依赖同步与预览补查：首轮 **11 passed / 2 failed**，失败均为 `docker info` 10 秒超时，
  两个容器测试尚未进入业务检查。随后只读探测 2.48 秒成功返回 Docker 29.8.2；
  只重跑失败项，**2 passed**，66.72 秒，验证模板依赖锁同步及容器内前后端 HTTP 响应。
  日志：`paid-fix-preview-deps.log`、`paid-fix-preview-deps-retry.log`。
  该环境波动保留记录，未通过加大运行超时或隐藏错误让测试通过。全量默认跳过的 6 项均已单独实际执行通过。
- 后端 Ruff 与格式检查通过；mypy 171 个文件通过；UTF-8 编码检查及 `git diff --check` 通过。
- 启动探测脚本语法、模拟健康响应的成功分支通过；本机后端未在 60 秒内返回就绪响应时，脚本按预期退出 1。
  没有为验证重启用户服务。

### 边界与尚未验证

- 上述测试使用固定测试应用、模拟模型、隔离数据库或容器；没有额外调用付费模型。
  原 project 2 的完整网站生成、真实需求逐项验收和最终交付没有重跑，不能声明已完成。
- 未运行真实供应商网络故障恢复、计费账单核对、整个 `pnpm dev` 冷启动及浏览器手动点击验收。
  没有改动或自动恢复原 run，没有代用户审批，没有部署应用。
- 测试框架仍报告 Starlette 对 httpx TestClient 的弃用警告；本次没有扩展为依赖升级。
