#!/usr/bin/env python3
"""Generate Containerlab topology, FRR IS-IS configs, and controller.yaml files
for the realistic asymmetric 11-node nobel-eu subgraph (Frankfurt <-> Paris, 4 ADPs).

Outputs:
  - topology.nobel.clab.yaml
  - configs/nobel/<slug>/frr.conf
  - configs/nobel/<slug>/controller.yaml
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from analyze_topohub import load_topohub_json, min_cost_k_disjoint_paths  # noqa: E402

TOPO_NAME = "p4-nobel-lab"
MGMT_NET = "clab-nobel-mgmt"
MGMT_SUBNET = "172.20.21.0/24"
DEFAULT_CAPACITY_BPS = 5_000_000
DEFAULT_TC_RATE = "5mbit"
DEFAULT_TC_BURST = "16kb"
DEFAULT_TC_LATENCY = "50ms"

SRC_NAME = "Frankfurt"
DST_NAME = "Paris"

# Short 3-letter slugs for clean Linux/Docker/Containerlab naming
NODE_SLUGS = {
    "Amsterdam": "ams",
    "Brussels": "bru",
    "Frankfurt": "fra",
    "Hamburg": "ham",
    "London": "lon",
    "Lyon": "lyo",
    "Milan": "mil",
    "Munich": "mun",
    "Paris": "par",
    "Strasbourg": "str",
    "Zurich": "zur",
}


def slug_for(name):
    if name in NODE_SLUGS:
        return NODE_SLUGS[name]
    return name.lower()[:3]


def isis_metric_from_dist(dist_km):
    return max(1, round(dist_km / 10.0))


def build_topology(json_path, out_topo_path, configs_dir, capacity_bps, tc_rate):
    _, nodes, adj, edges = load_topohub_json(json_path)
    name_to_id = {n["name"]: nid for nid, n in nodes.items()}
    s_id = name_to_id[SRC_NAME]
    t_id = name_to_id[DST_NAME]

    paths = min_cost_k_disjoint_paths(adj, s_id, t_id, k=4)
    if len(paths) < 4:
        raise RuntimeError(f"Expected 4 edge-disjoint paths {SRC_NAME}->{DST_NAME}, found {len(paths)}")

    sub_ids = sorted({nid for p in paths for nid in p})
    sub_id_set = set(sub_ids)
    sub_edges = sorted((u, v) for (u, v) in edges if u in sub_id_set and v in sub_id_set)

    # Assign router indices 1..N deterministically
    r_index = {nid: idx + 1 for idx, nid in enumerate(sub_ids)}
    r_slug = {nid: slug_for(nodes[nid]["name"]) for nid in sub_ids}

    # Port numbering per router: eth1..ethK
    # Edge routers fra (h1) and par (h2) reserve eth1 (port 1) for their host
    next_port = {nid: 1 for nid in sub_ids}
    ports = {nid: [] for nid in sub_ids}  # list of dicts describing each local interface

    h1_mac = "00:00:00:01:01:01"
    h2_mac = "00:00:00:02:02:02"

    def alloc_mac(nid, port_num):
        return f"00:00:00:00:{r_index[nid]:02x}:{port_num:02x}"

    # Host links first (eth1 on fra and par)
    fra_port = next_port[s_id]
    next_port[s_id] += 1
    fra_h1_mac = alloc_mac(s_id, fra_port)
    ports[s_id].append({
        "ifname": f"eth{fra_port}",
        "port": fra_port,
        "peer_desc": "h1",
        "ip": "10.0.1.254/24",
        "local_mac": fra_h1_mac,
        "peer_mac": h1_mac,
        "passive": True,
        "metric": None,
        "dist_km": None,
    })

    par_port = next_port[t_id]
    next_port[t_id] += 1
    par_h2_mac = alloc_mac(t_id, par_port)
    ports[t_id].append({
        "ifname": f"eth{par_port}",
        "port": par_port,
        "peer_desc": "h2",
        "ip": "10.0.2.254/24",
        "local_mac": par_h2_mac,
        "peer_mac": h2_mac,
        "passive": True,
        "metric": None,
        "dist_km": None,
    })

    # Transit links
    clab_links = [
        {
            "a_node": "h1", "a_if": "eth1", "a_mac": h1_mac,
            "b_node": r_slug[s_id], "b_if": f"eth{fra_port}", "b_mac": fra_h1_mac,
        },
        {
            "a_node": r_slug[t_id], "a_if": f"eth{par_port}", "a_mac": par_h2_mac,
            "b_node": "h2", "b_if": "eth1", "b_mac": h2_mac,
        },
    ]

    for edge_num, (u, v) in enumerate(sub_edges, start=1):
        dist_km = edges[(u, v)]["dist"]
        metric = isis_metric_from_dist(dist_km)
        pu = next_port[u]
        pv = next_port[v]
        next_port[u] += 1
        next_port[v] += 1

        mac_u = alloc_mac(u, pu)
        mac_v = alloc_mac(v, pv)
        ip_u = f"10.1.{edge_num}.{r_index[u]}/24"
        ip_v = f"10.1.{edge_num}.{r_index[v]}/24"

        ports[u].append({
            "ifname": f"eth{pu}",
            "port": pu,
            "peer_desc": r_slug[v],
            "ip": ip_u,
            "local_mac": mac_u,
            "peer_mac": mac_v,
            "passive": False,
            "metric": metric,
            "dist_km": dist_km,
        })
        ports[v].append({
            "ifname": f"eth{pv}",
            "port": pv,
            "peer_desc": r_slug[u],
            "ip": ip_v,
            "local_mac": mac_v,
            "peer_mac": mac_u,
            "passive": False,
            "metric": metric,
            "dist_km": dist_km,
        })
        clab_links.append({
            "a_node": r_slug[u], "a_if": f"eth{pu}", "a_mac": mac_u,
            "b_node": r_slug[v], "b_if": f"eth{pv}", "b_mac": mac_v,
        })

    # Write per-router configs (frr.conf and controller.yaml)
    os.makedirs(configs_dir, exist_ok=True)
    for nid in sub_ids:
        slug = r_slug[nid]
        idx = r_index[nid]
        node_dir = os.path.join(configs_dir, slug)
        os.makedirs(node_dir, exist_ok=True)

        # frr.conf
        frr_lines = [
            "frr version 8.4",
            "frr defaults traditional",
            f"hostname {slug}",
            "",
        ]
        for p in ports[nid]:
            frr_lines.append(f"interface {p['ifname']}")
            frr_lines.append(f" description to_{p['peer_desc']}")
            frr_lines.append(f" ip address {p['ip']}")
            frr_lines.append(" ip router isis FAMTAR")
            if p["passive"]:
                frr_lines.append(" isis passive")
            else:
                frr_lines.append(" isis network point-to-point")
                frr_lines.append(" isis circuit-type level-2-only")
                frr_lines.append(" no isis hello padding")
                frr_lines.append(f" isis metric {p['metric']}")
            frr_lines.append("")

        frr_lines.extend([
            "router isis FAMTAR",
            f" net 49.0001.0000.0000.{idx:04d}.00",
            " metric-style wide",
            " is-type level-2-only",
            " lsp-gen-interval 1",
            " spf-interval 1",
            "",
        ])
        with open(os.path.join(node_dir, "frr.conf"), "w") as f:
            f.write("\n".join(frr_lines))

        # controller.yaml
        mgmt_ip = f"172.20.21.{100 + idx}"
        ctrl_lines = [
            f"name: {slug}",
            f"device_id: {idx}",
            f"thrift_port: {9090 + idx}",
            f"grpc_addr: {mgmt_ip}:9559",
            f"netns: clab-{TOPO_NAME}-{slug}",
            f"frr_container: clab-{TOPO_NAME}-{slug}-frr",
            "port_map:",
        ]
        for p in ports[nid]:
            ctrl_lines.append(f"  {p['ifname']}: {p['port']}")
        ctrl_lines.append("default_metrics:")
        for p in ports[nid]:
            if not p["passive"]:
                ctrl_lines.append(f"  {p['ifname']}: {p['metric']}")
        ctrl_lines.append("dmac:")
        for p in ports[nid]:
            ctrl_lines.append(f"  {p['port']}: [\"{p['peer_mac']}\", \"{p['local_mac']}\"]")
        ctrl_lines.extend([
            "congestion:",
            "  poll_interval_s: 0.1",
            f"  capacity_bps: {capacity_bps}",
            "  th_max: 0.90",
            "  th_min: 0.70",
            "  ewma_alpha: 0.2",
            "  hold_down_s: 2.0",
            "  congested_metric: 1000",
            "",
        ])
        with open(os.path.join(node_dir, "controller.yaml"), "w") as f:
            f.write("\n".join(ctrl_lines))

    # Write topology.nobel.clab.yaml
    topo_lines = [
        f"name: {TOPO_NAME}",
        "",
        "mgmt:",
        f"  network: {MGMT_NET}",
        f"  ipv4-subnet: {MGMT_SUBNET}",
        "",
        "topology:",
        "  nodes:",
    ]

    for nid in sub_ids:
        slug = r_slug[nid]
        idx = r_index[nid]
        mgmt_ip = f"172.20.21.{100 + idx}"
        if_args = " ".join(f"-i {p['port']}@{p['ifname']}" for p in ports[nid])

        topo_lines.extend([
            f"    {slug}:",
            "      kind: linux",
            "      image: p4lang/behavioral-model:latest",
            f"      mgmt-ipv4: {mgmt_ip}",
            "      binds:",
            "        - basic_forwarding.json:/root/basic_forwarding.json",
            "      sysctls:",
            "        net.ipv4.ip_forward: 0",
            "        net.ipv6.conf.all.forwarding: 0",
            "      exec:",
            "        - sysctl -qw net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0",
            "      cmd: >",
            '        bash -c "sleep 2 &&',
            "        simple_switch_grpc",
            f"        --thrift-port {9090 + idx}",
            f"        --device-id {idx}",
            f"        {if_args}",
            "        /root/basic_forwarding.json",
            "        --",
            '        --grpc-server-addr 0.0.0.0:9559"',
            "",
            f"    {slug}-frr:",
            "      kind: linux",
            "      image: frrouting/frr:latest",
            f"      network-mode: container:{slug}",
            "      binds:",
            "        - configs/daemons:/etc/frr/daemons",
            "        - configs/vtysh.conf:/etc/frr/vtysh.conf",
            f"        - configs/nobel/{slug}/frr.conf:/etc/frr/frr.conf",
            "      exec:",
            "        - sysctl -qw net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0",
            "        - apk add --no-cache tcpdump",
            "        - iptables -A OUTPUT -p icmp --icmp-type destination-unreachable -j DROP",
        ])
        for p in ports[nid]:
            if not p["passive"]:
                topo_lines.append(
                    f"        - tc qdisc replace dev {p['ifname']} root tbf rate {tc_rate} burst {DEFAULT_TC_BURST} latency {DEFAULT_TC_LATENCY}"
                )
        topo_lines.append("")

    # Host h1 (at fra) and h2 (at par)
    iperf_servers = [f"        - iperf3 -s -p {port} -D" for port in range(5201, 5209)]
    topo_lines.extend([
        "    h1:",
        "      kind: linux",
        "      image: alpine:latest",
        "      cmd: sleep infinity",
        "      exec:",
        "        - ip addr add 10.0.1.1/24 dev eth1",
        "        - ip route add 10.0.0.0/8 via 10.0.1.254",
        f"        - arp -s 10.0.1.254 {fra_h1_mac}",
        "        - apk add --no-cache iperf3 python3",
        "        - iperf3 -s -D",
        "",
        "    h2:",
        "      kind: linux",
        "      image: alpine:latest",
        "      cmd: sleep infinity",
        "      exec:",
        "        - ip addr add 10.0.2.1/24 dev eth1",
        "        - ip route add 10.0.0.0/8 via 10.0.2.254",
        f"        - arp -s 10.0.2.254 {par_h2_mac}",
        "        - apk add --no-cache iperf3 python3",
        *iperf_servers,
        "",
        "  links:",
    ])

    for link in clab_links:
        topo_lines.extend([
            "    - type: veth",
            "      endpoints:",
            f"        - node: {link['a_node']}",
            f"          interface: {link['a_if']}",
            f"          mac: {link['a_mac']}",
            f"        - node: {link['b_node']}",
            f"          interface: {link['b_if']}",
            f"          mac: {link['b_mac']}",
        ])
    topo_lines.append("")

    with open(out_topo_path, "w") as f:
        f.write("\n".join(topo_lines))

    # Print summary
    print(f"Generated {out_topo_path} ({len(sub_ids)} routers, {len(sub_edges)} transit links + 2 hosts)")
    print(f"Generated configs in {configs_dir}/<slug>/{{frr.conf,controller.yaml}}")
    print("\nRouters:")
    for nid in sub_ids:
        slug = r_slug[nid]
        idx = r_index[nid]
        if_list = ", ".join(f"{p['ifname']}->to_{p['peer_desc']}({p['metric'] or 'host'})" for p in ports[nid])
        print(f"  {idx:2d}. {slug} ({nodes[nid]['name']}) mgmt=172.20.21.{100+idx} thrift={9090+idx} : {if_list}")

    print("\n4 Disjoint Paths (with IS-IS metrics):")
    for pi, p in enumerate(paths, 1):
        p_slugs = " -> ".join(r_slug[x] for x in p)
        cost = sum(isis_metric_from_dist(adj[p[i]][p[i + 1]]) for i in range(len(p) - 1))
        dist = sum(adj[p[i]][p[i + 1]] for i in range(len(p) - 1))
        print(f"  P{pi} ({len(p)-1} hops, {dist:.1f} km, metric={cost}): {p_slugs}")


def main():
    parser = argparse.ArgumentParser(description="Build Containerlab & FAMTAR configs for nobel-eu 11-node subgraph.")
    parser.add_argument("--json", default=os.path.join(PROJECT_ROOT, "nobel-eu.json"))
    parser.add_argument("--topo-out", default=os.path.join(PROJECT_ROOT, "topology.nobel.clab.yaml"))
    parser.add_argument("--configs-dir", default=os.path.join(PROJECT_ROOT, "configs", "nobel"))
    parser.add_argument("--capacity-bps", type=int, default=DEFAULT_CAPACITY_BPS)
    parser.add_argument("--tc-rate", default=DEFAULT_TC_RATE)
    args = parser.parse_args()

    build_topology(args.json, args.topo_out, args.configs_dir, args.capacity_bps, args.tc_rate)


if __name__ == "__main__":
    main()
