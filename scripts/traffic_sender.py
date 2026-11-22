#!/usr/bin/env python3
"""FAMTAR Benchmark UDP Traffic Sender.

Generates 500 UDP flows (or configurable number) according to the parameters from
the FAMTAR paper:
- Inter-arrival time: Exponential distribution (mean: 0.5s)
- Flow size: Pareto distribution (mean: 1 MB, shape: 1.25)
- Rate per flow: Constant Bit Rate (100 kB/s)
- Packet size: 1000 bytes
- Fixed seed for reproducible comparisons between ECMP and FAMTAR
"""

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import struct
import sys
import time

MAGIC = 0xFA4D  # 'FA' 'MD'
HEADER_FORMAT = "!HHId"  # magic (uint16), flow_id (uint16), seq_no (uint32), send_ts (double)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class TrafficSender:
    def __init__(self, target_ip="10.0.2.1", target_port=5000, num_flows=500,
                 rate_kbs=100.0, pkt_size=1000, mean_size_mb=1.0,
                 pareto_shape=1.25, mean_iat=0.5, seed=42,
                 base_sport=10000, max_duration=None, output_file="/tmp/sender_stats.json"):
        self.target_ip = target_ip
        self.target_port = target_port
        self.target_addr = (target_ip, target_port)
        self.num_flows = num_flows
        self.rate_kbs = rate_kbs
        self.pkt_size = pkt_size
        self.mean_size_mb = mean_size_mb
        self.pareto_shape = pareto_shape
        self.mean_iat = mean_iat
        self.seed = seed
        self.base_sport = base_sport
        self.max_duration = max_duration
        self.output_file = output_file

        self.running = True
        self.stats = {}
        self.total_pkts_sent = 0
        self.total_bytes_sent = 0
        self.buckets = {}

        # Pre-computed padding
        payload_len = max(0, self.pkt_size - HEADER_SIZE)
        self.padding = b"\x00" * payload_len

        # Flow parameters plan
        self.plan = []

    def build_plan(self):
        random.seed(self.seed)

        # Pareto scale xm: E[X] = (shape * xm) / (shape - 1) => xm = E[X] * (shape - 1) / shape
        mean_bytes = self.mean_size_mb * 1_000_000
        xm = mean_bytes * (self.pareto_shape - 1.0) / self.pareto_shape

        pps = (self.rate_kbs * 1000.0) / self.pkt_size
        pkt_interval = 1.0 / pps

        curr_start = 0.0
        for i in range(self.num_flows):
            if i > 0:
                iat = random.expovariate(1.0 / self.mean_iat)
                curr_start += iat

            flow_bytes = random.paretovariate(self.pareto_shape) * xm
            total_pkts = max(1, int(round(flow_bytes / self.pkt_size)))
            sport = self.base_sport + (i % 50000)

            self.plan.append({
                "flow_id": i,
                "start_time": curr_start,
                "total_bytes": total_pkts * self.pkt_size,
                "total_pkts": total_pkts,
                "sport": sport,
                "pkt_interval": pkt_interval,
            })

        print(f"[*] Generated plan for {self.num_flows} flows (seed={self.seed}):", flush=True)
        print(f"    Avg planned size: {sum(f['total_bytes'] for f in self.plan)/len(self.plan)/1e6:.2f} MB", flush=True)
        print(f"    Last flow starts at t={self.plan[-1]['start_time']:.1f} s", flush=True)

    async def run_flow(self, flow_info, global_start_time):
        flow_id = flow_info["flow_id"]
        start_delay = flow_info["start_time"]
        total_pkts = flow_info["total_pkts"]
        sport = flow_info["sport"]
        interval = flow_info["pkt_interval"]

        # Wait until scheduled start
        await asyncio.sleep(start_delay)
        if not self.running:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", sport))
        except Exception as e:
            # Fallback to random ephemeral port if bind fails
            pass
        sock.setblocking(False)

        loop = asyncio.get_running_loop()
        next_send = loop.time()
        pkts_sent = 0

        for seq in range(total_pkts):
            if not self.running:
                break

            now_wall = time.time()
            if self.max_duration and (now_wall - global_start_time) >= self.max_duration:
                break

            header = struct.pack(HEADER_FORMAT, MAGIC, flow_id, seq, now_wall)
            packet_data = header + self.padding

            try:
                sock.sendto(packet_data, self.target_addr)
                pkts_sent += 1
                self.total_pkts_sent += 1
                pkt_len = len(packet_data)
                self.total_bytes_sent += pkt_len
                sec_idx = int(now_wall - global_start_time)
                b = self.buckets.get(sec_idx)
                if b is None:
                    b = {"sec": sec_idx, "pkts": 0, "bytes": 0}
                    self.buckets[sec_idx] = b
                b["pkts"] += 1
                b["bytes"] += pkt_len
            except BlockingIOError:
                pass
            except Exception as e:
                break

            next_send += interval
            sleep_time = next_send - loop.time()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        sock.close()
        self.stats[flow_id] = {
            "pkts_sent": pkts_sent,
            "bytes_sent": pkts_sent * self.pkt_size,
        }

    async def main_async(self):
        self.build_plan()
        global_start_time = time.time()
        print(f"[*] Starting traffic generation to {self.target_addr}...", flush=True)

        tasks = [
            asyncio.create_task(self.run_flow(f, global_start_time))
            for f in self.plan
        ]

        # Monitor loop
        monitor_task = asyncio.create_task(self.monitor_progress(global_start_time))

        await asyncio.gather(*tasks, return_exceptions=True)
        self.running = False
        monitor_task.cancel()

        duration = time.time() - global_start_time
        print(f"[+] All flows finished in {duration:.1f} s", flush=True)
        self.save_results(duration)

    async def monitor_progress(self, global_start_time):
        while self.running:
            await asyncio.sleep(5.0)
            elapsed = time.time() - global_start_time
            active = len([f for f in self.plan if f["flow_id"] not in self.stats and f["start_time"] <= elapsed])
            mbps = (self.total_bytes_sent * 8.0) / (max(1.0, elapsed) * 1e6)
            print(f"[{elapsed:5.1f}s] Sent {self.total_pkts_sent} pkts ({self.total_bytes_sent/1e6:.1f} MB) | "
                  f"Active flows: ~{active} | Avg rate: {mbps:.2f} Mbit/s", flush=True)

    def save_results(self, duration):
        mbps = (self.total_bytes_sent * 8.0) / (max(0.001, duration) * 1e6)
        ts_list = [
            {
                "sec": sec,
                "pkts": self.buckets[sec]["pkts"],
                "bytes": self.buckets[sec]["bytes"],
                "mbps": round((self.buckets[sec]["bytes"] * 8.0) / 1e6, 3),
            }
            for sec in sorted(self.buckets.keys())
        ]
        out = {
            "summary": {
                "num_flows": self.num_flows,
                "total_pkts_sent": self.total_pkts_sent,
                "total_bytes_sent": self.total_bytes_sent,
                "duration_sec": round(duration, 2),
                "avg_mbps": round(mbps, 3),
                "seed": self.seed,
            },
            "plan_summary": {
                "mean_size_mb": self.mean_size_mb,
                "pareto_shape": self.pareto_shape,
                "mean_iat": self.mean_iat,
                "rate_kbs": self.rate_kbs,
            },
            "timeseries": ts_list,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.output_file)), exist_ok=True)
        with open(self.output_file, "w") as f:
            json.dump(out, f, indent=2)
        print(f"[+] Sender stats saved to {self.output_file}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="FAMTAR Benchmark UDP Traffic Sender")
    parser.add_argument("--target-ip", default="10.0.2.1", help="Target IP (default: 10.0.2.1)")
    parser.add_argument("--target-port", type=int, default=5000, help="Target UDP port (default: 5000)")
    parser.add_argument("--num-flows", type=int, default=500, help="Number of flows (default: 500)")
    parser.add_argument("--rate-kbs", type=float, default=100.0, help="Rate per flow in kB/s (default: 100.0)")
    parser.add_argument("--pkt-size", type=int, default=1000, help="Packet size in bytes (default: 1000)")
    parser.add_argument("--mean-size-mb", type=float, default=1.0, help="Mean flow size in MB (default: 1.0)")
    parser.add_argument("--pareto-shape", type=float, default=1.25, help="Pareto shape parameter (default: 1.25)")
    parser.add_argument("--mean-iat", type=float, default=0.5, help="Mean inter-arrival time in seconds (default: 0.5)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--base-sport", type=int, default=10000, help="Base source port (default: 10000)")
    parser.add_argument("--max-duration", type=float, default=None, help="Maximum run duration in seconds")
    parser.add_argument("--output", default="/tmp/sender_stats.json", help="Path to output JSON")
    args = parser.parse_args()

    sender = TrafficSender(
        target_ip=args.target_ip,
        target_port=args.target_port,
        num_flows=args.num_flows,
        rate_kbs=args.rate_kbs,
        pkt_size=args.pkt_size,
        mean_size_mb=args.mean_size_mb,
        pareto_shape=args.pareto_shape,
        mean_iat=args.mean_iat,
        seed=args.seed,
        base_sport=args.base_sport,
        max_duration=args.max_duration,
        output_file=args.output
    )
    asyncio.run(sender.main_async())


if __name__ == "__main__":
    main()
