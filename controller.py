import threading
import socket
import subprocess
from pyroute2 import NetNS
import p4runtime_sh.shell as sh

p4_lock = threading.Lock()

class P4Router:
    def __init__(self, conf):
        self.name = conf["name"]
        self.grpc_addr = conf["grpc_addr"]
        self.device_id = conf["device_id"]
        self.netns = conf["netns"]
        self.port_map = conf["port_map"]
        self.dmac = conf.get("dmac", {})
        self.default_metrics = conf.get("default_metrics", {})
        self.iface_map = {}
        self.client = None

    def ifname_from_port(self, port):
        return next((name for name, p in self.port_map.items() if p == port), None)

    def init_p4(self, first=False):
        """Connect to switch, push pipeline, install DMAC rules and enable digests."""
        with p4_lock:
            if first:
                sh.setup(
                    device_id=self.device_id,
                    grpc_addr=self.grpc_addr,
                    election_id=ELECTION_ID,
                    config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH),
                    verbose=False,
                )
                self.client = sh.client
            else:
                self.client = sh.P4RuntimeClient(
                    device_id=self.device_id,
                    grpc_addr=self.grpc_addr,
                    election_id=ELECTION_ID,
                )
                self.client.set_fwd_pipe_config(P4INFO_PATH, BIN_PATH)

            sh.client = self.client
            sh.global_options["verbose"] = False

            for port, (dst_mac, src_mac) in self.dmac.items():
                te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
                te.match["standard_metadata.egress_spec"] = str(port)
                te.action["dst_mac"] = dst_mac
                te.action["src_mac"] = src_mac
                te.insert()
                print(f"[{self.name}] dmac: port {port} -> dst {dst_mac}, src {src_mac}", flush=True)

            de = sh.DigestEntry("congestion_digest_t")
            de.max_timeout_ns = 0
            de.max_list_size = 1
            de.ack_timeout_ns = 0
            de.insert()
            print(f"[{self.name}] DigestEntry installed for congestion_digest_t", flush=True)

    def run(self):
        # flags=0: do not create a new empty netns if the name is wrong
        with NetNS(self.netns, flags=0) as ipr:
            ipr.bind()
            print(f"[{self.name}] listening on {self.netns}", flush=True)

            # map ifindex -> port name
            for link in ipr.get_links():
                self.iface_map[link["index"]] = link.get_attr("IFLA_IFNAME")

            # sync existing routes
            for msg in ipr.get_routes(family=socket.AF_INET, table=254):
                route_info = self.parse_route_msg(msg)
                if route_info and route_info['p4_port']:
                    print(f"[{self.name}] Existing route: {route_info}", flush=True)
                    self.update_p4_route(route_info["prefix"], route_info["p4_port"])

            # listen for route changes
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
                        self.update_p4_route(route_info["prefix"], route_info["p4_port"])
                    elif event == "RTM_DELROUTE":
                        print(f"[{self.name}] Route removed: {route_info}", flush=True)
                        self.delete_p4_route(route_info["prefix"])

    def parse_route_msg(self, msg):
        if msg.get('family') != socket.AF_INET:
            return None
        if msg.get('table') != 254:  # main routing table
            return None
        dst_len = msg.get('dst_len')
        dst_ip = msg.get_attr('RTA_DST')
        oif = msg.get_attr('RTA_OIF')
        gateway = msg.get_attr('RTA_GATEWAY')
        prefix = f"{dst_ip}/{dst_len}" if dst_ip else "0.0.0.0/0"

        # ECMP routes (e.g. OSPF with equal-cost paths) carry no RTA_OIF /
        # RTA_GATEWAY - next hops are in RTA_MULTIPATH instead. The P4 table
        # supports a single egress port, so use the first mapped next hop.
        if oif is None:
            for nh in msg.get_attr('RTA_MULTIPATH') or []:
                nh_ifname = self.iface_map.get(nh.get('oif'))
                if nh_ifname in self.port_map:
                    oif = nh.get('oif')
                    gateway = nh.get_attr('RTA_GATEWAY')
                    break

        ifname = self.iface_map.get(oif)
        p4_port = self.port_map.get(ifname)

        return {
            "prefix": prefix,
            "ifname": ifname,
            "p4_port": p4_port,
            "gateway": gateway
        }

    def update_p4_route(self, prefix, p4_port):
        with p4_lock:
            sh.client = self.client
            te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.set_nhop")
            te.match["hdr.ipv4.dstAddr"] = prefix
            te.action["port"] = str(p4_port)
            try:
                te.insert()
                print(f"[{self.name}] P4 route added: {prefix} -> port {p4_port}", flush=True)
            except Exception:
                te.modify()
                print(f"[{self.name}] P4 route modified: {prefix} -> port {p4_port}", flush=True)

    def delete_p4_route(self, prefix):
        with p4_lock:
            sh.client = self.client
            te = sh.TableEntry("MyIngress.ipv4_lpm")
            te.match["hdr.ipv4.dstAddr"] = prefix
            try:
                te.delete()
                print(f"[{self.name}] P4 route deleted: {prefix}", flush=True)
            except Exception as e:
                print(f"[{self.name}] Error deleting P4 route {prefix}: {e}", flush=True)

    def listen_digests(self):
        print(f"[{self.name}] listening for congestion digests via gRPC stream", flush=True)
        while True:
            msg = self.client.get_stream_packet("digest", timeout=None)
            if msg is None:
                break
            for entry in msg.digest.data:
                port = int.from_bytes(entry.struct.members[0].bitstring, "big")
                status = int.from_bytes(entry.struct.members[1].bitstring, "big")
                ifname = self.ifname_from_port(port)
                if not ifname:
                    print(f"[{self.name}] Digest for unmapped port {port} (status={status})", flush=True)
                    continue
                if ifname not in self.default_metrics:
                    # ignore host interfaces - not transit
                    continue
                if status == 1:
                    print(f"[{self.name}] CONGESTION DETECTED on port {port} ({ifname}) -> setting IS-IS metric to 1000", flush=True)
                    subprocess.run([
                        "docker", "exec", f"clab-p4-basic-lab-{self.name}-frr",
                        "vtysh", "-c", "conf t", "-c", f"interface {ifname}", "-c", "isis metric 1000"
                    ], check=False)
                elif status == 0:
                    default_metric = self.default_metrics.get(ifname, 10)
                    print(f"[{self.name}] CONGESTION CLEARED on port {port} ({ifname}) -> restoring IS-IS metric to {default_metric}", flush=True)
                    subprocess.run([
                        "docker", "exec", f"clab-p4-basic-lab-{self.name}-frr",
                        "vtysh", "-c", "conf t", "-c", f"interface {ifname}", "-c", f"isis metric {default_metric}"
                    ], check=False)

            ack = sh.p4runtime_pb2.StreamMessageRequest()
            ack.digest_ack.digest_id = msg.digest.digest_id
            ack.digest_ack.list_id = msg.digest.list_id
            self.client.stream_out_q.put(ack)

    def start(self):
        t_route = threading.Thread(target=self.run, daemon=True)
        t_digest = threading.Thread(target=self.listen_digests, daemon=True)
        t_route.start()
        t_digest.start()
        return [t_route, t_digest]


ROUTERS = [
    {
        "name": "r1",
        "grpc_addr": "172.20.20.4:9559",
        "device_id": 1,
        "netns": "clab-p4-basic-lab-r1",
        "port_map": {
            "eth1": 1,  # port to h1
            "eth2": 2,  # port to r2
            "eth3": 3,  # port to r3
        },
        "default_metrics": {
            "eth2": 10,
            "eth3": 20,
        },
        "dmac": {
            1: ("00:00:00:00:01:01", "00:00:00:00:00:01"),  # h1:eth1
            2: ("00:00:00:00:00:21", "00:00:00:00:00:12"),  # r2:eth1
            3: ("00:00:00:00:00:31", "00:00:00:00:00:13"),  # r3:eth1
        },
    },
    {
        "name": "r2",
        "grpc_addr": "172.20.20.5:9559",
        "device_id": 2,
        "netns": "clab-p4-basic-lab-r2",
        "port_map": {
            "eth1": 1,  # port to r1
            "eth2": 2,  # port to r4
        },
        "default_metrics": {
            "eth1": 10,
            "eth2": 10,
        },
        "dmac": {
            1: ("00:00:00:00:00:12", "00:00:00:00:00:21"),  # r1:eth2
            2: ("00:00:00:00:00:42", "00:00:00:00:00:24"),  # r4:eth2
        },
    },
    {
        "name": "r3",
        "grpc_addr": "172.20.20.6:9559",
        "device_id": 3,
        "netns": "clab-p4-basic-lab-r3",
        "port_map": {
            "eth1": 1,  # port to r1
            "eth2": 2,  # port to r4
        },
        "default_metrics": {
            "eth1": 20,
            "eth2": 20,
        },
        "dmac": {
            1: ("00:00:00:00:00:13", "00:00:00:00:00:31"),  # r1:eth3
            2: ("00:00:00:00:00:43", "00:00:00:00:00:34"),  # r4:eth3
        },
    },
    {
        "name": "r4",
        "grpc_addr": "172.20.20.7:9559",
        "device_id": 4,
        "netns": "clab-p4-basic-lab-r4",
        "port_map": {
            "eth1": 1,  # port to h2
            "eth2": 2,  # port to r2
            "eth3": 3,  # port to r3
        },
        "default_metrics": {
            "eth2": 10,
            "eth3": 20,
        },
        "dmac": {
            1: ("00:00:00:00:02:02", "00:00:00:00:00:04"),  # h2:eth1
            2: ("00:00:00:00:00:24", "00:00:00:00:00:42"),  # r2:eth2
            3: ("00:00:00:00:00:34", "00:00:00:00:00:43"),  # r3:eth2
        },
    },
]

ELECTION_ID = (0, 1)
P4INFO_PATH = "basic_forwarding.p4info.txtpb"
BIN_PATH = "basic_forwarding.json"

def main():
    sh.global_options["verbose"] = False
    threads = []

    for i, r_conf in enumerate(ROUTERS):
        try:
            router = P4Router(r_conf)
            router.init_p4(first=(i == 0))
            threads.extend(router.start())
        except Exception as e:
            print(f"Error initializing router {r_conf['name']}: {e}", flush=True)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
