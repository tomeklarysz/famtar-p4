import threading
import socket
from pyroute2 import NetNS
import p4runtime_sh.shell as sh

p4_lock = threading.Lock()

class P4Router:
    def __init__(self, name, grpc_addr, device_id, netns, port_map, client=None):
        self.name = name
        self.grpc_addr = grpc_addr
        self.device_id = device_id
        self.netns = netns
        self.port_map = port_map
        self.iface_map = {}
        self.client = client

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
                    # parse event message to extract route information
                    route_info = self.parse_route_msg(msg)
                    if not route_info:
                        continue
                    if event == "RTM_NEWROUTE":
                        print(f"[{self.name}] New route added: {route_info}", flush=True)
                        if route_info["p4_port"] is None:
                            print(f"[{self.name}] Skipping {route_info['prefix']}: "
                                  f"interface {route_info['ifname']} not in port_map", flush=True)
                            continue
                        # update p4 table entry
                        self.update_p4_route(route_info["prefix"], route_info["p4_port"])
                    elif event == "RTM_DELROUTE":
                        print(f"[{self.name}] Route removed: {route_info}", flush=True)
                        # remove p4 table entry
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

# "dmac" maps egress P4 port -> (dst_mac, src_mac) used by MyIngress.dmac_table:
#   dst_mac = MAC of the neighbor interface on that link
#   src_mac = MAC of our own interface on that link
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


def connect_router(r, first):
    """Connect to a switch, push the P4 pipeline and return the client."""
    if first:
        # sh.setup() also loads P4Info into the global p4runtime_sh context,
        # which TableEntry needs to resolve table/action names
        sh.setup(
            device_id=r["device_id"],
            grpc_addr=r["grpc_addr"],
            election_id=ELECTION_ID,
            config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH),
        )
        return sh.client
    client = sh.P4RuntimeClient(
        device_id=r["device_id"],
        grpc_addr=r["grpc_addr"],
        election_id=ELECTION_ID,
    )
    client.set_fwd_pipe_config(P4INFO_PATH, BIN_PATH)
    return client


def install_dmac_entries(r, client):
    """Fill MyIngress.dmac_table: egress port -> rewrite of L2 addresses."""
    with p4_lock:
        sh.client = client
        for port, (dst_mac, src_mac) in r["dmac"].items():
            te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
            te.match["standard_metadata.egress_spec"] = str(port)
            te.action["dst_mac"] = dst_mac
            te.action["src_mac"] = src_mac
            te.insert()
            print(f"[{r['name']}] dmac: port {port} -> dst {dst_mac}, src {src_mac}", flush=True)


def main():
    # disable p4runtime_sh protobuf dumps on every match/action assignment
    sh.global_options["verbose"] = False

    clients = {}

    # ipv4_lpm entries are not installed statically - P4Router.run() syncs
    # them from the kernel routing table (connected + IS-IS routes from FRR)
    for i, r in enumerate(ROUTERS):
        try:
            client = connect_router(r, first=(i == 0))
            clients[r["name"]] = client
            install_dmac_entries(r, client)
        except Exception as e:
            print(f"Error in P4Runtime for {r['name']}: {e}", flush=True)

    threads = []
    for r in ROUTERS:
        if r["name"] not in clients:
            print(f"[{r['name']}] no P4Runtime connection, skipping route sync", flush=True)
            continue
        router = P4Router(
            name=r["name"],
            grpc_addr=r["grpc_addr"],
            device_id=r["device_id"],
            netns=r["netns"],
            port_map=r["port_map"],
            client=clients[r["name"]]
        )
        t = threading.Thread(target=router.run, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
