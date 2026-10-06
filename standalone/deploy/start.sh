#!/usr/bin/env bash
# 启动门禁网关 + go2rtc。用法：
#   ./deploy/start.sh              启动（前台跟随网关日志）
#   ./deploy/start.sh -d           后台启动
#   ./deploy/start.sh stop         全部停止
#
# 设备密码等配置放在 deploy/.env（不要提交到版本库）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/standalone/deploy/.env"
PID_DIR="$ROOT/data/run"
LOG_DIR="$ROOT/data/log"
GO2RTC_NAME=acs-go2rtc

mkdir -p "$PID_DIR" "$LOG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a; # shellcheck disable=SC1090
  source "$ENV_FILE"; set +a
else
  echo "缺少 $ENV_FILE，请先照 deploy/.env.example 创建" >&2
  exit 1
fi

: "${ACS_HOST:?ACS_HOST 未设置}"
: "${ACS_PASSWORD:?ACS_PASSWORD 未设置}"

export ACS_USER="${ACS_USER:-admin}"
export ACS_HTTP_PORT="${ACS_HTTP_PORT:-8080}"
export ACS_DB="${ACS_DB:-$ROOT/data/gateway.db}"
export ACS_INGEST_MODE="${ACS_INGEST_MODE:-httphost}"
export GO2RTC_BASE="${GO2RTC_BASE:-http://127.0.0.1:1984}"
export GO2RTC_STREAM="${GO2RTC_STREAM:-door}"
export ACS_RTSP_CHANNEL="${ACS_RTSP_CHANNEL:-102}"
export DOORBELL_SECONDS="${DOORBELL_SECONDS:-30}"

start_go2rtc() {
  if docker ps --format '{{.Names}}' | grep -qx "$GO2RTC_NAME"; then
    echo "go2rtc 已在运行"
    return
  fi
  docker rm -f "$GO2RTC_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$GO2RTC_NAME" --network host --restart unless-stopped \
    -v "$ROOT/standalone/deploy/go2rtc/go2rtc.yaml:/config/go2rtc.yaml:ro" \
    alexxit/go2rtc:latest >/dev/null
  for _ in $(seq 1 20); do
    curl -sf -o /dev/null "http://127.0.0.1:1984/api" && break || sleep 0.5
  done
  echo "go2rtc 已启动"
}

start_gateway() {
  if [[ -f "$PID_DIR/gateway.pid" ]] && kill -0 "$(cat "$PID_DIR/gateway.pid")" 2>/dev/null; then
    echo "网关已在运行 (pid $(cat "$PID_DIR/gateway.pid"))"
    return
  fi
  nohup python3 -u "$ROOT/gateway/server.py" >>"$LOG_DIR/gateway.log" 2>&1 &
  echo $! >"$PID_DIR/gateway.pid"
  echo "网关已启动 (pid $(cat "$PID_DIR/gateway.pid"))，日志 $LOG_DIR/gateway.log"
}

stop_all() {
  if [[ -f "$PID_DIR/gateway.pid" ]]; then
    kill "$(cat "$PID_DIR/gateway.pid")" 2>/dev/null || true
    rm -f "$PID_DIR/gateway.pid"
    echo "网关已停止"
  fi
  docker stop "$GO2RTC_NAME" >/dev/null 2>&1 && echo "go2rtc 已停止" || true
}

case "${1:-start}" in
  stop) stop_all ;;
  start)
    start_go2rtc
    start_gateway
    sleep 3
    curl -s "http://127.0.0.1:${ACS_HTTP_PORT}/api/status" | python3 -m json.tool || true
    echo
    echo "中控屏: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${ACS_HTTP_PORT}/"
    [[ "${2:-}" == "-d" ]] || { echo "（Ctrl-C 只退出日志跟随，服务仍在后台）"; tail -f "$LOG_DIR/gateway.log"; }
    ;;
  *) echo "用法: $0 [start [-d] | stop]" >&2; exit 1 ;;
esac
