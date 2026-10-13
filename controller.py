import sys
import socket
from pyroute2 import NetNS
import p4runtime_sh.shell as sh

ELECTION_ID = (0, 1)
P4INFO_PATH = "basic_forwarding.p4info.txtpb"
BIN_PATH = "basic_forwarding.json"

ROUTERS = [
    {
        "name": "r1",
        "grpc": "172.20.20.4:9559",
        "device_id": 1,
        "netns": "clab-p4-basic-lab-r1",
    },
    {
        "name": "r2",
        "grpc": "172.20.20.5:9559",
        "device_id": 2,
        "netns": "clab-p4-basic-lab-r2",
    },
]


def run(router):
    # map ifindex -> port name
    iface_map = {}
    try:
        with NetNS(router["netns"]) as ipr:
            for link in ipr.get_links():
                iface_map[link["index"]] = link.get_attr("IFLA_IFNAME")
            
            # listen for route changes
            for msg in ipr.monitor():
                event = msg.get("event")

                if event not in ("RTM_NEWROUTE", "RTM_DELROUTE"):
                    continue

                # parse event message to extract route information
                if event == "RTM_NEWROUTE":
                    # update p4 table entry
                    pass
                elif event == "RTM_DELROUTE":
                    # remove p4 table entry
                    pass
                
    except Exception as e:
        print(f"Error in router {router['name']}: {e}", file=sys.stderr)
    return iface_map


def main():
    for r in ROUTERS:
        iface_map = run(r)
        r["iface_map"] = iface_map

    # r1: eth1 (port 1) -> h1, eth2 (port 2) -> r2
    try:
        sh.setup(
            device_id=ROUTERS[0]["device_id"],
            grpc_addr=ROUTERS[0]["grpc"],
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
            grpc_addr=ROUTERS[1]["grpc"],
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


if __name__ == "__main__":
    main()