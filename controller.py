import threading
import socket
from pyroute2 import NetNS
import p4runtime_sh.shell as sh

class P4Router:
    def __init__(self, name, grpc_addr, device_id, netns, port_map):
        self.name = name
        self.grpc_addr = grpc_addr
        self.device_id = device_id
        self.netns = netns
        self.port_map = port_map
        self.iface_map = {}

    def run(self):
        with NetNS(self.netns) as ipr:
            # map ifindex -> port name
            for link in ipr.get_links():
                self.iface_map[link["index"]] = link.get_attr("IFLA_IFNAME")
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
                        # update p4 table entry
                        pass
                    elif event == "RTM_DELROUTE":
                        # remove p4 table entry
                        pass

    def parse_route_msg(self, msg):
        if msg.get('family') != socket.AF_INET:
            return None
        dst_len = msg.get('dst_len')
        dst_ip = msg.get_attr('RTA_DST')
        oif = msg.get_attr('RTA_OIF')
        gateway = msg.get_attr('RTA_GATEWAY')
        prefix = f"{dst_ip}/{dst_len}" if dst_ip else "0.0.0.0/0"
        ifname = self.iface_map.get(oif)
        p4_port = self.port_map.get(ifname)

        return {
            "prefix": prefix,
            "ifname": ifname,
            "p4_port": p4_port,
            "gateway": gateway
        }

ELECTION_ID = (0, 1)
P4INFO_PATH = "basic_forwarding.p4info.txtpb"
BIN_PATH = "basic_forwarding.json"

ROUTERS = [
    {
        "name": "r1",
        "grpc_addr": "172.20.20.4:9559",
        "device_id": 1,
        "netns": "clab-p4-basic-lab-r1",
        "port_map": {
            "eth1": 1,  # port to h1
            "eth2": 2,  # port to r2
        }
    },
    {
        "name": "r2",
        "grpc_addr": "172.20.20.5:9559",
        "device_id": 2,
        "netns": "clab-p4-basic-lab-r2",
        "port_map": {
            "eth1": 1,  # port to r1
            "eth2": 2,  # port to h2
        }
    },
]



def main():

    # r1: eth1 (port 1) -> h1, eth2 (port 2) -> r2
    try:
        sh.setup(
            device_id=ROUTERS[0]["device_id"],
            grpc_addr=ROUTERS[0]["grpc_addr"],
            election_id=ELECTION_ID,
            config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH),
        )

        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.set_nhop")
        te.match["hdr.ipv4.dstAddr"] = "10.0.1.0/24"
        te.action["port"] = "1"
        te.insert()

        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.set_nhop")
        te.match["hdr.ipv4.dstAddr"] = "10.0.2.0/24"
        te.action["port"] = "2"
        te.insert()

        te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
        te.match["standard_metadata.egress_spec"] = "1"
        te.action["dst_mac"] = "00:00:00:00:01:01"
        te.action["src_mac"] = "00:00:00:00:00:fe"
        te.insert()

        te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
        te.match["standard_metadata.egress_spec"] = "2"
        te.action["dst_mac"] = "00:00:00:00:00:03"
        te.action["src_mac"] = "00:00:00:00:00:fe"
        te.insert()

        sh.teardown()
    except Exception as e:
        print(f"Error in P4Runtime for r1: {e}")

    # r2: eth1 (port 1) -> r1, eth2 (port 2) -> h2
    try:
        sh.setup(
            device_id=ROUTERS[1]["device_id"],
            grpc_addr=ROUTERS[1]["grpc_addr"],
            election_id=ELECTION_ID,
            config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH),
        )

        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.set_nhop")
        te.match["hdr.ipv4.dstAddr"] = "10.0.1.0/24"
        te.action["port"] = "1"
        te.insert()

        te = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.set_nhop")
        te.match["hdr.ipv4.dstAddr"] = "10.0.2.0/24"
        te.action["port"] = "2"
        te.insert()

        te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
        te.match["standard_metadata.egress_spec"] = "1"
        te.action["dst_mac"] = "00:00:00:00:00:02"
        te.action["src_mac"] = "00:00:00:00:00:fe"
        te.insert()

        te = sh.TableEntry("MyIngress.dmac_table")(action="MyIngress.set_dmac")
        te.match["standard_metadata.egress_spec"] = "2"
        te.action["dst_mac"] = "00:00:00:00:02:02"
        te.action["src_mac"] = "00:00:00:00:00:fe"
        te.insert()

        sh.teardown()
    except Exception as e:
        print(f"Error in P4Runtime for r2: {e}")

    threads = []
    for r in ROUTERS:
        router = P4Router(
            name=r["name"],
            grpc_addr=r["grpc_addr"],
            device_id=r["device_id"],
            netns=r["netns"],
            port_map=r["port_map"]
        )
        t = threading.Thread(target=router.run, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()