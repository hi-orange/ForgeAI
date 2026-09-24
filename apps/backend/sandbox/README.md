# 工程检查环境

工程 Agent 在 `fullstack-v1` 工作区按功能读写代码，并使用 `run_check` 获取真实反馈。
项目级依赖通过有界工具 `install_project_dependency`（npm/pnpm/uv/pip）增删；
写入 `frontend/package.json` 后平台会尝试在工作区同步安装。系统包（apt 等）不在此工具范围。
提交 `complete_work_item` 会强制运行全部工程检查；失败保留当前功能并交回模型修复。
检查通过的状态是 `engineering_checked`，不是业务验收通过、发布成功或在线预览可用。

## 准备环境

在安装并启动 Linux 容器模式的 Docker 后，从 `apps/backend` 执行：

```sh
docker build -f sandbox/Dockerfile -t forgeai-checks:fullstack-v1 .
```

镜像构建阶段预装模板依赖，作为与模板清单一致时的快速路径。
运行检查时：若生成应用的 `package.json` / `pyproject.toml` 与模板一致，复用预装模块；
若清单已演进（例如增加 `vue-router`），检查控制器会按生成清单联网安装依赖后再构建。
生产环境应在可信构建机器预构建镜像并将 `ENGINEERING_CHECK_IMAGE` 指向审核过的镜像摘要。
更新 `sandbox/check.py` 或模板基线依赖后需重新构建镜像。

配置：`ENGINEERING_CHECK_IMAGE`（默认上述标签）与
`ENGINEERING_CHECK_TIMEOUT_SECONDS`（默认 300 秒，平台硬限制 300 秒）。
缺少 Docker、守护进程或镜像时暂停构建，代码与日志保留。环境就绪后点击“继续构建”。

## 实际检查范围

- `database`：Python 编译、按清单安装后端依赖（如有变更）、空 SQLite 库迁移到 head、
  Alembic 模型一致性检查、输出实际表结构。
- `backend`：上述数据库检查、启动 Uvicorn、请求健康接口与 OpenAPI，输出注册路由。
- `frontend`：按清单安装前端依赖（如有变更）、Vue/TypeScript 检查、Vite 生产构建、检查首页产物存在。
- `all`：后端与前端检查。每个功能提交都会运行，不复用旧源码的检查结论。

临时数据库只用于验证，检查结束后销毁；不能把“验证建表成功”理解为生产数据库已经部署。
这些工程检查不会证明所有业务验收条款、权限流程或浏览器交互都已满足。
工程检查容器仍是一次性冒烟，不负责在线预览。
构建完成后，平台可在本机 `127.0.0.1` 启动 loopback 预览会话（构建 `dist`、起生成后端、
静态页 + `/api` 反代），并在工作台 iframe 中加载；这不是多租户公网预览网关。

## 隔离与恢复

源码通过标准输入传入容器，不挂载宿主目录或 Docker socket，不传入平台环境变量。
容器非 root、根文件系统只读，移除 capabilities，并限制 CPU、内存、进程和临时磁盘。
为按生成清单安装依赖，检查容器允许出网，且 `/tmp` tmpfs 带 `exec`
（原生 npm 二进制需要可执行）；安装使用 `--ignore-scripts` 降低供应链风险。
输出仅保留有界日志尾部。超时会清理本次命名的容器。
边界参数依据 [Docker run 文档](https://docs.docker.com/reference/cli/docker/container/run/)。

工作区文件锁覆盖整个执行及恢复操作，防止同一主机多进程写入。此实现假设后端进程共享同一
本地工作区，不支持各自持有独立磁盘副本的多主机执行。任务租约仍负责拒绝旧执行提交。
容器退出不会修改宿主工作区，失败不会破坏源码；宿主进程被强制终止时，需要运维清理
残留 `forgeai-check-*` 容器。不能在共享生产宿主上把普通容器视为无限强度的安全边界。

构建事件追加保存在 `TaskExecution.draft.checkpoint.activity`，不再截断为最近 80 条。
这是现有 JSON 草稿的兼容性扩展，不需要数据库列迁移。前端合并同一操作的开始与结果，展示
完整步骤并随 `workspace_generation` 刷新文件。历史事件与功能检查结果随新执行继承。

## 验证

常规测试使用模拟模型和模拟检查边界，不调用付费模型：

```sh
uv run python -m unittest discover -s tests -p 'test_engineering*.py' -v
```

环境就绪后显式设置 `FORGEAI_TEST_DOCKER=1` 并运行同一命令，增加真实模板的迁移、
后端启动和前端构建集成检查。未设置时该项会显示 skipped，不算通过。
