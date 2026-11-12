#!/usr/bin/env python3
"""FAMTAR Benchmark UDP Traffic Receiver.

Listens for UDP packets from traffic_sender.py, calculates throughput per second,
loss per flow, one-way delay, and dumps detailed metrics to a JSON file.
"""

import argparse
import json
import os
import signal
import socket
import struct
import sys
import time

MAGIC = 0xFA4D  # 'FA' 'MD' magic bytes
HEADER_FORMAT = "!HHId"  # magic (uint16), flow_id (uint16), seq_no (uint32), send_ts (double)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


class TrafficReceiver:
    def __init__(self, bind_ip="0.0.0.0", port=5000, output_file="/tmp/receiver_stats.json",
                 max_duration=None, idle_timeout=15.0):
        self.bind_ip = bind_ip
        self.port = port
        self.output_file = output_file
        self.max_duration = max_duration
        self.idle_timeout = idle_timeout
        self.running = True

        self.start_time = None
        self.last_pkt_time = None
        self.total_bytes = 0
        self.total_packets = 0
        self.total_lost = 0

        # Flow tracking: flow_id -> dict
        self.flows = {}

        # 1-second timeseries buckets: sec_index (int) -> dict
        self.buckets = {}

    def handle_signal(self, signum, frame):
        self.running = False

    def process_packet(self, data, now):
        pkt_len = len(data)
        if pkt_len < HEADER_SIZE:
            return

        magic, flow_id, seq_no, send_ts = struct.unpack_from(HEADER_FORMAT, data)
        if magic != MAGIC:
            return

        if self.start_time is None:
            self.start_time = now

        self.last_pkt_time = now
        self.total_bytes += pkt_len
        self.total_packets += 1

        delay_ms = max(0.0, (now - send_ts) * 1000.0)

        # Flow loss tracking
        lost_in_pkt = 0
        if flow_id not in self.flows:
            self.flows[flow_id] = {
                "first_seq": seq_no,
                "last_seq": seq_no,
                "received_pkts": 1,
                "received_bytes": pkt_len,
                "lost_pkts": 0,
                "first_time": now,
                "last_time": now,
            }
        else:
            fl = self.flows[flow_id]
            fl["received_pkts"] += 1
            fl["received_bytes"] += pkt_len
            fl["last_time"] = now
            if seq_no > fl["last_seq"] + 1:
                gap = seq_no - fl["last_seq"] - 1
                lost_in_pkt = gap
                fl["lost_pkts"] += gap
                self.total_lost += gap
            if seq_no > fl["last_seq"]:
                fl["last_seq"] = seq_no

        # 1-second interval bucket
        sec_idx = int(now - self.start_time)
        if sec_idx not in self.buckets:
            self.buckets[sec_idx] = {
                "sec": sec_idx,
                "bytes": 0,
                "pkts": 0,
                "lost": 0,
                "delay_sum": 0.0,
                "flows": set(),
            }

        b = self.buckets[sec_idx]
        b["bytes"] += pkt_len
        b["pkts"] += 1
        b["lost"] += lost_in_pkt
        b["delay_sum"] += delay_ms
        b["flows"].add(flow_id)

    def run(self):
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Try setting larger socket receive buffer (2 MB)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        except Exception:
            pass

        sock.bind((self.bind_ip, self.port))
        sock.settimeout(1.0)
        print(f"[*] Traffic receiver listening on {self.bind_ip}:{self.port}...", flush=True)

        while self.running:
            now = time.time()

            if self.max_duration and self.start_time:
                if (now - self.start_time) >= self.max_duration:
                    print(f"[*] Max duration ({self.max_duration}s) reached.", flush=True)
                    break

            if self.start_time and self.last_pkt_time:
                if (now - self.last_pkt_time) >= self.idle_timeout:
                    print(f"[*] Idle timeout ({self.idle_timeout}s) with no packets. Finishing.", flush=True)
                    break

            try:
                data, addr = sock.recvfrom(2048)
                self.process_packet(data, now)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Socket error: {e}", file=sys.stderr)
                break

        sock.close()
        self.save_results()

    def save_results(self):
        end_time = self.last_pkt_time or time.time()
        duration = max(0.001, (end_time - self.start_time)) if self.start_time else 0.0

        ts_list = []
        for sec in sorted(self.buckets.keys()):
            b = self.buckets[sec]
            pkts = b["pkts"]
            mean_delay = round(b["delay_sum"] / pkts, 2) if pkts > 0 else 0.0
            mbps = round((b["bytes"] * 8.0) / 1e6, 3)
            ts_list.append({
                "sec": sec,
                "bytes": b["bytes"],
                "mbps": mbps,
                "pkts": pkts,
                "lost": b["lost"],
                "active_flows": len(b["flows"]),
                "mean_delay_ms": mean_delay,
            })

        loss_pct = round((self.total_lost / max(1, self.total_packets + self.total_lost)) * 100.0, 3)
        overall_mbps = round((self.total_bytes * 8.0) / (duration * 1e6), 3) if duration > 0 else 0.0

        # Summarize window [20, 230] seconds if duration is sufficient
        window_bytes = 0
        window_pkts = 0
        window_lost = 0
        window_secs = 0
        for entry in ts_list:
            if 20 <= entry["sec"] <= 230:
                window_bytes += entry["bytes"]
                window_pkts += entry["pkts"]
                window_lost += entry["lost"]
                window_secs += 1

        window_mbps = round((window_bytes * 8.0) / (max(1, window_secs) * 1e6), 3) if window_secs > 0 else 0.0
        window_loss_pct = round((window_lost / max(1, window_pkts + window_lost)) * 100.0, 3)

        out_data = {
            "summary": {
                "total_flows": len(self.flows),
                "total_bytes": self.total_bytes,
                "total_packets": self.total_packets,
                "total_lost": self.total_lost,
                "loss_pct": loss_pct,
                "duration_sec": round(duration, 2),
                "overall_mbps": overall_mbps,
                "window_20_230": {
                    "active_secs": window_secs,
                    "total_bytes": window_bytes,
                    "total_pkts": window_pkts,
                    "total_lost": window_lost,
                    "avg_mbps": window_mbps,
                    "loss_pct": window_loss_pct,
                }
            },
            "timeseries": ts_list,
        }

        os.makedirs(os.path.dirname(os.path.abspath(self.output_file)), exist_ok=True)
        with open(self.output_file, "w") as f:
            json.dump(out_data, f, indent=2)

        print(f"[+] Results saved to {self.output_file}", flush=True)
        print(f"    Total packets: {self.total_packets}, Lost: {self.total_lost} ({loss_pct}%)", flush=True)
        print(f"    Avg throughput: {overall_mbps} Mbit/s (Window [20,230]s: {window_mbps} Mbit/s)", flush=True)


def main():
    parser = argparse.ArgumentParser(description="FAMTAR Benchmark UDP Traffic Receiver")
    parser.add_argument("--bind", default="0.0.0.0", help="IP address to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="UDP port (default: 5000)")
    parser.add_argument("--output", default="/tmp/receiver_stats.json", help="Path to output JSON")
    parser.add_argument("--max-duration", type=float, default=None, help="Maximum test duration in seconds")
    parser.add_argument("--idle-timeout", type=float, default=15.0, help="Idle timeout to terminate after traffic ends")
    args = parser.parse_args()

    receiver = TrafficReceiver(
        bind_ip=args.bind,
        port=args.port,
        output_file=args.output,
        max_duration=args.max_duration,
        idle_timeout=args.idle_timeout
    )
    receiver.run()


if __name__ == "__main__":
    main()
