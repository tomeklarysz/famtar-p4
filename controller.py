import argparse
import threading
import socket
import subprocess
import time
import yaml
from pyroute2 import NetNS
import p4runtime_sh.shell as sh

P4INFO_PATH = "basic_forwarding.p4info.txtpb"
BIN_PATH = "basic_forwarding.json"
ELECTION_ID = (0, 1)


class P4Router:
    def __init__(self, conf, mode="famtar"):
        self.conf = conf
        self.mode = mode
        self.ecmp_groups = {}  # prefix -> group_id
        self.name = conf["name"]
        self.grpc_addr = conf["grpc_addr"]
        self.device_id = conf["device_id"]
        self.netns = conf["netns"]
        self.frr_container = conf.get("frr_container", f"clab-p4-basic-lab-{self.name}-frr")
        self.port_map = conf["port_map"]
        self.dmac = conf.get("dmac", {})
        self.default_metrics = conf.get("default_metrics", {})
        self.iface_map = {}
        self.client = None
        self.netns_ready = threading.Event()
        self.p4_ready = threading.Event()

    def ifname_from_port(self, port):
        return next((name for name, p in self.port_map.items() if p == port), None)

    def init_p4(self):
        sh.setup(
            device_id=self.device_id,
            grpc_addr=self.grpc_addr,
            election_id=ELECTION_ID,
            config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH),
            verbose=False,
        )
        self.client = sh.client
        sh.global_options["verbose"] = False

        for port, (dst_mac, src_mac) in self.dmac.items():
            te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
            te.match["standard_metadata.egress_spec"] = str(port)
            te.action["dst_mac"] = dst_mac
            te.action["src_mac"] = src_mac
            te.insert()
            print(f"[{self.name}] dmac: port {port} -> dst {dst_mac}, src {src_mac}", flush=True)

        for port in self.port_map.values():
            member = sh.ActionProfileMember("MyIngress.ecmp_selector")(member_id=port, action="MyIngress.set_nhop")
            member.action["port"] = str(port)
            try:
                member.insert()
            except Exception:
                member.modify()
            print(f"[{self.name}] member: port {port} (member_id {port})", flush=True)

    def run(self):
        with NetNS(self.netns, flags=0) as ipr:
            ipr.bind()
            self.netns_ready.set()
            self.p4_ready.wait()
            print(f"[{self.name}] listening on {self.netns}", flush=True)

            for link in ipr.get_links():
                self.iface_map[link["index"]] = link.get_attr("IFLA_IFNAME")

            for msg in ipr.get_routes(family=socket.AF_INET, table=254):
                route_info = self.parse_route_msg(msg)
                if route_info and route_info['p4_port']:
                    print(f"[{self.name}] Existing route: {route_info}", flush=True)
                    self.update_p4_route(route_info["prefix"], route_info["p4_ports"])

            while True:
                for msg in ipr.get():
                    event = msg.get("event")
                    if event not in ("RTM_NEWROUTE", "RTM_DELROUTE"):
                        continue
                    route_info = self.parse_route_msg(msg)
                    if not route_info:
                        continue
                    if event == "RTM_NEWROUTE":
                        print(f"[{self.name}] New route added: {route_info}", flush=True)
                        if route_info["p4_port"] is None:
                            print(f"[{self.name}] Skipping {route_info['prefix']}: "
                                  f"interface {route_info['ifname']} not in port_map", flush=True)
                            continue
                        self.update_p4_route(route_info["prefix"], route_info["p4_ports"])
                    elif event == "RTM_DELROUTE":
                        print(f"[{self.name}] Route removed: {route_info}", flush=True)
                        self.delete_p4_route(route_info["prefix"])

    def parse_route_msg(self, msg):
        if msg.get('family') != socket.AF_INET:
            return None
        if msg.get('table') != 254:
            return None
        dst_len = msg.get('dst_len')
        dst_ip = msg.get_attr('RTA_DST')
        oif = msg.get_attr('RTA_OIF')
        gateway = msg.get_attr('RTA_GATEWAY')
        prefix = f"{dst_ip}/{dst_len}" if dst_ip else "0.0.0.0/0"

        if oif is None:
            for nh in msg.get_attr('RTA_MULTIPATH') or []:
                nh_ifname = self.iface_map.get(nh.get('oif'))
                if nh_ifname in self.port_map:
                    oif = nh.get('oif')
                    gateway = nh.get_attr('RTA_GATEWAY')
                    break

        ifname = self.iface_map.get(oif)
        p4_port = self.port_map.get(ifname)

        p4_ports = []
        for nh in msg.get_attr('RTA_MULTIPATH') or []:
            p = self.port_map.get(self.iface_map.get(nh.get('oif')))
            if p is not None and p not in p4_ports:
                p4_ports.append(p)
        if not p4_ports and p4_port is not None:
            p4_ports = [p4_port]

        return {
            "prefix": prefix,
            "ifname": ifname,
            "p4_port": p4_port,
            "p4_ports": p4_ports,
            "gateway": gateway
        }

    def update_p4_route(self, prefix, p4_ports):
        if self.mode == "ecmp" and len(p4_ports) > 1:
            self.update_p4_ecmp_route(prefix, p4_ports)
            return
        p4_port = p4_ports[0]
        te = sh.TableEntry("MyIngress.ipv4_lpm")
        te.match["hdr.ipv4.dstAddr"] = prefix
        te.member_id = p4_port
        try:
            te.insert()
            print(f"[{self.name}] P4 route added: {prefix} -> port {p4_port} (member {p4_port})", flush=True)
        except Exception:
            te.modify()
            print(f"[{self.name}] P4 route modified: {prefix} -> port {p4_port} (member {p4_port})", flush=True)

    def update_p4_ecmp_route(self, prefix, p4_ports):
        group_id = self.ecmp_groups.setdefault(prefix, len(self.ecmp_groups) + 100)
        group = sh.ActionProfileGroup("MyIngress.ecmp_selector")(group_id=group_id)
        for port in p4_ports:
            group.add(port)
        try:
            group.insert()
        except Exception:
            group.modify()

        te = sh.TableEntry("MyIngress.ipv4_lpm")
        te.match["hdr.ipv4.dstAddr"] = prefix
        te.group_id = group_id
        try:
            te.insert()
            print(f"[{self.name}] P4 ECMP route added: {prefix} -> ports {p4_ports} (group {group_id})", flush=True)
        except Exception:
            te.modify()
            print(f"[{self.name}] P4 ECMP route modified: {prefix} -> ports {p4_ports} (group {group_id})", flush=True)

    def delete_p4_route(self, prefix):
        te = sh.TableEntry("MyIngress.ipv4_lpm")
        te.match["hdr.ipv4.dstAddr"] = prefix
        try:
            te.delete()
            print(f"[{self.name}] P4 route deleted: {prefix}", flush=True)
        except Exception as e:
            print(f"[{self.name}] Error deleting P4 route {prefix}: {e}", flush=True)

    def read_port_bytes(self, port):
        try:
            ce = sh.CounterEntry("MyIngress.port_bytes_counter")
            ce.index = port
            for entry in ce.read():
                return entry.data.byte_count
        except Exception as e:
            return 0
        return 0

    def set_isis_metric(self, ifname, metric):
        subprocess.run([
            "docker", "exec", self.frr_container,
            "vtysh", "-c", "conf t", "-c", f"interface {ifname}", "-c", f"isis metric {metric}"
        ], check=False)

    def poll_traffic_loop(self):
        print(f"[{self.name}] starting polling traffic loop for FAMTAR", flush=True)
        cfg = self.conf.get("congestion", {})
        interval = cfg.get("poll_interval_s", 0.1)
        capacity = cfg.get("capacity_bps", 10_000_000)
        th_max = cfg.get("th_max", 0.90)
        th_min = cfg.get("th_min", 0.70)
        alpha = cfg.get("ewma_alpha", 0.5)
        hold_down = cfg.get("hold_down_s", 2.0)
        congested_metric = cfg.get("congested_metric", 1000)

        port_state = {}
        for ifname, port in self.port_map.items():
            if ifname in self.default_metrics:
                port_state[port] = {
                    "ifname": ifname,
                    "prev_bytes": self.read_port_bytes(port),
                    "prev_time": time.time(),
                    "ewma_rate": 0.0,
                    "congested": False,
                    "last_state_change": 0.0,
                }

        while True:
            time.sleep(interval)
            now = time.time()
            for port, st in port_state.items():
                curr_bytes = self.read_port_bytes(port)
                dt = now - st["prev_time"]
                if dt <= 0:
                    continue

                delta_bytes = curr_bytes - st["prev_bytes"]
                if delta_bytes < 0:
                    # Licznik zresetowany w playbooku
                    delta_bytes = 0

                instant_rate = (delta_bytes * 8.0) / dt
                st["ewma_rate"] = alpha * instant_rate + (1.0 - alpha) * st["ewma_rate"]
                st["prev_bytes"] = curr_bytes
                st["prev_time"] = now

                utilization = st["ewma_rate"] / capacity
                if not st["congested"]:
                    if utilization >= th_max and (now - st["last_state_change"] >= hold_down):
                        st["congested"] = True
                        st["last_state_change"] = now
                        print(f"[{self.name}] CONGESTION DETECTED on port {port} ({st['ifname']}) "
                              f"rate={st['ewma_rate']/1e6:.2f}M ({utilization*100:.1f}%) -> setting IS-IS metric to {congested_metric}", flush=True)
                        self.set_isis_metric(st["ifname"], congested_metric)
                else:
                    if utilization < th_min and (now - st["last_state_change"] >= hold_down):
                        st["congested"] = False
                        st["last_state_change"] = now
                        default_m = self.default_metrics.get(st["ifname"], 10)
                        print(f"[{self.name}] CONGESTION CLEARED on port {port} ({st['ifname']}) "
                              f"rate={st['ewma_rate']/1e6:.2f}M ({utilization*100:.1f}%) -> restoring IS-IS metric to {default_m}", flush=True)
                        self.set_isis_metric(st["ifname"], default_m)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to router YAML config")
    parser.add_argument("--mode", choices=["famtar", "ecmp"], default="famtar")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        conf = yaml.safe_load(f)

    print(f"[*] controller starting for {conf['name']} (mode: {args.mode}) from {args.config}", flush=True)

    sh.global_options["verbose"] = False
    router = P4Router(conf, mode=args.mode)

    t_route = threading.Thread(target=router.run, daemon=True)
    t_route.start()
    router.netns_ready.wait()

    try:
        router.init_p4()
        router.p4_ready.set()
    except Exception as e:
        print(f"Error initializing router {router.name}: {e}", flush=True)
        return

    threads = [t_route]
    if router.mode == "famtar":
        t_poll = threading.Thread(target=router.poll_traffic_loop, daemon=True)
        t_poll.start()
        threads.append(t_poll)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
