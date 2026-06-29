"""API 服务层(M4 入口层 / 界面 · FastAPI)。

REST + SSE,对齐 PRD 5.5 契约。所有路由只依赖 OrchestratorService,
不直接碰内核。Web 渠道(F-A.3)与界面(M12)走这里;飞书走长连接旁路。
"""
