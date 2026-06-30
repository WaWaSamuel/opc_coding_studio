# 后端镜像:FastAPI + LangGraph 编排 + 飞书长连接(M4 入口层)
# 构建上下文为 ./backend;包以 `backend.` 命名空间导入,故放到 /app/backend/。
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 先装依赖以利用层缓存
COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 再拷源码(.dockerignore 已排除 .env/__pycache__/*.db)
COPY . /app/backend/

# 持久化与工作流挂载点(compose volumes 覆盖)
RUN mkdir -p /app/data /app/workflows

EXPOSE 8001

# main.py 一处装配 OrchestratorService:Web(uvicorn)+ 飞书长连接(后台线程)
# 飞书长连接出站 WebSocket,免公网回调;有 LARK_* 凭据即自动启用。
CMD ["python", "-m", "backend.main"]
