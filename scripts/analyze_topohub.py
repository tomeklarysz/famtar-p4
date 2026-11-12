#!/usr/bin/env python3
"""Analyze a TopoHub JSON graph (e.g. nobel-eu.json) to select a realistic
asymmetric subgraph with 4 edge-disjoint paths where ECMP underperforms FAMTAR.

Uses pure Python standard library for graph algorithms and optional matplotlib
for plotting the selected subgraph over geographic coordinates.
"""

import argparse
import heapq
import json
from collections import defaultdict, deque


def load_topohub_json(path):
    with open(path, "r") as f:
        data = json.load(f)
    nodes = {n["id"]: n for n in data["nodes"]}
    edges = {}
    adj = defaultdict(dict)
    for link in data["edges"]:
        u, v = link["source"], link["target"]
        adj[u][v] = link["dist"]
        adj[v][u] = link["dist"]
        edges[(min(u, v), max(u, v))] = link
    return data, nodes, adj, edges


def max_edge_disjoint_paths(adj, s, t):
    cap = defaultdict(lambda: defaultdict(int))
    for u in adj:
        for v in adj[u]:
            cap[u][v] = 1
    flow = 0
    while True:
        parent = {s: None}
        q = deque([s])
        while q and t not in parent:
            curr = q.popleft()
            for nxt, c in cap[curr].items():
                if c > 0 and nxt not in parent:
                    parent[nxt] = curr
                    q.append(nxt)
        if t not in parent:
            break
        flow += 1
        v = t
        while v != s:
            u = parent[v]
            cap[u][v] -= 1
            cap[v][u] += 1
            v = u
    return flow


def min_cost_k_disjoint_paths(adj, s, t, k=4):
    """Find k edge-disjoint paths minimizing total distance via successive shortest augmenting paths."""
    f = defaultdict(lambda: defaultdict(int))
    for _ in range(k):
        dist = {s: 0.0}
        parent = {}
        inq = {s}
        q = deque([s])
        while q:
            u = q.popleft()
            inq.discard(u)
            for v in adj[u]:
                if f[v][u] > 0:
                    rc = -adj[u][v]
                    if dist[u] + rc < dist.get(v, float("inf")) - 1e-9:
                        dist[v] = dist[u] + rc
                        parent[v] = u
                        if v not in inq:
                            q.append(v)
                            inq.add(v)
                elif f[u][v] == 0:
                    rc = adj[u][v]
                    if dist[u] + rc < dist.get(v, float("inf")) - 1e-9:
                        dist[v] = dist[u] + rc
                        parent[v] = u
                        if v not in inq:
                            q.append(v)
                            inq.add(v)
        if t not in dist:
            break
        v = t
        while v != s:
            u = parent[v]
            if f[v][u] > 0:
                f[v][u] -= 1
            else:
                f[u][v] += 1
            v = u

    used = defaultdict(list)
    for u in f:
        for v, val in f[u].items():
            if val == 1:
                used[u].append(v)

    paths = []
    for nxt in used[s]:
        p = [s, nxt]
        curr = nxt
        while curr != t:
            curr = used[curr].pop(0)
            p.append(curr)
        paths.append(p)
    return sorted(paths, key=lambda p: sum(adj[p[i]][p[i + 1]] for i in range(len(p) - 1)))


def count_ecmp_paths(adj, s, t, use_dist=True):
    dist = {s: 0.0}
    ways = {s: 1}
    first_hops = {s: set()}
    pq = [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        for v, w in adj[u].items():
            cost = round(w) if use_dist else 1
            nd = d + cost
            if nd < dist.get(v, float("inf")) - 1e-9:
                dist[v] = nd
                ways[v] = ways[u]
                first_hops[v] = {v} if u == s else set(first_hops[u])
                heapq.heappush(pq, (nd, v))
            elif abs(nd - dist.get(v, float("inf"))) <= 1e-9:
                ways[v] += ways[u]
                first_hops[v] |= ({v} if u == s else first_hops[u])
    return dist.get(t), ways.get(t, 0), len(first_hops.get(t, set()))


def plot_subgraph_map(nodes, edges, paths, s, t, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sub_nodes = {n for p in paths for n in p}
    fig, ax = plt.subplots(figsize=(10, 7.5), dpi=200)

    # Full nobel-eu background in light gray
    for (u, v), link in edges.items():
        if u in sub_nodes and v in sub_nodes:
            continue
        x1, y1 = nodes[u]["pos"]
        x2, y2 = nodes[v]["pos"]
        ax.plot([x1, x2], [y1, y2], color="#dddddd", linewidth=1.0, zorder=1)

    for nid, n in nodes.items():
        if nid not in sub_nodes:
            x, y = n["pos"]
            ax.scatter(x, y, color="#cccccc", s=35, zorder=2)
            ax.text(x, y + 0.25, n["name"], fontsize=7, color="#999999", ha="center", zorder=2)

    # Induced subgraph cross-edges not on the 4 main paths
    path_edges = set()
    for p in paths:
        for i in range(len(p) - 1):
            path_edges.add((min(p[i], p[i + 1]), max(p[i], p[i + 1])))

    for (u, v), link in edges.items():
        if u in sub_nodes and v in sub_nodes and (u, v) not in path_edges:
            x1, y1 = nodes[u]["pos"]
            x2, y2 = nodes[v]["pos"]
            ax.plot([x1, x2], [y1, y2], color="#aaaaaa", linestyle="--", linewidth=1.2, zorder=3)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, f"{round(link['dist'])} km",
                    fontsize=6.5, color="#777777", ha="center", zorder=3)

    # Highlight 4 disjoint paths
    colors = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e"]
    for idx, (p, col) in enumerate(zip(paths, colors), 1):
        d_km = sum(edges[(min(p[i], p[i + 1]), max(p[i], p[i + 1]))]["dist"] for i in range(len(p) - 1))
        label = f"Ścieżka P{idx} ({len(p)-1} hopy, {d_km:.0f} km): {' → '.join(nodes[n]['name'] for n in p)}"
        for i in range(len(p) - 1):
            u, v = p[i], p[i + 1]
            x1, y1 = nodes[u]["pos"]
            x2, y2 = nodes[v]["pos"]
            ax.plot([x1, x2], [y1, y2], color=col, linewidth=2.6, zorder=4,
                    label=label if i == 0 else None)
            km = edges[(min(u, v), max(u, v))]["dist"]
            ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.15, f"{round(km)} km",
                    fontsize=7, fontweight="bold", color=col,
                    bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.75), zorder=5)

    # Plot subgraph nodes
    for nid in sub_nodes:
        x, y = nodes[nid]["pos"]
        is_end = nid in (s, t)
        ax.scatter(x, y, color="#111111" if is_end else "#ffffff",
                   edgecolors="#111111", s=130 if is_end else 75, linewidths=1.8, zorder=6)
        role = " [H1]" if nid == s else (" [H2]" if nid == t else "")
        ax.text(x, y + 0.35, f"{nodes[nid]['name']}{role}",
                fontsize=8.5 if is_end else 8,
                fontweight="bold" if is_end else "semibold",
                color="#111111", ha="center", zorder=7)

    ax.set_title(
        f"TopoHub nobel-eu: wycinek {len(sub_nodes)} węzłów dla {nodes[s]['name']} ↔ {nodes[t]['name']} (4 ścieżki rozłączne)",
        fontsize=11.5, fontweight="bold"
    )
    ax.set_xlabel("Długość geograficzna [°]")
    ax.set_ylabel("Szerokość geograficzna [°]")
    ax.grid(True, linestyle=":", alpha=0.4)
    ax.legend(loc="lower left", fontsize=7.5, framealpha=0.92)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"[+] Zapisano mapę podgrafu: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="nobel-eu.json")
    parser.add_argument("--src", type=int, default=10, help="Source node ID (default: 10 = Frankfurt)")
    parser.add_argument("--dst", type=int, default=19, help="Target node ID (default: 19 = Paris)")
    parser.add_argument("--plot", default="results/nobel_eu_subgraph.png")
    args = parser.parse_args()

    data, nodes, adj, edges = load_topohub_json(args.input)
    print(f"Loaded {data['graph']['name']}: {len(nodes)} nodes, {len(edges)} edges")
    print(f"Stats from TopoHub: {data['graph'].get('stats', {})}\n")

    print("=== Pairs with >= 4 edge-disjoint paths ===")
    for u in sorted(nodes):
        for v in sorted(nodes):
            if u < v and len(adj[u]) >= 4 and len(adj[v]) >= 4:
                k = max_edge_disjoint_paths(adj, u, v)
                if k >= 4:
                    d_d, w_d, nh_d = count_ecmp_paths(adj, u, v, True)
                    d_h, w_h, nh_h = count_ecmp_paths(adj, u, v, False)
                    print(f"  {nodes[u]['name']}({u}) <-> {nodes[v]['name']}({v}): "
                          f"adp={k} | ECMP(dist): {nh_d} nhop ({d_d:.0f} km) | ECMP(hops): {nh_h} nhop ({d_h} hops)")

    paths = min_cost_k_disjoint_paths(adj, args.src, args.dst, k=4)
    sub_nodes = sorted({n for p in paths for n in p})
    print(f"\n=== Selected pair: {nodes[args.src]['name']}({args.src}) -> {nodes[args.dst]['name']}({args.dst}) ===")
    print(f"Induced subgraph nodes ({len(sub_nodes)}): {[nodes[n]['name'] for n in sub_nodes]}")
    for i, p in enumerate(paths, 1):
        d_km = sum(adj[p[j]][p[j + 1]] for j in range(len(p) - 1))
        print(f"  P{i} ({len(p)-1} hops, {d_km:6.1f} km): {' -> '.join(nodes[n]['name'] for n in p)}")

    if args.plot:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(args.plot)), exist_ok=True)
        plot_subgraph_map(nodes, edges, paths, args.src, args.dst, args.plot)


if __name__ == "__main__":
    main()
