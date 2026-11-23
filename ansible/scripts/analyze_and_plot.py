#!/usr/bin/env python3
import argparse
import glob
import json
import math
import os
import re
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def mean_std(values):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return 0.0, 0.0
    m = sum(vals) / len(vals)
    if len(vals) < 2:
        return round(m, 2), 0.0
    var = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    return round(m, 2), round(math.sqrt(var), 2)


def resolve_mode_root(path):
    """If path ends with run_<k>, return (parent_mode_dir, path); else (path, path)."""
    norm = path.rstrip("/")
    base = os.path.basename(norm)
    if re.match(r"^run_\d+$", base):
        return os.path.dirname(norm), norm
    return norm, norm


def collect_run_summaries(mode_root):
    run_files = sorted(glob.glob(os.path.join(mode_root, "run_*", "summary.json")))
    summaries = []
    for rf in run_files:
        try:
            with open(rf, "r") as f:
                d = json.load(f)
            if isinstance(d, dict) and "scenario" in d:
                summaries.append(d)
        except Exception:
            pass
    if not summaries:
        top_f = os.path.join(mode_root, "summary.json")
        if os.path.exists(top_f):
            try:
                with open(top_f, "r") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "scenario" in d:
                    summaries.append(d)
            except Exception:
                pass
    return summaries


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

    fct_sec = float(sum_rcv.get("seconds") or sum_sent.get("seconds") or 0.0)
    bytes_rcv = int(sum_rcv.get("bytes") or sum_sent.get("bytes") or 0)
    rcv_bps = float(sum_rcv.get("bits_per_second") or 0.0)
    if rcv_bps <= 0 and fct_sec > 0 and bytes_rcv > 0:
        rcv_bps = (bytes_rcv * 8.0) / fct_sec

    series = {}
    for iv in data.get("intervals", []):
        s = iv.get("sum", {})
        dur = float(s.get("seconds", 1.0))
        if dur < 0.15:
            continue
        t = int(math.ceil(float(s.get("end", 0)))) + offset_sec
        series[t] = round(s.get("bits_per_second", 0) / 1e6, 2)

    return {
        "sent_mbps": round(sum_sent.get("bits_per_second", 0) / 1e6, 2),
        "rcv_mbps": round(rcv_bps / 1e6, 2),
        "fct_sec": round(fct_sec, 2),
        "finish_sec": round(offset_sec + fct_sec, 2),
        "bytes_rcv": bytes_rcv,
        "retransmits": int(sum_sent.get("retransmits", 0)),
        "mean_rtt_ms": round(mean_rtt, 2),
        "series": series,
    }


def parse_wire_throughput(r2_path, r3_path, stagger_sec=2, max_sec=None):
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

    f1_data = [x[0] for x in pkts if x[1] == 5201 and x[2] > 1000]
    t0 = min(f1_data) if f1_data else min(x[0] for x in pkts)

    f1_times = [x[0] - t0 for x in pkts if x[1] == 5201 and x[2] > 0]
    f2_times = [x[0] - t0 for x in pkts if x[1] == 5202 and x[2] > 0]
    obs_max = max(1, int(math.ceil(max(x[0] - t0 for x in pkts))))
    total_sec = max(obs_max, int(max_sec)) if max_sec else obs_max

    s1, s2 = {}, {}
    for sec in range(1, total_sec + 1):
        b1 = sum(x[2] for x in pkts if (sec - 1) <= (x[0] - t0) < sec and x[1] == 5201)
        b2 = sum(x[2] for x in pkts if (sec - 1) <= (x[0] - t0) < sec and x[1] == 5202)
        s1[sec] = round(b1 * 8 / 1e6, 2)
        s2[sec] = round(b2 * 8 / 1e6, 2)

    f1_active = [v for v in s1.values() if v > 0.2]
    f2_active = [v for v in s2.values() if v > 0.2]
    avg1 = round(sum(f1_active) / len(f1_active), 2) if f1_active else 0.0
    avg2 = round(sum(f2_active) / len(f2_active), 2) if f2_active else 0.0

    wire_fct1 = round(max(f1_times) - min(f1_times), 2) if len(f1_times) > 1 else 0.0
    wire_fct2 = round(max(f2_times) - min(f2_times), 2) if len(f2_times) > 1 else 0.0

    return counts, (s1, avg1, wire_fct1), (s2, avg2, wire_fct2)


def extract_series(data, force_max_t=None):
    s1 = {int(k): v for k, v in ((data.get("flow1") or {}).get("series") or {}).items()}
    s2 = {int(k): v for k, v in ((data.get("flow2") or {}).get("series") or {}).items()}
    max_t = max(list(s1.keys()) + list(s2.keys()) + [force_max_t or 12])
    times = list(range(1, max_t + 1))
    y1 = [s1.get(t, 0.0) for t in times]
    y2 = [s2.get(t, 0.0) for t in times]
    ytot = [a + b for a, b in zip(y1, y2)]
    return times, y1, y2, ytot, max_t


def plot_single_throughput(summary, out_path):
    times, y1, y2, ytot, max_t = extract_series(summary)

    fig, ax = plt.subplots(figsize=(8.2, 4.5), dpi=200)
    ax.stackplot(
        times,
        y1,
        y2,
        labels=[
            f"Flow 1 (5201) -> {summary.get('flow1_path')} (FCT={summary.get('flow1_fct_sec', 0):.1f}s)",
            f"Flow 2 (5202) -> {summary.get('flow2_path')} (FCT={summary.get('flow2_fct_sec', 0):.1f}s)",
        ],
        colors=["#4c72b0", "#55a868"],
        alpha=0.75,
    )
    ax.plot(times, ytot, color="black", linewidth=2.0, label="Sumaryczny Throughput")
    ax.axhline(10.0, color="#c44e52", linestyle="--", linewidth=1.3, label="Pojemność 1 łącza (10 Mbit/s)")
    ax.axhline(9.0, color="#dd8452", linestyle=":", linewidth=1.2, label="Próg FAMTAR Th_max (9 Mbit/s)")

    ax.set_title(
        f"{summary['scenario']} — Tryb: {summary['mode'].upper()} "
        f"(śr. FCT: {summary.get('mean_fct_sec', 0):.2f} s)",
        fontsize=11.5, fontweight="bold",
    )
    ax.set_xlabel("Czas [s]")
    ax.set_ylabel("Przepustowość [Mbit/s]")
    ax.set_xlim(1, max_t)
    ax.set_ylim(0, 21)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_comparison_throughput(scenario, ecmp_data, famtar_data, out_ts_path, out_bars_path):
    _, _, _, _, max_e = extract_series(ecmp_data)
    _, _, _, _, max_f = extract_series(famtar_data)
    common_max_t = max(max_e, max_f)

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6), dpi=200, sharey=True)
    for ax, data, title in zip(
        axes,
        [ecmp_data, famtar_data],
        ["Klasyczny ECMP", "FAMTAR (P4 + IS-IS)"],
    ):
        times, y1, y2, ytot, _ = extract_series(data, force_max_t=common_max_t)
        st = data.get("stats", {})
        agg_m = st.get("aggregate_rcv_mbps_mean", data.get("aggregate_rcv_mbps", 0))
        fct_m = st.get("mean_fct_sec_mean", data.get("mean_fct_sec", 0))
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
            f"{title}\nSuma: {agg_m:.2f} Mbit/s | Śr. FCT: {fct_m:.2f} s",
            fontsize=10.5,
            fontweight="bold",
        )
        ax.set_xlabel("Czas [s]")
        ax.set_xlim(1, common_max_t)
        ax.set_ylim(0, 21)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper right", fontsize=8)

    axes[0].set_ylabel("Przepustowość [Mbit/s]")
    fig.suptitle(f"Przebieg transmisji w czasie: {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_ts_path)
    plt.close(fig)

    # Bar chart: Left = Throughput [Mbit/s], Right = Flow Completion Time (FCT) [s] (with +/- SD error bars)
    e_st = ecmp_data.get("stats", {})
    f_st = famtar_data.get("stats", {})
    n_runs = max(e_st.get("n_runs", 1), f_st.get("n_runs", 1))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), dpi=200)
    modes = ["ECMP", "FAMTAR"]
    x = [0, 1]
    w = 0.25

    f1_m = [e_st.get("f1_mbps_mean", ecmp_data["flow1"]["rcv_mbps"]),
            f_st.get("f1_mbps_mean", famtar_data["flow1"]["rcv_mbps"])]
    f1_s = [e_st.get("f1_mbps_std", 0.0), f_st.get("f1_mbps_std", 0.0)]

    f2_m = [e_st.get("f2_mbps_mean", ecmp_data["flow2"]["rcv_mbps"]),
            f_st.get("f2_mbps_mean", famtar_data["flow2"]["rcv_mbps"])]
    f2_s = [e_st.get("f2_mbps_std", 0.0), f_st.get("f2_mbps_std", 0.0)]

    tot_m = [e_st.get("aggregate_rcv_mbps_mean", ecmp_data["aggregate_rcv_mbps"]),
             f_st.get("aggregate_rcv_mbps_mean", famtar_data["aggregate_rcv_mbps"])]
    tot_s = [e_st.get("aggregate_rcv_mbps_std", 0.0), f_st.get("aggregate_rcv_mbps_std", 0.0)]

    b1 = ax1.bar([i - w for i in x], f1_m, yerr=f1_s if n_runs > 1 else None, capsize=4,
                 width=w, label="Flow 1 (5201)", color="#4c72b0")
    b2 = ax1.bar(x, f2_m, yerr=f2_s if n_runs > 1 else None, capsize=4,
                 width=w, label="Flow 2 (5202)", color="#55a868")
    b3 = ax1.bar([i + w for i in x], tot_m, yerr=tot_s if n_runs > 1 else None, capsize=4,
                 width=w, label="Suma (Aggregate)", color="#2b4162")
    ax1.axhline(10.0, color="#c44e52", linestyle="--", linewidth=1.1, label="Pojemność 1 łącza")
    ax1.set_xticks(x)
    ax1.set_xticklabels(modes, fontweight="bold")
    ax1.set_ylabel("Przepustowość [Mbit/s]")
    ax1.set_ylim(0, 22)
    ax1.set_title("Średnia przepustowość (Throughput)", fontweight="bold")
    ax1.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax1.legend(fontsize=8)
    for bars in (b1, b2, b3):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(f"{h:.2f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8)

    fct1_m = [e_st.get("f1_fct_sec_mean", ecmp_data.get("flow1_fct_sec", 0)),
              f_st.get("f1_fct_sec_mean", famtar_data.get("flow1_fct_sec", 0))]
    fct1_s = [e_st.get("f1_fct_sec_std", 0.0), f_st.get("f1_fct_sec_std", 0.0)]

    fct2_m = [e_st.get("f2_fct_sec_mean", ecmp_data.get("flow2_fct_sec", 0)),
              f_st.get("f2_fct_sec_mean", famtar_data.get("flow2_fct_sec", 0))]
    fct2_s = [e_st.get("f2_fct_sec_std", 0.0), f_st.get("f2_fct_sec_std", 0.0)]

    fct_avg_m = [e_st.get("mean_fct_sec_mean", ecmp_data.get("mean_fct_sec", 0)),
                 f_st.get("mean_fct_sec_mean", famtar_data.get("mean_fct_sec", 0))]
    fct_avg_s = [e_st.get("mean_fct_sec_std", 0.0), f_st.get("mean_fct_sec_std", 0.0)]

    fb1 = ax2.bar([i - w for i in x], fct1_m, yerr=fct1_s if n_runs > 1 else None, capsize=4,
                  width=w, label="Flow 1 (5201)", color="#4c72b0")
    fb2 = ax2.bar(x, fct2_m, yerr=fct2_s if n_runs > 1 else None, capsize=4,
                  width=w, label="Flow 2 (5202)", color="#55a868")
    fb3 = ax2.bar([i + w for i in x], fct_avg_m, yerr=fct_avg_s if n_runs > 1 else None, capsize=4,
                  width=w, label="Średni FCT", color="#c44e52")
    ax2.set_xticks(x)
    ax2.set_xticklabels(modes, fontweight="bold")
    ax2.set_ylabel("Czas ukończenia przepływu — FCT [s]")
    ax2.set_ylim(0, max(fct1_m + fct2_m + fct_avg_m + [15.0]) * 1.25)
    ax2.set_title("Czas ukończenia przepływu (Flow Completion Time)", fontweight="bold")
    ax2.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax2.legend(fontsize=8)
    for bars in (fb1, fb2, fb3):
        for bar in bars:
            h = bar.get_height()
            ax2.annotate(f"{h:.1f}s", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8)

    fig.suptitle(f"Zestawienie metryk TCP (n={n_runs}): {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_bars_path)
    plt.close(fig)


def handle_throughput(args):
    mode_root, run_dir = resolve_mode_root(args.dir)
    flow1 = parse_iperf(os.path.join(run_dir, "flow1.json"), offset_sec=0)
    flow2 = parse_iperf(os.path.join(run_dir, "flow2.json"), offset_sec=args.stagger)

    max_iperf_sec = int(math.ceil(max(
        (flow1["finish_sec"] if flow1 else 0),
        (flow2["finish_sec"] if flow2 else 0),
        12,
    )))

    counts, w1, w2 = parse_wire_throughput(
        os.path.join(run_dir, "tcpdump_r2.txt"),
        os.path.join(run_dir, "tcpdump_r3.txt"),
        stagger_sec=args.stagger,
        max_sec=max_iperf_sec,
    )
    if flow1 and w1:
        flow1["series"] = w1[0]
        if flow1["rcv_mbps"] <= 0:
            flow1["rcv_mbps"] = w1[1]
        if flow1["fct_sec"] <= 0:
            flow1["fct_sec"] = w1[2]
    if flow2 and w2:
        flow2["series"] = w2[0]
        if flow2["rcv_mbps"] <= 0:
            flow2["rcv_mbps"] = w2[1]
        if flow2["fct_sec"] <= 0:
            flow2["fct_sec"] = w2[2]

    p1 = "Ścieżka 1 (r2)" if counts["r2_f1"] >= counts["r3_f1"] else "Ścieżka 2 (r3)"
    p2 = "Ścieżka 1 (r2)" if counts["r2_f2"] >= counts["r3_f2"] else "Ścieżka 2 (r3)"

    f1_fct = flow1["fct_sec"] if flow1 else 0.0
    f2_fct = flow2["fct_sec"] if flow2 else 0.0
    mean_fct = round((f1_fct + f2_fct) / 2.0, 2) if (f1_fct and f2_fct) else round(f1_fct or f2_fct, 2)
    makespan = round(max(f1_fct, (args.stagger + f2_fct) if f2_fct else 0.0), 2)

    tot_rcv = round((flow1["rcv_mbps"] if flow1 else 0.0) + (flow2["rcv_mbps"] if flow2 else 0.0), 2)
    tot_ret = (flow1["retransmits"] if flow1 else 0) + (flow2["retransmits"] if flow2 else 0)

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "flow1": flow1,
        "flow2": flow2,
        "flow1_path": p1,
        "flow2_path": p2,
        "flow1_fct_sec": f1_fct,
        "flow2_fct_sec": f2_fct,
        "mean_fct_sec": mean_fct,
        "makespan_sec": makespan,
        "packets": counts,
        "aggregate_rcv_mbps": tot_rcv,
        "total_retransmits": tot_ret,
    }

    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    if flow1 and flow2:
        plot_single_throughput(summary, os.path.join(run_dir, "throughput_timeseries.png"))

    # Aggregate across all runs in mode_root
    runs = collect_run_summaries(mode_root)
    if runs:
        f1_m, f1_s = mean_std([r["flow1"]["rcv_mbps"] for r in runs if r.get("flow1")])
        f2_m, f2_s = mean_std([r["flow2"]["rcv_mbps"] for r in runs if r.get("flow2")])
        agg_m, agg_s = mean_std([r.get("aggregate_rcv_mbps") for r in runs])
        fct1_m, fct1_s = mean_std([r.get("flow1_fct_sec") for r in runs])
        fct2_m, fct2_s = mean_std([r.get("flow2_fct_sec") for r in runs])
        mfct_m, mfct_s = mean_std([r.get("mean_fct_sec") for r in runs])
        mk_m, mk_s = mean_std([r.get("makespan_sec") for r in runs])

        rep_idx = max(0, min(len(runs) - 1, getattr(args, "rep_run", 1) - 1))
        rep = dict(runs[rep_idx])
        rep["stats"] = {
            "n_runs": len(runs),
            "f1_mbps_mean": f1_m, "f1_mbps_std": f1_s,
            "f2_mbps_mean": f2_m, "f2_mbps_std": f2_s,
            "aggregate_rcv_mbps_mean": agg_m, "aggregate_rcv_mbps_std": agg_s,
            "f1_fct_sec_mean": fct1_m, "f1_fct_sec_std": fct1_s,
            "f2_fct_sec_mean": fct2_m, "f2_fct_sec_std": fct2_s,
            "mean_fct_sec_mean": mfct_m, "mean_fct_sec_std": mfct_s,
            "makespan_sec_mean": mk_m, "makespan_sec_std": mk_s,
        }
        with open(os.path.join(mode_root, "summary.json"), "w") as f:
            json.dump(rep, f, indent=2)
        if rep.get("flow1") and rep.get("flow2"):
            plot_single_throughput(rep, os.path.join(mode_root, "throughput_timeseries.png"))

        print(
            f"[{args.scenario}:{args.mode}] (n={len(runs)}) "
            f"f1={f1_m:.2f}±{f1_s:.2f}M (FCT={fct1_m:.2f}±{fct1_s:.2f}s) | "
            f"f2={f2_m:.2f}±{f2_s:.2f}M (FCT={fct2_m:.2f}±{fct2_s:.2f}s) | "
            f"total={agg_m:.2f}±{agg_s:.2f}M | mean_FCT={mfct_m:.2f}±{mfct_s:.2f}s"
        )

    base_results = os.path.dirname(mode_root.rstrip("/"))
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
    if not os.path.exists(filepath):
        return rtts
    for line in open(filepath):
        m = re.search(r"time=([\d\.]+)\s*ms", line)
        if m:
            rtts.append(round(float(m.group(1)), 2))
    return rtts


def parse_icmp_switch_time(r2_file, r3_file):
    def get_timestamps(path):
        ts = []
        if not os.path.exists(path):
            return ts
        for line in open(path):
            m = re.match(r"^(\d+):(\d+):(\d+\.\d+)\s+IP.*ICMP echo request", line)
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                ts.append(h * 3600 + mn * 60 + s)
        return ts

    t_r2 = get_timestamps(r2_file)
    t_r3 = get_timestamps(r3_file)
    return len(t_r2), len(t_r3), t_r2, t_r3


def handle_qos(args):
    rtts = parse_ping_series(os.path.join(args.dir, "ping_mice.txt"))
    c_r2, c_r3, t_r2, t_r3 = parse_icmp_switch_time(
        os.path.join(args.dir, "tcpdump_r2_icmp.txt"),
        os.path.join(args.dir, "tcpdump_r3_icmp.txt"),
    )

    times = [round(i * 0.1, 2) for i in range(len(rtts))]
    el_start = float(args.stagger)
    el_end = float(args.stagger + args.elephant_duration)

    during_el = [r for t, r in zip(times, rtts) if el_start <= t <= el_end]
    after_switch = [r for t, r in zip(times, rtts) if (el_start + 1.5) <= t <= el_end]

    reaction_ms = None
    if t_r2 and t_r3:
        t0 = min(t_r2[0], t_r3[0])
        first_r3_rel = t_r3[0] - t0
        if first_r3_rel >= el_start:
            reaction_ms = round((first_r3_rel - el_start) * 1000.0, 1)

    summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "icmp_packets_r2": c_r2,
        "icmp_packets_r3": c_r3,
        "reaction_time_ms": reaction_ms,
        "rtt_max_ms": round(max(rtts), 2) if rtts else 0.0,
        "rtt_mean_during_elephant_ms": round(sum(during_el) / len(during_el), 2) if during_el else 0.0,
        "rtt_mean_after_reroute_ms": round(sum(after_switch) / len(after_switch), 2) if after_switch else 0.0,
        "times": times,
        "rtts": rtts,
    }

    with open(os.path.join(args.dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)


NOBEL_PATHS = [
    ("p1", "P1: par-bru-fra (56)", "#4c72b0"),
    ("p2", "P2: par-str-fra (59)", "#55a868"),
    ("p3", "P3: par-lon-ams-ham-fra (145)", "#dd8452"),
    ("p4", "P4: par-lyo-zur-mil-mun-fra (162)", "#8172b3"),
]
NOBEL_PORTS = [5201, 5202, 5203, 5204]


def parse_nobel_wire(results_dir, max_sec=None):
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
        return counts, {k: {} for k, _, _ in NOBEL_PATHS}, {p: {} for p in NOBEL_PORTS}, 24

    f1_data = [x[0] for x in pkts if x[2] == 5201 and x[3] > 1000]
    t0 = min(f1_data) if f1_data else min(x[0] for x in pkts)
    obs_max = max(1, int(math.ceil(max(x[0] - t0 for x in pkts))))
    total_sec = max(obs_max, int(max_sec)) if max_sec else obs_max

    path_series = {k: {} for k, _, _ in NOBEL_PATHS}
    flow_series = {p: {} for p in NOBEL_PORTS}
    for sec in range(1, total_sec + 1):
        bucket = [x for x in pkts if (sec - 1) <= (x[0] - t0) < sec]
        for k, _, _ in NOBEL_PATHS:
            b = sum(x[3] for x in bucket if x[1] == k)
            path_series[k][sec] = round(b * 8 / 1e6, 2)
        for p in NOBEL_PORTS:
            b = sum(x[3] for x in bucket if x[2] == p)
            flow_series[p][sec] = round(b * 8 / 1e6, 2)

    return counts, path_series, flow_series, total_sec


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
        f"(stan ustalony: {summary['steady_agg_mbps']:.2f} Mbit/s | śr. FCT: {summary.get('mean_fct_sec', 0):.2f} s)",
        fontsize=11, fontweight="bold",
    )
    ax.set_xlabel("Czas [s]")
    ax.set_ylabel("Przepustowość [Mbit/s]")
    ax.set_xlim(1, max_t)
    ax.set_ylim(0, 23)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_nobel_comparison(scenario, ecmp_data, famtar_data, out_ts_path, out_bars_path):
    max_t = max(ecmp_data["duration"], famtar_data["duration"])
    times = list(range(1, max_t + 1))

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), dpi=200, sharey=True)
    for ax, data, title in zip(
        axes,
        [ecmp_data, famtar_data],
        ["Klasyczny ECMP (1 najkrótsza ścieżka)", "FAMTAR (4 ścieżki rozłączne)"],
    ):
        st = data.get("stats", {})
        agg_m = st.get("steady_agg_mbps_mean", data.get("steady_agg_mbps", 0))
        fct_m = st.get("mean_fct_sec_mean", data.get("mean_fct_sec", 0))
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
            f"{title}\nStan ustalony: {agg_m:.2f} Mbit/s | Śr. FCT: {fct_m:.2f} s",
            fontsize=10.5, fontweight="bold",
        )
        ax.set_xlabel("Czas [s]")
        ax.set_xlim(1, max_t)
        ax.set_ylim(0, 23)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper right", fontsize=7.8)

    axes[0].set_ylabel("Przepustowość [Mbit/s]")
    fig.suptitle(f"Topologia nobel-eu (Frankfurt -> Paris): {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_ts_path)
    plt.close(fig)

    # Bar chart: Left = Per-path + total throughput [Mbit/s], Right = FCT [s] (per-flow mean FCT & makespan)
    e_st = ecmp_data.get("stats", {})
    f_st = famtar_data.get("stats", {})
    n_runs = max(e_st.get("n_runs", 1), f_st.get("n_runs", 1))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.8, 4.6), dpi=200)
    x = list(range(len(NOBEL_PATHS) + 1))
    labels = ["P1\n(bru)", "P2\n(str)", "P3\n(ams-lon)", "P4\n(mil-lyo)", "SUMA"]
    w = 0.35

    e_vals = [e_st.get(f"{k}_mbps_mean", ecmp_data["steady_per_path"][k]) for k, _, _ in NOBEL_PATHS] + [
        e_st.get("steady_agg_mbps_mean", ecmp_data["steady_agg_mbps"])
    ]
    e_errs = [e_st.get(f"{k}_mbps_std", 0.0) for k, _, _ in NOBEL_PATHS] + [
        e_st.get("steady_agg_mbps_std", 0.0)
    ]
    f_vals = [f_st.get(f"{k}_mbps_mean", famtar_data["steady_per_path"][k]) for k, _, _ in NOBEL_PATHS] + [
        f_st.get("steady_agg_mbps_mean", famtar_data["steady_agg_mbps"])
    ]
    f_errs = [f_st.get(f"{k}_mbps_std", 0.0) for k, _, _ in NOBEL_PATHS] + [
        f_st.get("steady_agg_mbps_std", 0.0)
    ]

    b1 = ax1.bar([i - w / 2 for i in x], e_vals, yerr=e_errs if n_runs > 1 else None, capsize=3,
                 width=w, label="ECMP", color="#c44e52")
    b2 = ax1.bar([i + w / 2 for i in x], f_vals, yerr=f_errs if n_runs > 1 else None, capsize=3,
                 width=w, label="FAMTAR", color="#55a868")
    ax1.axhline(5.0, color="gray", linestyle="--", linewidth=1.0, label="Pojemność 1 łącza (5 Mbit/s)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontweight="bold")
    ax1.set_ylabel("Przepustowość [Mbit/s]")
    ax1.set_ylim(0, 23)
    ax1.set_title("Rozkład przepustowości na ścieżkach", fontweight="bold")
    ax1.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax1.legend(fontsize=8.5)
    for bars in (b1, b2):
        for bar in bars:
            h = bar.get_height()
            ax1.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8)

    # Right subplot: FCT comparison (Średni FCT pojedynczego przepływu & Całkowity czas transmisji 4 przepływów)
    fx = [0, 1]
    fw = 0.32
    e_fcts = [
        e_st.get("mean_fct_sec_mean", ecmp_data.get("mean_fct_sec", 0)),
        e_st.get("makespan_sec_mean", ecmp_data.get("makespan_sec", 0)),
    ]
    e_fct_errs = [
        e_st.get("mean_fct_sec_std", 0.0),
        e_st.get("makespan_sec_std", 0.0),
    ]
    f_fcts = [
        f_st.get("mean_fct_sec_mean", famtar_data.get("mean_fct_sec", 0)),
        f_st.get("makespan_sec_mean", famtar_data.get("makespan_sec", 0)),
    ]
    f_fct_errs = [
        f_st.get("mean_fct_sec_std", 0.0),
        f_st.get("makespan_sec_std", 0.0),
    ]

    fb1 = ax2.bar([i - fw / 2 for i in fx], e_fcts, yerr=e_fct_errs if n_runs > 1 else None, capsize=4,
                  width=fw, label="ECMP", color="#c44e52")
    fb2 = ax2.bar([i + fw / 2 for i in fx], f_fcts, yerr=f_fct_errs if n_runs > 1 else None, capsize=4,
                  width=fw, label="FAMTAR", color="#55a868")
    ax2.set_xticks(fx)
    ax2.set_xticklabels(["Średni FCT\n1 przepływu", "Całkowity czas\n(4 przepływy)"], fontweight="bold")
    ax2.set_ylabel("Czas [s]")
    ax2.set_ylim(0, max(e_fcts + f_fcts + [20.0]) * 1.25)
    ax2.set_title("Czas ukończenia przepływu (FCT)", fontweight="bold")
    ax2.grid(True, axis="y", linestyle="--", alpha=0.4)
    ax2.legend(fontsize=8.5)
    for bars in (fb1, fb2):
        for bar in bars:
            h = bar.get_height()
            ax2.annotate(f"{h:.1f}s", xy=(bar.get_x() + bar.get_width() / 2, h),
                         xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8.5, fontweight="bold")

    fig.suptitle(f"Zestawienie wyników TCP nobel-eu (n={n_runs}): {scenario}", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_bars_path)
    plt.close(fig)


def handle_nobel(args):
    mode_root, run_dir = resolve_mode_root(args.dir)
    stagger = args.stagger
    flows = {}
    for idx, port in enumerate(NOBEL_PORTS):
        flows[f"flow{idx + 1}"] = parse_iperf(
            os.path.join(run_dir, f"flow{idx + 1}.json"), offset_sec=idx * stagger
        )

    fcts = [flows[k]["fct_sec"] for k in flows if flows[k] and flows[k]["fct_sec"] > 0]
    finishes = [flows[k]["finish_sec"] for k in flows if flows[k] and flows[k]["finish_sec"] > 0]
    mean_fct = round(sum(fcts) / len(fcts), 2) if fcts else 0.0
    makespan = round(max(finishes), 2) if finishes else float(args.duration)

    counts, path_series, flow_series, total_sec = parse_nobel_wire(
        run_dir, max_sec=max(int(math.ceil(makespan)), args.duration or 16)
    )

    # Concurrent overlap window when all 4 flows are active: [3*stagger + 1 .. min(finishes)]
    steady_start = 3 * stagger + 1
    min_finish = int(math.floor(min(finishes))) if finishes else total_sec
    steady_end = max(steady_start + 1, min(min_finish, total_sec))
    steady_range = list(range(steady_start, steady_end + 1))

    steady_per_path = {}
    for k, _, _ in NOBEL_PATHS:
        vals = [path_series[k].get(t, 0.0) for t in steady_range]
        steady_per_path[k] = round(sum(vals) / len(vals), 2) if vals else 0.0
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
        "duration": total_sec,
        "stagger": stagger,
        "steady_window": [steady_range[0], steady_range[-1]],
        "flows": flows,
        "flow_paths": flow_paths,
        "mean_fct_sec": mean_fct,
        "makespan_sec": makespan,
        "packet_counts": counts,
        "path_series": {k: {str(t): v for t, v in path_series[k].items()} for k in path_series},
        "steady_per_path": steady_per_path,
        "steady_agg_mbps": steady_agg,
        "total_iperf_rcv_mbps": tot_iperf_rcv,
        "total_retransmits": tot_ret,
    }

    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    plot_nobel_single(summary, os.path.join(run_dir, "throughput_timeseries.png"))

    runs = collect_run_summaries(mode_root)
    if runs:
        stats = {"n_runs": len(runs)}
        for k, _, _ in NOBEL_PATHS:
            m, s = mean_std([r["steady_per_path"][k] for r in runs if "steady_per_path" in r])
            stats[f"{k}_mbps_mean"] = m
            stats[f"{k}_mbps_std"] = s
        agg_m, agg_s = mean_std([r.get("steady_agg_mbps") for r in runs])
        fct_m, fct_s = mean_std([r.get("mean_fct_sec") for r in runs])
        mk_m, mk_s = mean_std([r.get("makespan_sec") for r in runs])
        stats["steady_agg_mbps_mean"] = agg_m
        stats["steady_agg_mbps_std"] = agg_s
        stats["mean_fct_sec_mean"] = fct_m
        stats["mean_fct_sec_std"] = fct_s
        stats["makespan_sec_mean"] = mk_m
        stats["makespan_sec_std"] = mk_s

        rep_idx = max(0, min(len(runs) - 1, getattr(args, "rep_run", 1) - 1))
        rep = dict(runs[rep_idx])
        rep["stats"] = stats
        with open(os.path.join(mode_root, "summary.json"), "w") as f:
            json.dump(rep, f, indent=2)
        plot_nobel_single(rep, os.path.join(mode_root, "throughput_timeseries.png"))

        path_str = " ".join(f"{k}={stats[f'{k}_mbps_mean']:.2f}±{stats[f'{k}_mbps_std']:.2f}M" for k, _, _ in NOBEL_PATHS)
        print(
            f"[{args.scenario}:{args.mode}] (n={len(runs)}) "
            f"{path_str} | total={agg_m:.2f}±{agg_s:.2f}M | "
            f"mean_FCT={fct_m:.2f}±{fct_s:.2f}s | makespan={mk_m:.2f}±{mk_s:.2f}s"
        )

    base_results = os.path.dirname(mode_root.rstrip("/"))
    ecmp_json = os.path.join(base_results, f"{args.scenario}_ecmp", "summary.json")
    famtar_json = os.path.join(base_results, f"{args.scenario}_famtar", "summary.json")
    if os.path.exists(ecmp_json) and os.path.exists(famtar_json):
        with open(ecmp_json) as f1, open(famtar_json) as f2:
            e_data, f_data = json.load(f1), json.load(f2)
        ts_cmp = os.path.join(base_results, f"{args.scenario}_comparison_timeseries.png")
        bar_cmp = os.path.join(base_results, f"{args.scenario}_comparison_bars.png")
        plot_nobel_comparison(args.scenario, e_data, f_data, ts_cmp, bar_cmp)


def parse_tcpdump_tt(filepath):
    pkts = []
    if not os.path.exists(filepath):
        return pkts
    pat = re.compile(r"^(\d+\.\d+)\s+IP\s+.*length\s+(\d+)")
    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            m = pat.search(line)
            if m:
                pkts.append((float(m.group(1)), int(m.group(2))))
    return pkts


def handle_pareto(args):
    mode_root, run_dir = resolve_mode_root(args.dir)
    rx_json_path = os.path.join(run_dir, "receiver_stats.json")
    tx_json_path = os.path.join(run_dir, "sender_stats.json")
    if not os.path.exists(rx_json_path):
        print(f"Brak pliku {rx_json_path}!", file=sys.stderr)
        return

    with open(rx_json_path) as f:
        rx_data = json.load(f)
    tx_data = {}
    if os.path.exists(tx_json_path):
        with open(tx_json_path) as f:
            tx_data = json.load(f)

    timeseries = rx_data.get("timeseries", [])
    summary = rx_data.get("summary", {})
    win_bounds = summary.get("win_bounds", [10, 45])
    w_start, w_end = int(win_bounds[0]), int(win_bounds[1])
    w_info = dict(summary.get("window_20_230", {}))

    # Ground-truth packet loss using sender_stats.json if available
    tx_summary = tx_data.get("summary", {})
    tx_ts = {int(x["sec"]): x for x in tx_data.get("timeseries", [])}
    total_sent = int(tx_summary.get("total_pkts_sent", 0))
    total_rcvd = int(summary.get("total_packets", 0))
    if total_sent > 0:
        true_lost = max(0, total_sent - total_rcvd)
        true_loss_pct = round((true_lost / total_sent) * 100.0, 2)
        summary["total_lost"] = true_lost
        summary["loss_pct"] = true_loss_pct
        if tx_ts:
            win_sent_pkts = sum(tx_ts[s]["pkts"] for s in tx_ts if w_start <= s <= w_end)
            win_rcvd_pkts = int(w_info.get("total_pkts", 0))
            if win_sent_pkts > 0:
                win_lost = max(0, win_sent_pkts - win_rcvd_pkts)
                w_info["total_lost"] = win_lost
                w_info["loss_pct"] = round((win_lost / win_sent_pkts) * 100.0, 2)

    # Detect 4-path (nobel-eu: tcpdump_p1..p4) vs 2-path (basic lab: tcpdump_r2, tcpdump_r3)
    is_nobel = any(os.path.exists(os.path.join(run_dir, f"tcpdump_p{i}.txt")) for i in range(1, 5))
    if is_nobel:
        path_specs = NOBEL_PATHS
    else:
        path_specs = [
            ("r2", "Ścieżka 1 (r2)", "#dd8452"),
            ("r3", "Ścieżka 2 (r3)", "#55a868"),
        ]

    raw_pkts = {k: parse_tcpdump_tt(os.path.join(run_dir, f"tcpdump_{k}.txt")) for k, _, _ in path_specs}
    all_ts = [p[0] for plist in raw_pkts.values() for p in plist]
    t0 = min(all_ts) if all_ts else 0.0

    secs = [x["sec"] for x in timeseries]
    max_sec = max(secs) if secs else 1

    path_mbps = {}
    for k, _, _ in path_specs:
        sec_bytes = {}
        for t, length in raw_pkts[k]:
            s = int(t - t0)
            sec_bytes[s] = sec_bytes.get(s, 0) + length
        path_mbps[k] = [round((sec_bytes.get(s, 0) * 8.0) / 1e6, 3) for s in secs]

    tot_mbps_list = [x["mbps"] for x in timeseries]
    link_cap = 5.0 if is_nobel else 10.0
    tot_cap = 20.0

    out_summary = {
        "scenario": args.scenario,
        "mode": args.mode,
        "is_nobel": is_nobel,
        "win_bounds": [w_start, w_end],
        "total_flows": summary.get("total_flows", 0),
        "duration_sec": summary.get("duration_sec", 0.0),
        "overall_mbps": summary.get("overall_mbps", 0.0),
        "loss_pct": summary.get("loss_pct", 0.0),
        "total_packets": total_rcvd,
        "total_sent_packets": total_sent,
        "total_lost": summary.get("total_lost", 0),
        "window_20_230": w_info,
        "timeseries": timeseries,
        "path_mbps": path_mbps,
        "r2_mbps": path_mbps.get("r2", []),
        "r3_mbps": path_mbps.get("r3", []),
    }

    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(out_summary, f, indent=2)

    single_plot = os.path.join(run_dir, f"pareto_throughput_{args.mode}.png")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6.5), dpi=200, sharex=True)

    ax1.plot(secs, tot_mbps_list, color="#4c72b0", linewidth=1.8, label="Odebrany Throughput (h2)")
    ax1.axhline(link_cap, color="#c44e52", linestyle="--", linewidth=1.2, label=f"Pojemność 1 łącza ({link_cap:.0f} Mbit/s)")
    ax1.axhline(tot_cap, color="#2ca02c", linestyle=":", linewidth=1.2, label=f"Pojemność sieci ({tot_cap:.0f} Mbit/s)")
    if max_sec >= w_start + 5:
        ax1.axvspan(w_start, min(w_end, max_sec), color="gray", alpha=0.15, label=f"Okno pomiarowe [{w_start}, {w_end}]s")
    ax1.set_title(f"Benchmark UDP Pareto — Tryb: {args.mode.upper()}", fontsize=11.5, fontweight="bold")
    ax1.set_ylabel("Przepustowość [Mbit/s]")
    ax1.grid(True, linestyle="--", alpha=0.4)
    ax1.legend(loc="upper right", fontsize=8.5)

    for k, label, color in path_specs:
        ax2.plot(secs, path_mbps[k], color=color, linewidth=1.5, label=label)
    ax2.axhline(link_cap, color="#c44e52", linestyle="--", linewidth=1.0, alpha=0.7)
    ax2.set_xlabel("Czas [s]")
    ax2.set_ylabel("Ruch na ścieżce [Mbit/s]")
    ax2.grid(True, linestyle="--", alpha=0.4)
    ax2.legend(loc="upper right", fontsize=8)

    fig.tight_layout()
    fig.savefig(single_plot)
    plt.close(fig)

    # Aggregate across all runs in mode_root
    runs = collect_run_summaries(mode_root)
    if runs:
        win_mbps_m, win_mbps_s = mean_std([r.get("window_20_230", {}).get("avg_mbps", r.get("overall_mbps")) for r in runs])
        win_loss_m, win_loss_s = mean_std([r.get("window_20_230", {}).get("loss_pct", r.get("loss_pct")) for r in runs])
        ov_mbps_m, ov_mbps_s = mean_std([r.get("overall_mbps") for r in runs])
        ov_loss_m, ov_loss_s = mean_std([r.get("loss_pct") for r in runs])

        rep_idx = max(0, min(len(runs) - 1, getattr(args, "rep_run", 1) - 1))
        rep = dict(runs[rep_idx])
        rep["stats"] = {
            "n_runs": len(runs),
            "win_mbps_mean": win_mbps_m, "win_mbps_std": win_mbps_s,
            "win_loss_pct_mean": win_loss_m, "win_loss_pct_std": win_loss_s,
            "overall_mbps_mean": ov_mbps_m, "overall_mbps_std": ov_mbps_s,
            "overall_loss_pct_mean": ov_loss_m, "overall_loss_pct_std": ov_loss_s,
        }
        with open(os.path.join(mode_root, "summary.json"), "w") as f:
            json.dump(rep, f, indent=2)

        print(
            f"[{args.scenario}:{args.mode}] (n={len(runs)}) "
            f"Window[{w_start},{w_end}]s: Throughput={win_mbps_m:.2f}±{win_mbps_s:.2f} Mbit/s | "
            f"PacketLoss={win_loss_m:.2f}±{win_loss_s:.2f}% | "
            f"Overall={ov_mbps_m:.2f}±{ov_mbps_s:.2f} Mbit/s"
        )

    base_results = os.path.dirname(mode_root.rstrip("/"))
    ecmp_json = os.path.join(base_results, f"{args.scenario}_ecmp", "summary.json")
    famtar_json = os.path.join(base_results, f"{args.scenario}_famtar", "summary.json")

    if os.path.exists(ecmp_json) and os.path.exists(famtar_json):
        with open(ecmp_json) as f1, open(famtar_json) as f2:
            e_data, f_data = json.load(f1), json.load(f2)

        e_st = e_data.get("stats", {})
        f_st = f_data.get("stats", {})
        n_runs = max(e_st.get("n_runs", 1), f_st.get("n_runs", 1))
        e_win = e_st.get("win_mbps_mean", e_data.get("window_20_230", {}).get("avg_mbps", e_data.get("overall_mbps", 0)))
        f_win = f_st.get("win_mbps_mean", f_data.get("window_20_230", {}).get("avg_mbps", f_data.get("overall_mbps", 0)))
        e_win_s = e_st.get("win_mbps_std", 0.0)
        f_win_s = f_st.get("win_mbps_std", 0.0)

        e_loss = e_st.get("win_loss_pct_mean", e_data.get("window_20_230", {}).get("loss_pct", e_data.get("loss_pct", 0.0)))
        f_loss = f_st.get("win_loss_pct_mean", f_data.get("window_20_230", {}).get("loss_pct", f_data.get("loss_pct", 0.0)))
        e_loss_s = e_st.get("win_loss_pct_std", 0.0)
        f_loss_s = f_st.get("win_loss_pct_std", 0.0)

        # 1. Comparison of Aggregate Throughput Timeseries
        cmp_ts_plot = os.path.join(base_results, f"{args.scenario}_comparison_throughput.png")
        fig, ax = plt.subplots(figsize=(9.2, 4.5), dpi=200)

        e_ts = e_data.get("timeseries", [])
        f_ts = f_data.get("timeseries", [])
        ex = [p["sec"] for p in e_ts]
        ey = [p["mbps"] for p in e_ts]
        fx = [p["sec"] for p in f_ts]
        fy = [p["mbps"] for p in f_ts]

        ax.plot(ex, ey, color="#c44e52", linewidth=1.6, alpha=0.85,
                label=f"Klasyczny ECMP (śr. {e_win:.2f}±{e_win_s:.2f} Mbit/s)")
        ax.plot(fx, fy, color="#4c72b0", linewidth=1.8,
                label=f"FAMTAR (śr. {f_win:.2f}±{f_win_s:.2f} Mbit/s)")
        ax.axhline(link_cap, color="gray", linestyle="--", linewidth=1.1, label=f"Pojemność 1 łącza ({link_cap:.0f} Mbit/s)")
        ax.axhline(tot_cap, color="#2ca02c", linestyle=":", linewidth=1.1, label=f"Pojemność sieci ({tot_cap:.0f} Mbit/s)")
        max_cmp_sec = max(ex + fx) if (ex + fx) else 1
        if max_cmp_sec >= w_start + 5:
            ax.axvspan(w_start, min(w_end, max_cmp_sec), color="gray", alpha=0.12, label=f"Okno ewaluacji [{w_start}, {w_end}]s")

        ax.set_title(f"Strumień przepływów UDP Pareto: ECMP vs FAMTAR ({args.scenario})", fontsize=11.5, fontweight="bold")
        ax.set_xlabel("Czas [s]")
        ax.set_ylabel("Przepustowość [Mbit/s]")
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.legend(loc="upper right", fontsize=8.5)
        fig.tight_layout()
        fig.savefig(cmp_ts_plot)
        plt.close(fig)

        # 2. Comparison of Path Breakdown
        cmp_paths_plot = os.path.join(base_results, f"{args.scenario}_comparison_paths.png")
        fig, (ax_e, ax_f) = plt.subplots(1, 2, figsize=(12.5, 4.6), dpi=200, sharey=True)

        e_pm = e_data.get("path_mbps", {"r2": e_data.get("r2_mbps", []), "r3": e_data.get("r3_mbps", [])})
        f_pm = f_data.get("path_mbps", {"r2": f_data.get("r2_mbps", []), "r3": f_data.get("r3_mbps", [])})

        for k, label, color in path_specs:
            if k in e_pm and len(e_pm[k]) == len(ex):
                ax_e.plot(ex, e_pm[k], color=color, linewidth=1.4, label=label)
            if k in f_pm and len(f_pm[k]) == len(fx):
                ax_f.plot(fx, f_pm[k], color=color, linewidth=1.4, label=label)

        for ax_p, ttl in ((ax_e, "Klasyczny ECMP (Brak reakcji na zator)"), (ax_f, "FAMTAR (Dynamiczne odciążanie zatorów)")):
            ax_p.axhline(link_cap, color="#c44e52", linestyle="--", linewidth=1.2, label=f"Limit {link_cap:.0f} Mbit/s")
            ax_p.set_title(ttl, fontweight="bold")
            ax_p.set_xlabel("Czas [s]")
            ax_p.grid(True, linestyle="--", alpha=0.4)
            ax_p.legend(loc="upper right", fontsize=8)
        ax_e.set_ylabel("Obciążenie ścieżki [Mbit/s]")

        fig.tight_layout()
        fig.savefig(cmp_paths_plot)
        plt.close(fig)

        # 3. Bar chart: Throughput & Packet Loss % (with +/- SD error bars across seeds)
        cmp_bars_plot = os.path.join(base_results, f"{args.scenario}_comparison_summary.png")
        fig, (b_ax1, b_ax2) = plt.subplots(1, 2, figsize=(9.8, 4.4), dpi=200)

        modes = ["Klasyczny ECMP", "FAMTAR"]
        throughputs = [e_win, f_win]
        tp_errs = [e_win_s, f_win_s]
        losses = [e_loss, f_loss]
        loss_errs = [e_loss_s, f_loss_s]

        colors = ["#c44e52", "#4c72b0"]
        b1 = b_ax1.bar(modes, throughputs, yerr=tp_errs if n_runs > 1 else None, capsize=5,
                       color=colors, width=0.5, alpha=0.88)
        b_ax1.set_ylabel("Przepustowość [Mbit/s]")
        b_ax1.set_title(f"Średnia przepustowość (okno [{w_start}, {w_end}]s)", fontsize=10.5, fontweight="bold")
        b_ax1.grid(True, linestyle="--", alpha=0.4, axis="y")
        b_ax1.set_ylim(0, max(throughputs + [link_cap * 1.2]) * 1.28)
        for rect, err in zip(b1, tp_errs):
            h = rect.get_height()
            lbl = f"{h:.2f}±{err:.2f} M" if n_runs > 1 else f"{h:.2f} M"
            b_ax1.annotate(lbl, xy=(rect.get_x() + rect.get_width() / 2, h + (err if n_runs > 1 else 0)),
                           xytext=(0, 4), textcoords="offset points", ha="center", va="bottom",
                           fontsize=8.5, fontweight="bold")

        b2 = b_ax2.bar(modes, losses, yerr=loss_errs if n_runs > 1 else None, capsize=5,
                       color=colors, width=0.5, alpha=0.88)
        b_ax2.set_ylabel("Straty pakietów — Packet Loss [%]")
        b_ax2.set_title("Wskaźnik strat pakietów (Packet Loss %)", fontsize=10.5, fontweight="bold")
        b_ax2.grid(True, linestyle="--", alpha=0.4, axis="y")
        b_ax2.set_ylim(0, max(losses + [5.0]) * 1.32)
        for rect, err in zip(b2, loss_errs):
            h = rect.get_height()
            lbl = f"{h:.2f}±{err:.2f}%" if n_runs > 1 else f"{h:.2f}%"
            b_ax2.annotate(lbl, xy=(rect.get_x() + rect.get_width() / 2, h + (err if n_runs > 1 else 0)),
                           xytext=(0, 4), textcoords="offset points", ha="center", va="bottom",
                           fontsize=8.5, fontweight="bold")

        fig.suptitle(f"Zestawienie statystyczne UDP Pareto (n={n_runs}): {args.scenario}", fontsize=12.5, fontweight="bold")
        fig.tight_layout()
        fig.savefig(cmp_bars_plot)
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["throughput", "qos", "pareto", "nobel"], required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--dir", required=True)
    parser.add_argument("--stagger", type=int, default=2)
    parser.add_argument("--duration", type=int, default=16)
    parser.add_argument("--elephant-duration", type=int, default=10)
    parser.add_argument("--rep-run", type=int, default=1, help="1-based run index used as representative timeseries")
    args = parser.parse_args()

    if args.type == "throughput":
        handle_throughput(args)
    elif args.type == "pareto":
        handle_pareto(args)
    elif args.type == "nobel":
        handle_nobel(args)
    else:
        handle_qos(args)


if __name__ == "__main__":
    main()
