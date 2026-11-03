#!/usr/bin/env python3
import argparse
import json
import os
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_iperf(filepath, offset_sec=0):
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r") as f:
        data = json.load(f)
    end = data.get("end", {})
    sum_sent = end.get("sum_sent", {})
    sum_rcv = end.get("sum_received", {})
    streams = end.get("streams", [])
    mean_rtt = (
        streams[0]["sender"].get("mean_rtt", 0) / 1000.0
        if streams and "sender" in streams[0]
        else 0.0
    )

    series = {}
    for iv in data.get("intervals", []):
        s = iv.get("sum", {})
        t = int(round(s.get("end", 0))) + offset_sec
        series[t] = round(s.get("bits_per_second", 0) / 1e6, 2)

    return {
        "sent_mbps": round(sum_sent.get("bits_per_second", 0) / 1e6, 2),
        "rcv_mbps": round(sum_rcv.get("bits_per_second", 0) / 1e6, 2),
        "retransmits": int(sum_sent.get("retransmits", 0)),
        "mean_rtt_ms": round(mean_rtt, 2),
        "series": series,
    }


def count_packets(filepath, port):
    if not os.path.exists(filepath):
        return 0
    with open(filepath, "r", errors="ignore") as f:
        return sum(1 for line in f if f".{port}:" in line or f".{port} " in line)


def extract_series(data):
    s1 = {int(k): v for k, v in ((data.get("flow1") or {}).get("series") or {}).items()}
    s2 = {int(k): v for k, v in ((data.get("flow2") or {}).get("series") or {}).items()}
    max_t = max(list(s1.keys()) + list(s2.keys()) + [16])
    times = list(range(1, max_t + 1))
    y1 = [s1.get(t, 0.0) for t in times]
    y2 = [s2.get(t, 0.0) for t in times]
    ytot = [a + b for a, b in zip(y1, y2)]
    return times, y1, y2, ytot, max_t


def plot_single_throughput(summary, out_path):
    times, y1, y2, ytot, max_t = extract_series(summary)

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=200)
    ax.stackplot(
        times,
        y1,
        y2,
        labels=[
            f"Flow 1 (port 5201) -> {summary.get('flow1_path')}",
            f"Flow 2 (port 5202) -> {summary.get('flow2_path')}",
        ],
        colors=["#4c72b0", "#55a868"],
        alpha=0.75,
    )
    ax.plot(times, ytot, color="black", linewidth=2.0, label="Sumaryczny Throughput")
    ax.axhline(10.0, color="#c44e52", linestyle="--", linewidth=1.3, label="Pojemność 1 łącza (10 Mbit/s)")
    ax.axhline(9.0, color="#dd8452", linestyle=":", linewidth=1.2, label="Próg FAMTAR Th_max (9 Mbit/s)")

    ax.set_title(f"{summary['scenario']} — Tryb: {summary['mode'].upper()}", fontsize=12, fontweight="bold")
    ax.set_xlabel("Czas [s]")
    ax.set_ylabel("Przepustowość [Mbit/s]")
    ax.set_xlim(1, max_t)
    ax.set_ylim(0, 20)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="upper left", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_comparison_throughput(scenario, ecmp_data, famtar_data, out_ts_path, out_bars_path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), dpi=200, sharey=True)
    for ax, data, title in zip(
        axes,
        [ecmp_data, famtar_data],
        ["Klasyczny ECMP", "FAMTAR (P4 + IS-IS)"],
    ):
        times, y1, y2, ytot, max_t = extract_series(data)
        ax.stackplot(
            times,
            y1,
            y2,
            labels=[
                f"Flow 1 ({data.get('flow1_path')})",
                f"Flow 2 ({data.get('flow2_path')})",
            ],
            colors=["#4c72b0", "#55a868"],
            alpha=0.75,
        )
        ax.plot(times, ytot, color="black", linewidth=2.0, label="Suma (Total)")
        ax.axhline(10.0, color="#c44e52", linestyle="--", linewidth=1.2, label="Limit 1 łącza (10 Mbit/s)")
        ax.set_title(
            f"{title}\nŚr. suma: {data.get('aggregate_rcv_mbps')} Mbit/s | Retransmisje: {data.get('total_retransmits')}",
            fontsize=10.5,
            fontweight="bold",
        )
        ax.set_xlabel("Czas [s]")
        ax.set_xlim(1, max_t)
        ax.set_ylim(0, 20)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper left", fontsize=8)

    axes[0].set_ylabel("Przepustowość [Mbit/s]")
    fig.suptitle(f"Porównanie w funkcji czasu: {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_ts_path)
    plt.close(fig)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.2), dpi=200)
    modes = ["ECMP", "FAMTAR"]
    f1_vals = [ecmp_data["flow1"]["rcv_mbps"], famtar_data["flow1"]["rcv_mbps"]]
    f2_vals = [ecmp_data["flow2"]["rcv_mbps"], famtar_data["flow2"]["rcv_mbps"]]
    tot_vals = [ecmp_data["aggregate_rcv_mbps"], famtar_data["aggregate_rcv_mbps"]]

    x = [0, 1]
    w = 0.25
    b1 = ax1.bar([i - w for i in x], f1_vals, width=w, label="Flow 1 (5201)", color="#4c72b0")
    b2 = ax1.bar(x, f2_vals, width=w, label="Flow 2 (5202)", color="#55a868")
    b3 = ax1.bar([i + w for i in x], tot_vals, width=w, label="Suma (Aggregate)", color="#2b4162")
    ax1.axhline(10.0, color="#c44e52", linestyle="--", linewidth=1.1, label="Pojemność 1 łącza")
    ax1.set_xticks(x)
    ax1.set_xticklabels(modes, fontweight="bold")
    ax1.set_ylabel("Przepustowość [Mbit/s]")
    ax1.set_ylim(0, 20)
    ax1.set_title("Przepustowość (Throughput)", fontweight="bold")
    ax1.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax1.legend(fontsize=8)
    for bars in (b1, b2, b3):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8)

    retrans = [ecmp_data["total_retransmits"], famtar_data["total_retransmits"]]
    br = ax2.bar(x, retrans, width=0.4, color=["#c44e52", "#55a868"])
    ax2.set_xticks(x)
    ax2.set_xticklabels(modes, fontweight="bold")
    ax2.set_ylabel("Liczba retransmisji TCP")
    ax2.set_title("Straty / Retransmisje TCP", fontweight="bold")
    ax2.grid(True, axis="y", linestyle="--", alpha=0.4)
    for bar in br:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width() / 2, h),
                     xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9, fontweight="bold")

    fig.suptitle(f"Zestawienie metryk: {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_bars_path)
    plt.close(fig)


def handle_throughput(args):
    flow1 = parse_iperf(os.path.join(args.dir, "flow1.json"), offset_sec=0)
    flow2 = parse_iperf(os.path.join(args.dir, "flow2.json"), offset_sec=args.stagger)

    r2_f1 = count_packets(os.path.join(args.dir, "tcpdump_r2.txt"), 5201)
    r2_f2 = count_packets(os.path.join(args.dir, "tcpdump_r2.txt"), 5202)
    r3_f1 = count_packets(os.path.join(args.dir, "tcpdump_r3.txt"), 5201)
    r3_f2 = count_packets(os.path.join(args.dir, "tcpdump_r3.txt"), 5202)

    p1 = "Ścieżka 1 (r2)" if r2_f1 >= r3_f1 else "Ścieżka 2 (r3)"
    p2 = "Ścieżka 1 (r2)" if r2_f2 >= r3_f2 else "Ścieżka 2 (r3)"

    tot_rcv = round((flow1["rcv_mbps"] if flow1 else 0.0) + (flow2["rcv_mbps"] if flow2 else 0.0), 2)
    tot_ret = (flow1["retransmits"] if flow1 else 0) + (flow2["retransmits"] if flow2 else 0)

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "flow1": flow1,
        "flow2": flow2,
        "flow1_path": p1,
        "flow2_path": p2,
        "packets": {"r2_f1": r2_f1, "r3_f1": r3_f1, "r2_f2": r2_f2, "r3_f2": r3_f2},
        "aggregate_rcv_mbps": tot_rcv,
        "total_retransmits": tot_ret,
    }

    with open(os.path.join(args.dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    single_plot = os.path.join(args.dir, "throughput_timeseries.png")
    if flow1 and flow2:
        plot_single_throughput(summary, single_plot)
        print(
            f"[{args.scenario}:{args.mode}] "
            f"f1={flow1['rcv_mbps']:.2f}M ({p1}) "
            f"f2={flow2['rcv_mbps']:.2f}M ({p2}) "
            f"total={tot_rcv:.2f}M retrans={tot_ret}"
        )

    base_results = os.path.dirname(args.dir.rstrip("/"))
    ecmp_json = os.path.join(base_results, f"{args.scenario}_ecmp", "summary.json")
    famtar_json = os.path.join(base_results, f"{args.scenario}_famtar", "summary.json")
    if os.path.exists(ecmp_json) and os.path.exists(famtar_json):
        with open(ecmp_json) as f1, open(famtar_json) as f2:
            e_data, f_data = json.load(f1), json.load(f2)
        ts_cmp = os.path.join(base_results, f"{args.scenario}_comparison_timeseries.png")
        bar_cmp = os.path.join(base_results, f"{args.scenario}_comparison_bars.png")
        plot_comparison_throughput(args.scenario, e_data, f_data, ts_cmp, bar_cmp)


def parse_ping_series(filepath):
    rtts = []
    loss_pct = 0.0
    if not os.path.exists(filepath):
        return rtts, loss_pct
    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            m = re.search(r"seq=(\d+).*time=([0-9.]+)\s*ms", line)
            if m:
                rtts.append((round(int(m.group(1)) * 0.1, 1), float(m.group(2))))
            m_loss = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", line)
            if m_loss:
                loss_pct = float(m_loss.group(1))
    return rtts, loss_pct


def parse_icmp_switch_time(r2_file, r3_file):
    def extract_ts_seq(path):
        if not os.path.exists(path):
            return []
        items = []
        with open(path, "r", errors="ignore") as f:
            for line in f:
                m = re.search(r"^([0-9.]+)\s+IP.*icmp.*seq\s+(\d+)", line, re.IGNORECASE)
                if m:
                    items.append((float(m.group(1)), int(m.group(2))))
        return items

    r2_pkts = extract_ts_seq(r2_file)
    r3_pkts = extract_ts_seq(r3_file)
    first_r3_seq = r3_pkts[0][1] if r3_pkts else None
    return len(r2_pkts), len(r3_pkts), first_r3_seq


def handle_qos(args):
    rtts, loss_pct = parse_ping_series(os.path.join(args.dir, "ping.txt"))
    r2_cnt, r3_cnt, first_r3_seq = parse_icmp_switch_time(
        os.path.join(args.dir, "icmp_r2.txt"),
        os.path.join(args.dir, "icmp_r3.txt"),
    )

    t_start = float(args.stagger)
    t_end = t_start + float(args.elephant_duration)

    baseline_vals = [v for t, v in rtts if t < t_start]
    steady_vals = [v for t, v in rtts if (t_start + 1.5) <= t <= t_end]
    peak_val = max([v for t, v in rtts if t_start <= t <= t_end], default=0.0)

    avg_base = round(sum(baseline_vals) / len(baseline_vals), 2) if baseline_vals else 0.0
    avg_steady = round(sum(steady_vals) / len(steady_vals), 2) if steady_vals else 0.0

    switch_time_s = round(first_r3_seq * 0.1, 2) if first_r3_seq is not None else None
    reaction_time_s = (
        round(max(0.0, switch_time_s - t_start), 2)
        if switch_time_s is not None and switch_time_s >= t_start
        else None
    )

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "baseline_rtt_ms": avg_base,
        "steady_congested_rtt_ms": avg_steady,
        "peak_rtt_ms": round(peak_val, 2),
        "loss_pct": loss_pct,
        "icmp_packets": {"r2": r2_cnt, "r3": r3_cnt},
        "switch_time_s": switch_time_s,
        "reaction_time_s": reaction_time_s,
        "rtt_series": rtts,
    }

    with open(os.path.join(args.dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    single_plot = os.path.join(args.dir, "rtt_timeseries.png")
    if rtts:
        fig, ax = plt.subplots(figsize=(8.5, 4.3), dpi=200)
        xs = [p[0] for p in rtts]
        ys = [p[1] for p in rtts]
        ax.plot(xs, ys, color="#1f77b4" if args.mode == "famtar" else "#d62728", linewidth=1.8, label=f"RTT ({args.mode.upper()})")
        ax.axvspan(t_start, t_end, color="gray", alpha=0.18, label="Aktywny Elephant Flow (10 Mbit/s)")
        if switch_time_s is not None:
            ax.axvline(switch_time_s, color="#2ca02c", linestyle="--", linewidth=1.5,
                       label=f"Przełączenie trasy na r3 (t={switch_time_s}s, Δt={reaction_time_s}s)")
        ax.set_title(f"Scenariusz 3 (QoS / RTT) — Tryb: {args.mode.upper()}", fontweight="bold")
        ax.set_xlabel("Czas [s]")
        ax.set_ylabel("Opóźnienie RTT [ms]")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper right", fontsize=8.5)
        fig.tight_layout()
        fig.savefig(single_plot)
        plt.close(fig)

    print(
        f"[{args.scenario}:{args.mode}] "
        f"base_rtt={avg_base:.2f}ms steady_rtt={avg_steady:.2f}ms peak_rtt={peak_val:.2f}ms "
        f"icmp(r2={r2_cnt},r3={r3_cnt})"
    )

    base_results = os.path.dirname(args.dir.rstrip("/"))
    ecmp_json = os.path.join(base_results, f"{args.scenario}_ecmp", "summary.json")
    famtar_json = os.path.join(base_results, f"{args.scenario}_famtar", "summary.json")
    if os.path.exists(ecmp_json) and os.path.exists(famtar_json):
        with open(ecmp_json) as f1, open(famtar_json) as f2:
            e_data, f_data = json.load(f1), json.load(f2)
        cmp_plot = os.path.join(base_results, f"{args.scenario}_comparison_rtt.png")
        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=200)
        ex = [p[0] for p in e_data.get("rtt_series", [])]
        ey = [p[1] for p in e_data.get("rtt_series", [])]
        fx = [p[0] for p in f_data.get("rtt_series", [])]
        fy = [p[1] for p in f_data.get("rtt_series", [])]
        ax.plot(ex, ey, color="#c44e52", linewidth=1.8, alpha=0.85,
                label=f"ECMP (śr. RTT w zatorze: {e_data.get('steady_congested_rtt_ms')} ms)")
        ax.plot(fx, fy, color="#4c72b0", linewidth=2.0,
                label=f"FAMTAR (śr. RTT po adaptacji: {f_data.get('steady_congested_rtt_ms')} ms)")
        ax.axvspan(t_start, t_end, color="gray", alpha=0.15, label="Elephant Flow (10 Mbit/s)")
        if f_data.get("switch_time_s") is not None:
            st = f_data["switch_time_s"]
            rt = f_data["reaction_time_s"]
            ax.axvline(st, color="#2ca02c", linestyle="--", linewidth=1.4,
                       label=f"Adaptacja FAMTAR (Δt = {rt} s)")
        ax.set_title("Ochrona QoS (Mice RTT przy zatorze Elephant Flow): ECMP vs FAMTAR", fontsize=11.5, fontweight="bold")
        ax.set_xlabel("Czas [s]")
        ax.set_ylabel("Opóźnienie RTT [ms]")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper right", fontsize=8.5)
        fig.tight_layout()
        fig.savefig(cmp_plot)
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["throughput", "qos"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--stagger", type=int, default=2)
    parser.add_argument("--elephant-duration", type=int, default=10)
    args = parser.parse_args()

    if args.type == "throughput":
        handle_throughput(args)
    else:
        handle_qos(args)


if __name__ == "__main__":
    main()
