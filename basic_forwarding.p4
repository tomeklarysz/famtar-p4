#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8>  PROTO_TCP = 6;
const bit<8>  PROTO_UDP = 17;

const bit<32> FFT_SIZE       = 65536;
const bit<48> FFT_TIMEOUT_US = 30000000;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

header tcp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<32> seqNo;
    bit<32> ackNo;
    bit<4>  dataOffset;
    bit<3>  res;
    bit<3>  ecn;
    bit<6>  ctrl;
    bit<16> window;
    bit<16> checksum;
    bit<16> urgentPtr;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length_;
    bit<16> checksum;
}

struct headers {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    tcp_t      tcp;
    udp_t      udp;
}

struct metadata {
    bit<16> l4_src_port;
    bit<16> l4_dst_port;
    bit<16> fft_index;
    bit<32> flow_signature;
    bit<1>  fft_hit;
    bit<1>  cache_fft;
    bit<32> next_hop_ip;
}

parser MyParser(packet_in packet,
                out headers hdr,
                inout metadata meta,
                inout standard_metadata_t standard_metadata) {

    state start {
        transition parse_ethernet;
    }

    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            TYPE_IPV4: parse_ipv4;
            default:   accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            PROTO_TCP: parse_tcp;
            PROTO_UDP: parse_udp;
            default:   accept;
        }
    }

    state parse_tcp {
        packet.extract(hdr.tcp);
        transition accept;
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply {
       // empty for now, just for test purposes
    }
}

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t standard_metadata) {

    register<bit<32>>(FFT_SIZE) reg_signature;
    register<bit<9>>(FFT_SIZE)  reg_egress_port;
    register<bit<8>>(FFT_SIZE)  reg_saved_ttl;
    register<bit<48>>(FFT_SIZE) reg_timestamp;

    action drop() {
        mark_to_drop(standard_metadata);
    }

    action set_nhop(bit<9> port) {
        standard_metadata.egress_spec = port;
    }

    action set_dmac(bit<48> dst_mac, bit<48> src_mac) {
        hdr.ethernet.dstAddr = dst_mac;
        hdr.ethernet.srcAddr = src_mac;
    }

    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            set_nhop;
            drop;
            NoAction;
        }
        size = 1024;
        default_action = drop();
    }

    table dmac_table {
        key = {
            standard_metadata.egress_spec: exact;
        }
        actions = {
            set_dmac;
            drop;
            NoAction;
        }
        size = 64;
        default_action = drop();
    }

    apply {
        if (!hdr.ipv4.isValid()) {
            drop();
            return;
        }

        if (hdr.ipv4.ttl == 0) {
            drop();
            return;
        }

        meta.fft_hit     = 0;
        meta.cache_fft   = 0;
        meta.l4_src_port = 0;
        meta.l4_dst_port = 0;

        bool is_tcp_udp = false;

        if (hdr.tcp.isValid()) {
            is_tcp_udp       = true;
            meta.l4_src_port = hdr.tcp.srcPort;
            meta.l4_dst_port = hdr.tcp.dstPort;
        } else if (hdr.udp.isValid()) {
            is_tcp_udp       = true;
            meta.l4_src_port = hdr.udp.srcPort;
            meta.l4_dst_port = hdr.udp.dstPort;
        }

        if (is_tcp_udp) {
            hash(meta.fft_index,
                 HashAlgorithm.crc16,
                 (bit<16>)0,
                 { hdr.ipv4.srcAddr,
                   hdr.ipv4.dstAddr,
                   meta.l4_src_port,
                   meta.l4_dst_port,
                   hdr.ipv4.protocol },
                 (bit<32>)65536);

            hash(meta.flow_signature,
                 HashAlgorithm.crc32,
                 (bit<32>)0,
                 { hdr.ipv4.srcAddr,
                   hdr.ipv4.dstAddr,
                   meta.l4_src_port,
                   meta.l4_dst_port,
                   hdr.ipv4.protocol },
                 (bit<32>)0xFFFFFFFF);

            bit<32> stored_sig;
            bit<9>  stored_port;
            bit<8>  stored_ttl;
            bit<48> stored_ts;

            reg_signature.read(stored_sig,     (bit<32>)meta.fft_index);
            reg_egress_port.read(stored_port,  (bit<32>)meta.fft_index);
            reg_saved_ttl.read(stored_ttl,     (bit<32>)meta.fft_index);
            reg_timestamp.read(stored_ts,      (bit<32>)meta.fft_index);

            bit<48> now = standard_metadata.ingress_global_timestamp;
            bit<48> age = now - stored_ts;

            if (stored_sig == 0 || age > FFT_TIMEOUT_US) {
                meta.cache_fft = 1;
            } else if (stored_sig == meta.flow_signature) {
                if (hdr.ipv4.ttl < stored_ttl) {
                    reg_signature.write((bit<32>)meta.fft_index, (bit<32>)0);
                    meta.cache_fft = 1;
                } else {
                    meta.fft_hit = 1;
                    standard_metadata.egress_spec = stored_port;
                    reg_timestamp.write((bit<32>)meta.fft_index, now);
                }
            }
        }

        if (meta.fft_hit == 0) {
            ipv4_lpm.apply();
        }

        if (meta.cache_fft == 1) {
            reg_signature.write((bit<32>)meta.fft_index,   meta.flow_signature);
            reg_egress_port.write((bit<32>)meta.fft_index, standard_metadata.egress_spec);
            reg_saved_ttl.write((bit<32>)meta.fft_index,   hdr.ipv4.ttl);
            reg_timestamp.write((bit<32>)meta.fft_index,
                                standard_metadata.ingress_global_timestamp);
        }

        dmac_table.apply();

        hdr.ipv4.ttl = hdr.ipv4.ttl - 1;
    }
}

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t standard_metadata) {
    apply {
        // empty for now, just for test purposes
    }
}

control MyComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version,
              hdr.ipv4.ihl,
              hdr.ipv4.diffserv,
              hdr.ipv4.totalLen,
              hdr.ipv4.identification,
              hdr.ipv4.flags,
              hdr.ipv4.fragOffset,
              hdr.ipv4.ttl,
              hdr.ipv4.protocol,
              hdr.ipv4.srcAddr,
              hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

control MyDeparser(packet_out packet, in headers hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.tcp);
        packet.emit(hdr.udp);
    }
}

// switch instantiation

V1Switch(
    MyParser(),
    MyVerifyChecksum(),
    MyIngress(),
    MyEgress(),
    MyComputeChecksum(),
    MyDeparser()
) main;