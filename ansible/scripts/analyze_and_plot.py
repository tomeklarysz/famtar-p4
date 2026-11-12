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
        if s.get("seconds", 1.0) < 0.5:
            continue
        t = int(round(s.get("end", 0))) + offset_sec
        series[t] = round(s.get("bits_per_second", 0) / 1e6, 2)

    return {
        "sent_mbps": round(sum_sent.get("bits_per_second", 0) / 1e6, 2),
        "rcv_mbps": round(sum_rcv.get("bits_per_second", 0) / 1e6, 2),
        "retransmits": int(sum_sent.get("retransmits", 0)),
        "mean_rtt_ms": round(mean_rtt, 2),
        "series": series,
    }


def parse_wire_throughput(r2_path, r3_path, stagger_sec=2, max_sec=16):
    counts = {"r2_f1": 0, "r3_f1": 0, "r2_f2": 0, "r3_f2": 0}
    pkts = []
    pat = re.compile(
        r"(\d+):(\d+):(\d+\.\d+)\s+IP\s+\S+\s+>\s+10\.0\.2\.1\.(5201|5202):.*length\s+(\d+)"
    )

    for tag, path in (("r2", r2_path), ("r3", r3_path)):
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="ignore") as f:
            for line in f:
                if ".5201:" in line or ".5201 " in line:
                    counts[f"{tag}_f1"] += 1
                elif ".5202:" in line or ".5202 " in line:
                    counts[f"{tag}_f2"] += 1
                m = pat.search(line)
                if m:
                    length = int(m.group(5))
                    if length > 0:
                        t = (
                            int(m.group(1)) * 3600
                            + int(m.group(2)) * 60
                            + float(m.group(3))
                        )
                        pkts.append((t, int(m.group(4)), length))

    if not pkts:
        return counts, None, None

    f2_data = [x[0] for x in pkts if x[1] == 5202 and x[2] > 1000]
    t0 = (min(f2_data) - float(stagger_sec)) if f2_data else min(x[0] for x in pkts)

    s1, s2 = {}, {}
    for sec in range(1, max_sec + 1):
        b1 = sum(x[2] for x in pkts if (sec - 1) <= (x[0] - t0) < sec and x[1] == 5201)
        b2 = sum(x[2] for x in pkts if (sec - 1) <= (x[0] - t0) < sec and x[1] == 5202)
        s1[sec] = round(b1 * 8 / 1e6, 2)
        s2[sec] = round(b2 * 8 / 1e6, 2)

    steady_range = list(range(stagger_sec + 2, max_sec + 1))
    avg1 = round(sum(s1[t] for t in steady_range) / len(steady_range), 2)
    avg2 = round(sum(s2[t] for t in steady_range) / len(steady_range), 2)

    return counts, (s1, avg1), (s2, avg2)


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

    counts, w1, w2 = parse_wire_throughput(
        os.path.join(args.dir, "tcpdump_r2.txt"),
        os.path.join(args.dir, "tcpdump_r3.txt"),
        stagger_sec=args.stagger,
    )
    if flow1 and w1:
        flow1["series"], flow1["rcv_mbps"] = w1
    if flow2 and w2:
        flow2["series"], flow2["rcv_mbps"] = w2

    p1 = "Ścieżka 1 (r2)" if counts["r2_f1"] >= counts["r3_f1"] else "Ścieżka 2 (r3)"
    p2 = "Ścieżka 1 (r2)" if counts["r2_f2"] >= counts["r3_f2"] else "Ścieżka 2 (r3)"

    tot_rcv = round((flow1["rcv_mbps"] if flow1 else 0.0) + (flow2["rcv_mbps"] if flow2 else 0.0), 2)
    tot_ret = (flow1["retransmits"] if flow1 else 0) + (flow2["retransmits"] if flow2 else 0)

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "flow1": flow1,
        "flow2": flow2,
        "flow1_path": p1,
        "flow2_path": p2,
        "packets": counts,
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


NOBEL_PATHS = [
    ("p1", "P1: fra-bru-par (56)", "#4c72b0"),
    ("p2", "P2: fra-str-par (59)", "#55a868"),
    ("p3", "P3: fra-ham-ams-lon-par (145)", "#dd8452"),
    ("p4", "P4: fra-mun-mil-zur-lyo-par (162)", "#8172b3"),
]
NOBEL_PORTS = [5201, 5202, 5203, 5204]


def parse_nobel_wire(results_dir, max_sec=24):
    """Parse tcpdump_p{1..4}.txt captured on Frankfurt's 4 egress transit links."""
    pat = re.compile(
        r"(\d+):(\d+):(\d+\.\d+)\s+IP\s+\S+\s+>\s+10\.0\.2\.1\.(520[1-4]):.*length\s+(\d+)"
    )
    counts = {p[0]: {port: 0 for port in NOBEL_PORTS} for p in NOBEL_PATHS}
    pkts = []  # (ts, path_key, port, length)

    for key, _, _ in NOBEL_PATHS:
        path = os.path.join(results_dir, f"tcpdump_{key}.txt")
        if not os.path.exists(path):
            continue
        with open(path, "r", errors="ignore") as f:
            for line in f:
                m = pat.search(line)
                if not m:
                    continue
                port = int(m.group(4))
                length = int(m.group(5))
                if length <= 0:
                    continue
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                counts[key][port] += 1
                pkts.append((t, key, port, length))

    if not pkts:
        return counts, {k: {} for k, _, _ in NOBEL_PATHS}, {p: {} for p in NOBEL_PORTS}

    f1_data = [x[0] for x in pkts if x[2] == 5201 and x[3] > 1000]
    t0 = min(f1_data) if f1_data else min(x[0] for x in pkts)

    path_series = {k: {} for k, _, _ in NOBEL_PATHS}
    flow_series = {p: {} for p in NOBEL_PORTS}
    for sec in range(1, max_sec + 1):
        bucket = [x for x in pkts if (sec - 1) <= (x[0] - t0) < sec]
        for k, _, _ in NOBEL_PATHS:
            b = sum(x[3] for x in bucket if x[1] == k)
            path_series[k][sec] = round(b * 8 / 1e6, 2)
        for p in NOBEL_PORTS:
            b = sum(x[3] for x in bucket if x[2] == p)
            flow_series[p][sec] = round(b * 8 / 1e6, 2)

    return counts, path_series, flow_series


def plot_nobel_single(summary, out_path):
    max_t = summary["duration"]
    times = list(range(1, max_t + 1))
    ys = [[summary["path_series"][k].get(str(t), summary["path_series"][k].get(t, 0.0)) for t in times]
          for k, _, _ in NOBEL_PATHS]
    ytot = [sum(col) for col in zip(*ys)]

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=200)
    ax.stackplot(
        times, *ys,
        labels=[label for _, label, _ in NOBEL_PATHS],
        colors=[color for _, _, color in NOBEL_PATHS],
        alpha=0.80,
    )
    ax.plot(times, ytot, color="black", linewidth=2.0, label="Suma wszystkich ścieżek")
    ax.axhline(5.0, color="#c44e52", linestyle="--", linewidth=1.2, label="Limit 1 ścieżki (5 Mbit/s)")
    ax.axhline(20.0, color="#2b4162", linestyle=":", linewidth=1.2, label="Pojemność 4 ścieżek (20 Mbit/s)")
    ax.set_title(
        f"{summary['scenario']} — Tryb: {summary['mode'].upper()} "
        f"(śr. w stanie ustalonym: {summary['steady_agg_mbps']:.2f} Mbit/s)",
        fontsize=11.5, fontweight="bold",
    )
    ax.set_xlabel("Czas [s]")
    ax.set_ylabel("Przepustowość [Mbit/s]")
    ax.set_xlim(1, max_t)
    ax.set_ylim(0, 23)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_nobel_comparison(scenario, ecmp_data, famtar_data, out_ts_path, out_bars_path):
    max_t = max(ecmp_data["duration"], famtar_data["duration"])
    times = list(range(1, max_t + 1))

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), dpi=200, sharey=True)
    for ax, data, title in zip(axes, [ecmp_data, famtar_data], ["Klasyczny ECMP (1 najkrótsza ścieżka)", "FAMTAR (4 ścieżki rozłączne)"]):
        ys = [[data["path_series"][k].get(str(t), data["path_series"][k].get(t, 0.0)) for t in times]
              for k, _, _ in NOBEL_PATHS]
        ytot = [sum(col) for col in zip(*ys)]
        ax.stackplot(
            times, *ys,
            labels=[label for _, label, _ in NOBEL_PATHS],
            colors=[color for _, _, color in NOBEL_PATHS],
            alpha=0.80,
        )
        ax.plot(times, ytot, color="black", linewidth=2.0, label="Suma (Total)")
        ax.axhline(5.0, color="#c44e52", linestyle="--", linewidth=1.2, label="Limit 1 ścieżki (5 Mbit/s)")
        ax.set_title(
            f"{title}\nStan ustalony: {data['steady_agg_mbps']:.2f} Mbit/s | Retransmisje: {data['total_retransmits']}",
            fontsize=10.5, fontweight="bold",
        )
        ax.set_xlabel("Czas [s]")
        ax.set_xlim(1, max_t)
        ax.set_ylim(0, 23)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper left", fontsize=7.8)

    axes[0].set_ylabel("Przepustowość [Mbit/s]")
    fig.suptitle(f"Topologia nobel-eu (Frankfurt -> Paris): {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_ts_path)
    plt.close(fig)

    # Bar chart: per-path steady throughput + total steady throughput + retransmissions
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.5), dpi=200)
    x = list(range(len(NOBEL_PATHS) + 1))
    labels = ["P1\n(bru)", "P2\n(str)", "P3\n(ams-lon)", "P4\n(mil-lyo)", "SUMA"]
    w = 0.35
    e_vals = [ecmp_data["steady_per_path"][k] for k, _, _ in NOBEL_PATHS] + [ecmp_data["steady_agg_mbps"]]
    f_vals = [famtar_data["steady_per_path"][k] for k, _, _ in NOBEL_PATHS] + [famtar_data["steady_agg_mbps"]]

    b1 = ax1.bar([i - w / 2 for i in x], e_vals, width=w, label="ECMP", color="#c44e52")
    b2 = ax1.bar([i + w / 2 for i in x], f_vals, width=w, label="FAMTAR", color="#55a868")
    ax1.axhline(5.0, color="gray", linestyle="--", linewidth=1.0, label="Pojemność 1 łącza (5 Mbit/s)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontweight="bold")
    ax1.set_ylabel("Przepustowość w stanie ustalonym [Mbit/s]")
    ax1.set_ylim(0, 23)
    ax1.set_title("Rozkład ruchu na ścieżkach (stan ustalony)", fontweight="bold")
    ax1.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax1.legend(fontsize=8.5)
    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8)

    retrans = [ecmp_data["total_retransmits"], famtar_data["total_retransmits"]]
    br = ax2.bar([0, 1], retrans, width=0.45, color=["#c44e52", "#55a868"])
    ax2.set_xticks([0, 1])
    ax2.set_xticklabels(["ECMP", "FAMTAR"], fontweight="bold")
    ax2.set_ylabel("Liczba retransmisji TCP")
    ax2.set_title("Straty / Retransmisje TCP (4 przepływy)", fontweight="bold")
    ax2.grid(True, axis="y", linestyle="--", alpha=0.4)
    for bar in br:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width() / 2, h),
                     xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9, fontweight="bold")

    fig.suptitle(f"Zestawienie wyników nobel-eu: {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_bars_path)
    plt.close(fig)


def handle_nobel(args):
    dur = args.duration
    stagger = args.stagger
    flows = {}
    for idx, port in enumerate(NOBEL_PORTS):
        flows[f"flow{idx + 1}"] = parse_iperf(
            os.path.join(args.dir, f"flow{idx + 1}.json"), offset_sec=idx * stagger
        )

    counts, path_series, flow_series = parse_nobel_wire(args.dir, max_sec=dur)
    steady_start = 3 * stagger + 2
    steady_range = [t for t in range(steady_start, dur)] or list(range(1, dur + 1))

    steady_per_path = {}
    for k, _, _ in NOBEL_PATHS:
        vals = [path_series[k].get(t, 0.0) for t in steady_range]
        steady_per_path[k] = round(sum(vals) / len(vals), 2)
    steady_agg = round(sum(steady_per_path.values()), 2)

    flow_paths = {}
    for idx, port in enumerate(NOBEL_PORTS):
        best_k = max(counts.keys(), key=lambda k: counts[k][port])
        flow_paths[f"flow{idx + 1}"] = best_k if counts[best_k][port] > 0 else "none"

    tot_ret = sum((flows[k]["retransmits"] if flows[k] else 0) for k in flows)
    tot_iperf_rcv = round(sum((flows[k]["rcv_mbps"] if flows[k] else 0.0) for k in flows), 2)

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "duration": dur,
        "stagger": stagger,
        "steady_window": [steady_range[0], steady_range[-1]],
        "flows": flows,
        "flow_paths": flow_paths,
        "packet_counts": counts,
        "path_series": {k: {str(t): v for t, v in path_series[k].items()} for k in path_series},
        "steady_per_path": steady_per_path,
        "steady_agg_mbps": steady_agg,
        "total_iperf_rcv_mbps": tot_iperf_rcv,
        "total_retransmits": tot_ret,
    }

    with open(os.path.join(args.dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    plot_nobel_single(summary, os.path.join(args.dir, "throughput_timeseries.png"))
    path_str = " ".join(f"{k}={steady_per_path[k]:.2f}M" for k, _, _ in NOBEL_PATHS)
    print(
        f"[{args.scenario}:{args.mode}] steady({steady_range[0]}..{steady_range[-1]}s) "
        f"{path_str} total={steady_agg:.2f}M iperf_sum={tot_iperf_rcv:.2f}M retrans={tot_ret}"
    )

    base_results = os.path.dirname(args.dir.rstrip("/"))
    ecmp_json = os.path.join(base_results, f"{args.scenario}_ecmp", "summary.json")
    famtar_json = os.path.join(base_results, f"{args.scenario}_famtar", "summary.json")
    if os.path.exists(ecmp_json) and os.path.exists(famtar_json):
        with open(ecmp_json) as f1, open(famtar_json) as f2:
            e_data, f_data = json.load(f1), json.load(f2)
        ts_cmp = os.path.join(base_results, f"{args.scenario}_comparison_timeseries.png")
        bar_cmp = os.path.join(base_results, f"{args.scenario}_comparison_bars.png")
        plot_nobel_comparison(args.scenario, e_data, f_data, ts_cmp, bar_cmp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["throughput", "qos", "nobel"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--stagger", type=int, default=2)
    parser.add_argument("--duration", type=int, default=24)
    parser.add_argument("--elephant-duration", type=int, default=10)
    args = parser.parse_args()

    if args.type == "throughput":
        handle_throughput(args)
    elif args.type == "nobel":
        handle_nobel(args)
    else:
        handle_qos(args)


if __name__ == "__main__":
    main()
