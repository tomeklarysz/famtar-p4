#!/bin/bash
set -e

ACTION="${1:-}"
if [ "${2:-}" = "nobel" ] || [ "${2:-}" = "configs" ] || [ -d "${2:-}" ]; then
  MODE="famtar"
  TARGET="$2"
else
  MODE="${2:-famtar}"
  TARGET="${3:-${TOPO:-configs}}"
fi
if [ "$TARGET" != "configs" ] && [ -d "configs/$TARGET" ]; then
  CONFIG_DIR="configs/$TARGET"
else
  CONFIG_DIR="$TARGET"
fi

usage() {
  echo "Użycie: $0 {start|stop|restart|status} [famtar|ecmp] [configs|nobel|ścieżka]"
  echo ""
  echo "Przykłady:"
  echo "  $0 restart famtar         # przełącza kontrolery (r1..r4) w tryb FAMTAR"
  echo "  $0 restart ecmp           # przełącza kontrolery (r1..r4) w tryb ECMP"
  echo "  $0 restart famtar nobel   # uruchamia kontrolery dla topologii nobel-eu"
  echo "  $0 stop                   # zatrzymuje wszystkie procesy kontrolera"
  echo "  $0 status                 # sprawdza stan procesów kontrolera"
  exit 1
}

stop_controllers() {
  echo "[*] Zatrzymywanie procesów kontrolera..."
  sudo pkill -9 -f "python.*controller\.py" 2>/dev/null || true
  sudo pkill -9 -f "sudo.*controller\.py" 2>/dev/null || true
  sleep 1
  echo "[+] Kontrolery zatrzymane."
}

start_controllers() {
  local target_mode="$1"
  shopt -s nullglob
  local confs=("$CONFIG_DIR"/*/controller.yaml)
  shopt -u nullglob
  if [ ${#confs[@]} -eq 0 ]; then
    echo "[!] Brak plików */controller.yaml w katalogu: $CONFIG_DIR" >&2
    exit 1
  fi

  stop_controllers
  echo "[*] Uruchamianie kontrolerów (${#confs[@]} węzłów z $CONFIG_DIR) w trybie: $target_mode..."
  mkdir -p logs
  for conf in "${confs[@]}"; do
    local r
    r=$(basename "$(dirname "$conf")")
    sudo GRPC_VERBOSITY=ERROR venv/bin/python3 controller.py --config "$conf" --mode "$target_mode" \
      > "logs/$r.log" 2>&1 &
  done
  sleep 1
  echo "[+] Kontrolery uruchomione w tle."
  check_status
}

check_status() {
  echo "[*] Aktywne procesy kontrolera (Python):"
  local procs
  procs=$(pgrep -a -f "controller\.py" 2>/dev/null | grep -v "sudo" || true)
  if [ -n "$procs" ]; then
    echo "$procs"
  else
    echo "  Brak działających kontrolerów."
  fi
}

case "$ACTION" in
  start)
    start_controllers "$MODE"
    ;;
  stop)
    stop_controllers
    ;;
  restart)
    stop_controllers
    start_controllers "$MODE"
    ;;
  status)
    check_status
    ;;
  *)
    usage
    ;;
esac
