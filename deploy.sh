#!/bin/bash

set -e

echo "[1/3] P4 compilation..."
docker run --rm -v $(pwd):/work -w /work p4lang/p4c \
  p4c-bm2-ss --arch v1model -o basic_forwarding.json \
  --p4runtime-files basic_forwarding.p4info.txtpb basic_forwarding.p4

echo "[2/3] starting containerlab..."
sudo containerlab destroy --topo topology.clab.yaml 2>/dev/null || true
sudo containerlab deploy --topo topology.clab.yaml

echo "[3/3] starting control plane (Python)..."
sleep 2
sudo GRPC_VERBOSITY=ERROR venv/bin/python3 controller.py --mode "${1:-famtar}"

echo "[*] environment ready. You can now run tests or interact with the network."
