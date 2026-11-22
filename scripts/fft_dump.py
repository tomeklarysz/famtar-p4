#!/usr/bin/env python3
"""Dump non-empty FFT slots from the BMv2 registers of each router.

Usage: scripts/fft_dump.py [r1 r2 ...] [--all] [--topo nobel | --config-dir configs/nobel]
  --all          show expired entries too (older than FFT_TIMEOUT)
  --topo NAME    shortcut for --config-dir configs/NAME (e.g. nobel)
  --config-dir D load router containers & thrift ports from D/*/controller.yaml
"""
import argparse
import glob
import os
import re
import struct
import subprocess
import zlib

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

THRIFT_PORTS = {"r1": 9090, "r2": 9091, "r3": 9092, "r4": 9093}
REGISTERS = ["reg_signature", "reg_egress_port", "reg_saved_ttl", "reg_timestamp"]
FFT_TIMEOUT_US = 2_000_000
HOSTS = ["h1", "h2"]
PROTO = {"tcp": 6, "udp": 17}


def load_router_specs(config_dir=None):
    """Return ({router: (container, thrift_port)}, lab_prefix)."""
    if not config_dir:
        return (
            {r: (f"clab-p4-basic-lab-{r}", port) for r, port in THRIFT_PORTS.items()},
            "clab-p4-basic-lab",
        )
    specs = {}
    lab_prefix = "clab-p4-basic-lab"
    for path in sorted(glob.glob(os.path.join(config_dir, "*", "controller.yaml"))):
        with open(path) as f:
            conf = yaml.safe_load(f)
        name = conf["name"]
        netns = conf["netns"]
        thrift_port = conf.get("thrift_port", 9089 + int(conf["device_id"]))
        specs[name] = (netns, thrift_port)
        if netns.endswith(f"-{name}"):
            lab_prefix = netns[: -(len(name) + 1)]
    return specs, lab_prefix


def read_registers(container_name, thrift_port):
    cmds = "".join(f"register_read FamtarIngress.{r}\n" for r in REGISTERS)
    out = subprocess.run(
        ["docker", "exec", "-i", container_name,
         "simple_switch_CLI", "--thrift-port", str(thrift_port)],
        input=cmds, capture_output=True, text=True, check=True).stdout
    regs = {}
    for m in re.finditer(r"FamtarIngress\.(\w+)=\s*([\d,\s]+)", out):
        regs[m.group(1)] = [int(v) for v in m.group(2).replace(",", " ").split()]
    return regs


def switch_uptime_us(container_name):
    # ingress_global_timestamp counts microseconds since simple_switch started
    out = subprocess.run(
        ["docker", "exec", container_name, "ps", "-o", "etimes=", "-C", "simple_switch_grpc"],
        capture_output=True, text=True).stdout.split()
    return int(out[0]) * 1_000_000 if out else None


def flow_signature(src, dst, sport, dport, proto):
    # same field order as the hash() call in famtar_router.p4
    data = struct.pack("!4s4sHHB", bytes(map(int, src.split("."))),
                       bytes(map(int, dst.split("."))), sport, dport, proto)
    return zlib.crc32(data) % 0xFFFFFFFF


def known_flows(lab_prefix):
    """Map signature -> readable 5-tuple, from sockets open on the hosts (both directions)."""
    flows = {}
    for host in HOSTS:
        out = subprocess.run(["docker", "exec", f"{lab_prefix}-{host}", "netstat", "-tun"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] not in PROTO:
                continue
            (src, sport), (dst, dport) = (p.rsplit(":", 1) for p in parts[3:5])
            # iperf3 servers listen on :: so their sockets show IPv4-mapped addresses
            src, dst = src.removeprefix("::ffff:"), dst.removeprefix("::ffff:")
            if ":" in src or ":" in dst:
                continue
            for a, ap, b, bp in ((src, sport, dst, dport), (dst, dport, src, sport)):
                sig = flow_signature(a, b, int(ap), int(bp), PROTO[parts[0]])
                flows[sig] = f"{parts[0]} {a}:{ap} -> {b}:{bp}"
    return flows


def dump(router, container_name, thrift_port, flows, show_all):
    regs = read_registers(container_name, thrift_port)
    now = switch_uptime_us(container_name)
    rows = []
    for idx, sig in enumerate(regs["reg_signature"]):
        if sig == 0:
            continue
        ts = regs["reg_timestamp"][idx]
        age = (now - ts) if now is not None else None
        expired = age is not None and age > FFT_TIMEOUT_US
        if expired and not show_all:
            continue
        rows.append((idx, sig, regs["reg_egress_port"][idx], regs["reg_saved_ttl"][idx],
                     age, expired))

    print(f"== {router}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    if not rows:
        return
    print(f"  {'idx':>5}  {'signature':>10}  {'port':>4}  {'ttl':>3}  {'idle[s]':>7}  flow")
    for idx, sig, port, ttl, age, expired in sorted(rows, key=lambda r: r[4] or 0):
        idle = f"~{age / 1e6:.0f}" if age is not None else "?"
        label = flows.get(sig, "?")
        mark = "  (expired)" if expired else ""
        print(f"  {idx:>5}  {sig:>10}  {port:>4}  {ttl:>3}  {idle:>7}  {label}{mark}")


def main():
    parser = argparse.ArgumentParser(description="Dump non-empty FFT slots from BMv2 registers.")
    parser.add_argument("routers", nargs="*", help="Subset of routers to dump")
    parser.add_argument("--all", action="store_true", help="Show expired entries too")
    parser.add_argument("--topo", help="Topology shortcut (e.g. nobel -> configs/nobel)")
    parser.add_argument("--config-dir", help="Directory containing <router>/controller.yaml")
    args = parser.parse_args()

    config_dir = args.config_dir
    if not config_dir and args.topo:
        config_dir = os.path.join(PROJECT_ROOT, "configs", args.topo)

    specs, lab_prefix = load_router_specs(config_dir)
    routers = args.routers or list(specs.keys())
    flows = known_flows(lab_prefix)
    for r in routers:
        container_name, thrift_port = specs[r]
        dump(r, container_name, thrift_port, flows, args.all)


if __name__ == "__main__":
    main()
