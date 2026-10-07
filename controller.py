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

    te_h2 = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.ipv4_forward")
    te_h2.match["hdr.ipv4.dstAddr"] = "10.0.2.1"
    te_h2.action["dstMac"] = "aa:c1:ab:c0:7a:8c"
    te_h2.action["egress_port"] = "2"
    te_h2.insert()

    te_h1 = sh.TableEntry("MyIngress.ipv4_lpm")(action="MyIngress.ipv4_forward")
    te_h1.match["hdr.ipv4.dstAddr"] = "10.0.1.1"
    te_h1.action["dstMac"] = "aa:c1:ab:35:4c:aa"
    te_h1.action["egress_port"] = "1"
    te_h1.insert()

    sh.teardown()

if __name__ == "__main__":
    main()