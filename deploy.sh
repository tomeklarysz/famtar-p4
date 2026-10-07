#!/bin/bash

set -e

echo "[1/4] P4 compilation..."
docker run --rm -v $(pwd):/work -w /work p4lang/p4c \
  p4c-bm2-ss --arch v1model -o basic_forwarding.json \
  --p4runtime-files basic_forwarding.p4info.txtpb basic_forwarding.p4

echo "[2/4] starting containerlab..."
sudo containerlab destroy --topo topology.clab.yaml 2>/dev/null || true
sudo containerlab deploy --topo topology.clab.yaml

echo "[3/4] static ARP entries..."
docker exec clab-p4-basic-lab-h1 arp -s 10.0.1.254 00:00:00:00:00:fe
docker exec clab-p4-basic-lab-h2 arp -s 10.0.2.254 00:00:00:00:00:fe

echo "[4/4] starting control plane (Python)..."
source venv/bin/activate
python3 controller.py

echo "[*] environment ready. You can now run tests or interact with the network."