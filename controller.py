import sys
import p4runtime_sh.shell as sh

SWITCH_GRPC_ADDR = "172.20.20.4:9559"
DEVICE_ID = 1
ELECTION_ID = (0, 1)

P4INFO_PATH = "basic_forwarding.p4info.txtpb"
BIN_PATH = "basic_forwarding.json"


def main():
    try:
        sh.setup(
            device_id=DEVICE_ID,
            grpc_addr=SWITCH_GRPC_ADDR,
            election_id=ELECTION_ID,
            config=sh.FwdPipeConfig(P4INFO_PATH, BIN_PATH)
        )
    except Exception as e:
        print(f"[!] error setting up P4Runtime: {e}")
        sys.exit(1)

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
    te.action["dst_mac"] = "00:00:00:00:02:02"
    te.action["src_mac"] = "00:00:00:00:00:fe"
    te.insert()

    sh.teardown()


if __name__ == "__main__":
    main()