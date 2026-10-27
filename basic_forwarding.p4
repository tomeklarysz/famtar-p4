#include <core.p4>
#include <v1model.p4>

const bit<16> TYPE_IPV4 = 0x0800;
const bit<8>  PROTO_TCP = 6;
const bit<8>  PROTO_UDP = 17;

const bit<32> FFT_SIZE       = 65536;
const bit<48> FFT_TIMEOUT_US = 30000000;

const bit<48> CONG_WINDOW_US        = 200000;   // 200 ms
const bit<64> LINK_CAPACITY_BYTES_S = 1250000;  // 10 Mbit/s = 1 250 000 B/s
const bit<64> WINDOW_CAPACITY_BYTES = (LINK_CAPACITY_BYTES_S * (bit<64>)CONG_WINDOW_US) / 1000000; // 250 000 B
const bit<64> BYTES_TH_MAX          = (WINDOW_CAPACITY_BYTES * 80) / 100; // 80% (200 000 B)
const bit<64> BYTES_TH_MIN          = (WINDOW_CAPACITY_BYTES * 30) / 100; // 30% (75 000 B)

struct congestion_digest_t {
    bit<9> port;
    bit<8> status;
}

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

    register<bit<64>>(16) reg_port_bytes;
    register<bit<48>>(16) reg_port_last_ts;
    register<bit<1>>(16)  reg_port_congested;

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

    action check_silent_port(in bit<32> port_id, inout bool digest_sent) {
        if (!digest_sent && standard_metadata.egress_spec != (bit<9>)port_id) {
            bit<48> last_ts;
            bit<1>  is_cong;
            reg_port_last_ts.read(last_ts, port_id);
            reg_port_congested.read(is_cong, port_id);
            bit<48> now = standard_metadata.ingress_global_timestamp;
            if (is_cong == 1 && (now - last_ts) > (CONG_WINDOW_US * 2)) {
                reg_port_congested.write(port_id, 0);
                reg_port_bytes.write(port_id, 0);
                reg_port_last_ts.write(port_id, now);
                digest<congestion_digest_t>(1, { (bit<9>)port_id, 0 });
                digest_sent = true;
            }
        }
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

        if (hdr.ipv4.ttl <= 1) {
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

        bool lpm_hit = false;
        if (meta.fft_hit == 0) {
            lpm_hit = ipv4_lpm.apply().hit;
        }

        if (meta.cache_fft == 1 && lpm_hit && standard_metadata.egress_spec != 511) {
            reg_signature.write((bit<32>)meta.fft_index,   meta.flow_signature);
            reg_egress_port.write((bit<32>)meta.fft_index, standard_metadata.egress_spec);
            reg_saved_ttl.write((bit<32>)meta.fft_index,   hdr.ipv4.ttl);
            reg_timestamp.write((bit<32>)meta.fft_index,
                                standard_metadata.ingress_global_timestamp);
        }

        dmac_table.apply();

        if (standard_metadata.egress_spec < 16) {
            bit<32> port_idx = (bit<32>)standard_metadata.egress_spec;
            bit<64> accumulated_bytes;
            bit<48> last_ts;
            bit<1>  current_congested;
            bool    digest_sent = false;

            reg_port_bytes.read(accumulated_bytes, port_idx);
            reg_port_last_ts.read(last_ts, port_idx);
            reg_port_congested.read(current_congested, port_idx);

            bit<48> now = standard_metadata.ingress_global_timestamp;
            bit<48> dt  = now - last_ts;

            if (dt > (CONG_WINDOW_US * 2)) {
                if (current_congested == 1) {
                    reg_port_congested.write(port_idx, 0);
                    digest<congestion_digest_t>(1, { standard_metadata.egress_spec, 0 });
                    digest_sent = true;
                }
                reg_port_bytes.write(port_idx, (bit<64>)standard_metadata.packet_length);
                reg_port_last_ts.write(port_idx, now);
            } else if (dt > CONG_WINDOW_US) {
                accumulated_bytes = accumulated_bytes + (bit<64>)standard_metadata.packet_length;
                if (accumulated_bytes > BYTES_TH_MAX && current_congested == 0) {
                    reg_port_congested.write(port_idx, 1);
                    digest<congestion_digest_t>(1, { standard_metadata.egress_spec, 1 });
                    digest_sent = true;
                } else if (accumulated_bytes < BYTES_TH_MIN && current_congested == 1) {
                    reg_port_congested.write(port_idx, 0);
                    digest<congestion_digest_t>(1, { standard_metadata.egress_spec, 0 });
                    digest_sent = true;
                }
                reg_port_bytes.write(port_idx, 0);
                reg_port_last_ts.write(port_idx, now);
            } else {
                accumulated_bytes = accumulated_bytes + (bit<64>)standard_metadata.packet_length;
                reg_port_bytes.write(port_idx, accumulated_bytes);
            }

            check_silent_port(1, digest_sent);
            check_silent_port(2, digest_sent);
            check_silent_port(3, digest_sent);
        }

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