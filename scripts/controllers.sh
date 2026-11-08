#!/bin/bash
set -e

ACTION="${1:-}"
MODE="${2:-famtar}"
ROUTERS=(r1 r2 r3 r4)

usage() {
  echo "Użycie: $0 {start|stop|restart|status} [famtar|ecmp]"
  echo ""
  echo "Przykłady:"
  echo "  $0 restart famtar   # przełącza kontrolery w tryb FAMTAR"
  echo "  $0 restart ecmp     # przełącza kontrolery w tryb ECMP"
  echo "  $0 stop             # zatrzymuje wszystkie procesy kontrolera"
  echo "  $0 status           # sprawdza stan procesów kontrolera"
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
  stop_controllers
  echo "[*] Uruchamianie kontrolerów w trybie: $target_mode..."
  mkdir -p logs
  for r in "${ROUTERS[@]}"; do
    sudo GRPC_VERBOSITY=ERROR venv/bin/python3 controller.py --config "configs/$r/controller.yaml" --mode "$target_mode" \
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
