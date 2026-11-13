#!/usr/bin/env bash
# Phase 11A live paper-trading observation: start or stop the workers on the host.
#
#   scripts/paper_observation.sh start   # API + market-data stream, recovery, news stream, paper execution,
#                                        # position monitor and orchestrator workers
#   scripts/paper_observation.sh status
#   scripts/paper_observation.sh stop
#   scripts/paper_observation.sh ctl <orchestration.cli arguments>
#                                        # e.g. ctl status | ctl protection | ctl alerts --open
#
# PAPER TRADING ONLY. Every order is a simulated paper order filled by the isolated paper
# broker from stored market data. Starting the workers does NOT enable trading: new entries
# also need the operator control ENABLED, which only an operator sets with
#   scripts/paper_observation.sh ctl enable --actor operator:<name> --reason "<why>"
#
# Configuration: the git-ignored .env.paper11a (paper environment, a dedicated database and
# role) overrides .env (provider credentials). Logs and PIDs go to logs/paper11a/ (ignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env.paper11a"
LOGS="$ROOT/logs/paper11a"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
WORKERS=(market_data_stream market_data_recovery news_stream paper_execution position_monitor trading_orchestrator)

load_env() {
  [ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 2; }
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  export PYTHONPATH="$ROOT/backend"
}

start() {
  load_env
  mkdir -p "$LOGS"
  cd "$ROOT"
  for w in "${WORKERS[@]}"; do
    if [ -f "$LOGS/$w.pid" ] && kill -0 "$(cat "$LOGS/$w.pid")" 2>/dev/null; then
      echo "$w already running"; continue
    fi
    nohup "$PY" -m "app.workers.$w" >>"$LOGS/$w.log" 2>&1 &
    echo $! >"$LOGS/$w.pid"
    echo "started $w (pid $!)"
  done
  if ! { [ -f "$LOGS/api.pid" ] && kill -0 "$(cat "$LOGS/api.pid")" 2>/dev/null; }; then
    nohup "$PY" -m uvicorn app.main:create_app --factory --host 127.0.0.1 \
      --port "${PAPER_API_PORT:-8124}" >>"$LOGS/api.log" 2>&1 &
    echo $! >"$LOGS/api.pid"
    echo "started api on 127.0.0.1:${PAPER_API_PORT:-8124} (pid $!)"
  fi
}

status() {
  for w in "${WORKERS[@]}" api; do
    if [ -f "$LOGS/$w.pid" ] && kill -0 "$(cat "$LOGS/$w.pid")" 2>/dev/null; then
      echo "$w: running (pid $(cat "$LOGS/$w.pid"))"
    else
      echo "$w: stopped"
    fi
  done
}

stop() {
  for w in api "${WORKERS[@]}"; do
    if [ -f "$LOGS/$w.pid" ]; then
      kill "$(cat "$LOGS/$w.pid")" 2>/dev/null || true
      rm -f "$LOGS/$w.pid"
      echo "stopped $w"
    fi
  done
}

# The orchestration command line against the paper database (status, protection, alerts,
# enable, pause, disable, emergency-stop, ...). Operator actions need --actor operator:<name>.
ctl() {
  load_env
  cd "$ROOT"
  exec "$PY" -m app.orchestration.cli "$@"
}

case "${1:-}" in
  start) start ;;
  status) status ;;
  stop) stop ;;
  ctl) shift; ctl "$@" ;;
  *) echo "usage: $0 start|status|stop|ctl <args>" >&2; exit 2 ;;
esac
