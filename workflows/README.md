# 业务工作流定义(git 真源)

此目录存放业务工作流的定义骨架与模板(PRD F-B.8:电商/资讯/业务优化占位)。
当前 M1~M4 编排由代码控制(`backend/orchestrator/graph_runtime.py`),此处仅作
docker-compose 持久化挂载点(`./workflows:/app/workflows`)与未来声明式工作流的落点。

> 纳入 git 真源:工作流定义随仓库版本管理;运行态状态/日志在 `data/`(gitignore)。
