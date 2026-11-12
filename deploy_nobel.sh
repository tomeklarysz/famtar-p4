#!/bin/bash

set -e

echo "[1/3] Kompilacja P4..."
docker run --rm -v "$(pwd):/work" -w /work p4lang/p4c \
  p4c-bm2-ss --arch v1model -o basic_forwarding.json \
  --p4runtime-files basic_forwarding.p4info.txtpb basic_forwarding.p4

echo "[2/3] Uruchamianie topologii nobel-eu w Containerlab (11 routerów, 15 łączy)..."
sudo containerlab destroy --topo topology.clab.yaml 2>/dev/null || true
sudo containerlab destroy --topo topology.nobel.clab.yaml 2>/dev/null || true
sudo containerlab deploy --topo topology.nobel.clab.yaml

echo "[3/3] Uruchamianie kontrolerów (Python per router)..."
sleep 2
./scripts/controllers.sh restart "${1:-famtar}" nobel

trap 'echo ""; ./scripts/controllers.sh stop || true' INT TERM EXIT

echo "[*] Środowisko nobel-eu gotowe. Logi w logs/<router>.log (Ctrl+C zatrzymuje kontrolery)"
tail -q -f logs/ams.log logs/bru.log logs/fra.log logs/ham.log logs/lon.log logs/lyo.log logs/mil.log logs/mun.log logs/par.log logs/str.log logs/zur.log
