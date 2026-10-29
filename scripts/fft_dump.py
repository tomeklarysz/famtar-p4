#!/usr/bin/env python3
"""Dump non-empty FFT slots from the BMv2 registers of each router.

Usage: scripts/fft_dump.py [r1 r2 ...] [--all]
  --all   show expired entries too (older than FFT_TIMEOUT)
"""
import re
import struct
import subprocess
import sys
import zlib

THRIFT_PORTS = {"r1": 9090, "r2": 9091, "r3": 9092, "r4": 9093}
REGISTERS = ["reg_signature", "reg_egress_port", "reg_saved_ttl", "reg_timestamp"]
FFT_TIMEOUT_US = 2_000_000
HOSTS = ["h1", "h2"]
PROTO = {"tcp": 6, "udp": 17}


def container(name):
    return f"clab-p4-basic-lab-{name}"


def read_registers(router):
    cmds = "".join(f"register_read MyIngress.{r}\n" for r in REGISTERS)
    out = subprocess.run(
        ["docker", "exec", "-i", container(router),
         "simple_switch_CLI", "--thrift-port", str(THRIFT_PORTS[router])],
        input=cmds, capture_output=True, text=True, check=True).stdout
    regs = {}
    for m in re.finditer(r"MyIngress\.(\w+)=\s*([\d,\s]+)", out):
        regs[m.group(1)] = [int(v) for v in m.group(2).replace(",", " ").split()]
    return regs


def switch_uptime_us(router):
    # ingress_global_timestamp counts microseconds since simple_switch started
    out = subprocess.run(
        ["docker", "exec", container(router), "ps", "-o", "etimes=", "-C", "simple_switch_grpc"],
        capture_output=True, text=True).stdout.split()
    return int(out[0]) * 1_000_000 if out else None


def flow_signature(src, dst, sport, dport, proto):
    # same field order as the hash() call in basic_forwarding.p4
    data = struct.pack("!4s4sHHB", bytes(map(int, src.split("."))),
                       bytes(map(int, dst.split("."))), sport, dport, proto)
    return zlib.crc32(data) % 0xFFFFFFFF


def known_flows():
    """Map signature -> readable 5-tuple, from sockets open on the hosts (both directions)."""
    flows = {}
    for host in HOSTS:
        out = subprocess.run(["docker", "exec", container(host), "netstat", "-tun"],
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


def dump(router, flows, show_all):
    regs = read_registers(router)
    now = switch_uptime_us(router)
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    routers = args or list(THRIFT_PORTS)
    flows = known_flows()
    for r in routers:
        dump(r, flows, show_all)


if __name__ == "__main__":
    main()
