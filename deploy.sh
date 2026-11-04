#!/bin/bash

set -e

echo "[1/3] P4 compilation..."
docker run --rm -v $(pwd):/work -w /work p4lang/p4c \
  p4c-bm2-ss --arch v1model -o basic_forwarding.json \
  --p4runtime-files basic_forwarding.p4info.txtpb basic_forwarding.p4

echo "[2/3] starting containerlab..."
sudo containerlab destroy --topo topology.clab.yaml 2>/dev/null || true
sudo containerlab deploy --topo topology.clab.yaml

echo "[3/3] starting control plane (Python per router)..."
sleep 2
mkdir -p logs
for r in r1 r2 r3 r4; do
  sudo GRPC_VERBOSITY=ERROR venv/bin/python3 controller.py --config "configs/$r/controller.yaml" --mode "${1:-famtar}" \
    > "logs/$r.log" 2>&1 &
done

trap 'echo ""; echo "[*] stopping controllers..."; sudo pkill -f "controller.py --config" || true' INT TERM EXIT

echo "[*] environment ready. Logs in logs/r*.log (Ctrl+C stops all controllers)"
tail -q -f logs/r*.log
