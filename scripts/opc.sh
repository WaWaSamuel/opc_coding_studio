#!/usr/bin/env bash
# 一人公司 AI Agent 系统 — 一键运维脚本(收口安装/运行/停止/查看日志/测试/构建)
#
#   ./scripts/opc.sh <command>
#
# 本地裸跑(默认):用 .venv 起后端,后台进程 + pidfile + 日志文件;
# 容器模式:docker-* 子命令直接调 docker compose。
#
# 保命纪律:真实密钥只在根目录 .env(已 gitignore);本脚本不打印任何密钥。
set -euo pipefail

# ── 路径解析(脚本可在任意 cwd 调用)─────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

VENV="$ROOT_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
RUN_DIR="$ROOT_DIR/.run"          # pid 文件
LOG_DIR="$ROOT_DIR/logs"          # 运行日志
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"
BACKEND_PORT="${OPC_BACKEND_PORT:-8001}"     # 与 backend/config.py api_port 对齐
FRONTEND_PORT="${OPC_FRONTEND_PORT:-5174}"   # 与 frontend/vite.config.ts 对齐

mkdir -p "$RUN_DIR" "$LOG_DIR"

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

_alive() {  # _alive <pidfile> → 0 表示在跑
  local f="$1"
  [ -f "$f" ] || return 1
  local pid; pid="$(cat "$f" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# 收集一棵进程树(含自身)的所有 PID(后序:子在前、父在后)。
# npm run dev 会 fork 出 vite 子进程,只 kill 父进程会留孤儿占端口。
_proc_tree() {  # _proc_tree <pid> → 逐行输出整棵树的 pid
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _proc_tree "$child"
  done
  echo "$pid"
}

# 优雅停一组 PID:先 TERM、轮询、再 KILL。
_kill_pids() {  # _kill_pids <pid...>
  local pids=("$@") pid
  [ "${#pids[@]}" -gt 0 ] || return 0
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    local any=0
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && any=1; done
    [ "$any" = "0" ] && break
    sleep 0.3
  done
  for pid in "${pids[@]}"; do kill -9 "$pid" 2>/dev/null || true; done
}

# 按端口兜底清理监听进程(对付脱管/改端口的残留)。
_kill_port() {  # _kill_port <port>
  local port="$1" pids
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    c_ylw "清理占用 :$port 的残留进程 (pid=$(echo "$pids" | tr '\n' ' '))"
    # shellcheck disable=SC2086
    _kill_pids $pids
  fi
}

_stop_pidfile() {  # _stop_pidfile <name> <pidfile> [port]
  local name="$1" f="$2" port="${3:-}"
  if _alive "$f"; then
    local pid tree
    pid="$(cat "$f")"
    tree="$(_proc_tree "$pid")"
    c_ylw "停止 $name (pid=$pid,进程树: $(echo "$tree" | tr '\n' ' ')) ..."
    # shellcheck disable=SC2086
    _kill_pids $tree
    c_grn "$name 已停止"
  else
    c_ylw "$name 未在运行(按端口兜底检查)"
  fi
  rm -f "$f"
  # 无论 pidfile 是否在,都按端口兜底:清掉脱管的孤儿(如 vite 残留占端口)。
  [ -n "$port" ] && _kill_port "$port"
}

# ── install:安装后端 + 前端依赖 ────────────────────────────────
cmd_install() {
  c_grn "[1/3] 准备 Python 虚拟环境 .venv"
  if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV"
  fi
  c_grn "[2/3] 安装后端依赖 (backend/requirements.txt)"
  "$PIP" install --upgrade pip >/dev/null
  "$PIP" install -r "$ROOT_DIR/backend/requirements.txt"

  if [ -d "$ROOT_DIR/frontend" ]; then
    c_grn "[3/3] 安装前端依赖 (frontend/, npm)"
    if command -v npm >/dev/null 2>&1; then
      (cd "$ROOT_DIR/frontend" && npm install)
    else
      c_ylw "未检测到 npm,跳过前端依赖安装(只跑后端可忽略)"
    fi
  fi

  if [ ! -f "$ROOT_DIR/.env" ]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    c_ylw "已从 .env.example 生成 .env;按需填入 ARK_* / LARK_* / GITHUB_TOKEN(默认 mock 离线即可跑)"
  fi
  c_grn "依赖安装完成。运行: ./scripts/opc.sh run"
}

# ── run:后台起后端 + 前端 dev(默认全栈;--backend-only 只起后端)────
cmd_run() {
  local with_frontend=1
  for a in "$@"; do
    case "$a" in
      --backend-only|--no-frontend) with_frontend=0 ;;
      --with-frontend) with_frontend=1 ;;  # 兼容旧参数(现已默认带前端)
    esac
  done

  [ -x "$PY" ] || { c_red "未找到 .venv,请先 ./scripts/opc.sh install"; exit 1; }

  if _alive "$BACKEND_PID"; then
    c_ylw "后端已在运行 (pid=$(cat "$BACKEND_PID"))"
  else
    c_grn "启动后端 (python -m backend.main) → http://localhost:$BACKEND_PORT"
    nohup "$PY" -m backend.main >>"$BACKEND_LOG" 2>&1 &
    echo $! >"$BACKEND_PID"
    c_grn "后端 pid=$(cat "$BACKEND_PID");日志: $BACKEND_LOG"
  fi

  if [ "$with_frontend" = "1" ]; then
    if _alive "$FRONTEND_PID"; then
      c_ylw "前端 dev 已在运行 (pid=$(cat "$FRONTEND_PID"))"
    elif command -v npm >/dev/null 2>&1; then
      # 启动前清掉脱管在 :$FRONTEND_PORT 的残留(否则 vite 会改用别的端口,看似"没生效")。
      _kill_port "$FRONTEND_PORT"
      c_grn "启动前端 dev (vite) → http://localhost:$FRONTEND_PORT"
      ( cd "$ROOT_DIR/frontend" && exec nohup npm run dev >>"$FRONTEND_LOG" 2>&1 ) &
      echo $! >"$FRONTEND_PID"
      c_grn "前端 pid=$(cat "$FRONTEND_PID");日志: $FRONTEND_LOG"
    else
      c_red "未检测到 npm,无法启动前端 dev"
    fi
  fi
  c_grn "查看日志: ./scripts/opc.sh logs    停止: ./scripts/opc.sh stop"
}

# ── stop:停止本地后台进程(进程树 + 端口兜底)────────────────────
cmd_stop() {
  _stop_pidfile "前端 dev" "$FRONTEND_PID" "$FRONTEND_PORT"
  _stop_pidfile "后端" "$BACKEND_PID" "$BACKEND_PORT"
}

cmd_restart() { cmd_stop; cmd_run "$@"; }

# ── logs:查看日志(默认后端,-f 跟随)──────────────────────────
cmd_logs() {
  local target="backend" follow=0
  for a in "$@"; do
    case "$a" in
      backend|frontend) target="$a" ;;
      -f|--follow) follow=1 ;;
    esac
  done
  local f="$BACKEND_LOG"; [ "$target" = "frontend" ] && f="$FRONTEND_LOG"
  [ -f "$f" ] || { c_ylw "暂无日志: $f"; exit 0; }
  if [ "$follow" = "1" ]; then
    tail -f "$f"
  else
    tail -n 200 "$f"
  fi
}

# ── status:运行状态一览(pidfile + 端口实测,避免孤儿误报)──────
_port_pids() {  # _port_pids <port> → 监听该端口的 pid(空格分隔)
  { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; } | tr '\n' ' '
}

cmd_status() {
  local bp fp
  bp="$(_port_pids "$BACKEND_PORT")"; fp="$(_port_pids "$FRONTEND_PORT")"
  if _alive "$BACKEND_PID"; then c_grn "后端: 运行中 (pid=$(cat "$BACKEND_PID"), :$BACKEND_PORT)"
  elif [ -n "$bp" ]; then c_ylw "后端: 脱管残留占用 :$BACKEND_PORT (pid=$bp) — 用 stop 清理"
  else c_ylw "后端: 已停止"; fi
  if _alive "$FRONTEND_PID"; then c_grn "前端: 运行中 (pid=$(cat "$FRONTEND_PID"), :$FRONTEND_PORT)"
  elif [ -n "$fp" ]; then c_ylw "前端: 脱管残留占用 :$FRONTEND_PORT (pid=$fp) — 用 stop 清理"
  else c_ylw "前端: 已停止"; fi
}

# ── test:全量单测(离线确定性;强制 mock,屏蔽 shell 残留 ARK_* )──
cmd_test() {
  [ -x "$PY" ] || { c_red "未找到 .venv,请先 ./scripts/opc.sh install"; exit 1; }
  env -u ARK_MODEL -u MODEL_PROVIDER "$PY" -m pytest -q "$@"
}

# ── build:前端生产构建 ─────────────────────────────────────────
cmd_build() {
  command -v npm >/dev/null 2>&1 || { c_red "未检测到 npm"; exit 1; }
  (cd "$ROOT_DIR/frontend" && npm run build)
  c_grn "前端构建产物: frontend/dist"
}

# ── docker 模式 ────────────────────────────────────────────────
cmd_docker_up()   { docker compose up --build -d; c_grn "backend :8001  frontend :5174"; }
cmd_docker_down() { docker compose down; }
cmd_docker_logs() { docker compose logs -f "$@"; }

usage() {
  cat <<'EOF'
一人公司 AI Agent — 运维脚本

本地裸跑:
  install              创建 .venv、装后端/前端依赖、生成 .env
  run [--backend-only] 后台起后端 + 前端 dev(默认全栈;--backend-only 只起后端)
  stop                 停止本地后台进程(含子进程树 + 端口兜底清理)
  restart [...]        重启(透传 run 参数)
  logs [backend|frontend] [-f]   查看日志(默认后端;-f 跟随)
  status               查看运行状态(pidfile + 端口实测)
  test [...]           全量单测(强制 mock 离线;透传 pytest 参数)
  build                前端生产构建(frontend/dist)

容器模式:
  docker-up            docker compose up --build -d
  docker-down          docker compose down
  docker-logs [svc]    docker compose logs -f

示例:
  ./scripts/opc.sh install
  ./scripts/opc.sh run               # 起后端 + 前端
  ./scripts/opc.sh run --backend-only
  ./scripts/opc.sh logs -f
  ./scripts/opc.sh stop
EOF
}

case "${1:-help}" in
  install)      shift; cmd_install "$@" ;;
  run)          shift; cmd_run "$@" ;;
  stop)         shift; cmd_stop "$@" ;;
  restart)      shift; cmd_restart "$@" ;;
  logs)         shift; cmd_logs "$@" ;;
  status)       shift; cmd_status "$@" ;;
  test)         shift; cmd_test "$@" ;;
  build)        shift; cmd_build "$@" ;;
  docker-up)    shift; cmd_docker_up "$@" ;;
  docker-down)  shift; cmd_docker_down "$@" ;;
  docker-logs)  shift; cmd_docker_logs "$@" ;;
  help|-h|--help) usage ;;
  *) c_red "未知命令: $1"; echo; usage; exit 1 ;;
esac
