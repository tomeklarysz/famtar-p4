#!/bin/bash

set -e

ROUTERS="r1 r2 r3 r4"

echo "[1/5] P4 compilation..."
docker run --rm -v $(pwd):/work -w /work p4lang/p4c \
  p4c-bm2-ss --arch v1model -o basic_forwarding.json \
  --p4runtime-files basic_forwarding.p4info.txtpb basic_forwarding.p4

echo "[2/5] starting containerlab..."
sudo containerlab destroy --topo topology.clab.yaml 2>/dev/null || true
sudo containerlab deploy --topo topology.clab.yaml

echo "[3/5] disabling kernel IP forwarding on routers..."
# bmv2 and the router's kernel share the same netns and both receive frames
# on ethX. With ip_forward=1 the kernel would forward packets on its own,
# bypassing (or duplicating) the P4 data plane. FRR/IS-IS keeps working,
# because routing protocol traffic is local, not forwarded.
for r in $ROUTERS; do
  ns="clab-p4-basic-lab-$r"
  sudo ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0
  val=$(sudo ip netns exec "$ns" sysctl -n net.ipv4.ip_forward)
  if [ "$val" != "0" ]; then
    echo "ERROR: net.ipv4.ip_forward is still $val on $r" >&2
    exit 1
  fi
  echo "  $r: net.ipv4.ip_forward = $val"
done

echo "[4/5] static ARP entries..."
docker exec clab-p4-basic-lab-h1 arp -s 10.0.1.254 00:00:00:00:00:fe
docker exec clab-p4-basic-lab-h2 arp -s 10.0.2.254 00:00:00:00:00:fe

echo "[5/5] starting control plane (Python)..."
sleep 2
sudo GRPC_VERBOSITY=ERROR venv/bin/python3 controller.py

echo "[*] environment ready. You can now run tests or interact with the network."
