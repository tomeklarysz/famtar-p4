# FAMTAR Implementation in P4

This repository contains the implementation of the Flow-Aware Multi-Topology Adaptive Routing (FAMTAR) mechanism using the P4 programming language and a dedicated SDN controller written in Python.

The project relies on an SDN architecture, separating the data plane (P4/BMv2) from the control plane (Python/P4Runtime). Network topologies and end hosts are emulated using Containerlab.

## System Requirements
* **Docker** (>= 24.0.0)
* **Containerlab** (>= 0.50.0)
* **Python 3** (>= 3.10) with `venv` module

## Container Images
The project uses containerized network nodes. The required images are pulled automatically during deployment:
* `p4lang/p4c` – Official P4 compiler.
* `p4lang/behavioral-model` – BMv2 software switch executing the P4 pipeline.
* `frrouting/frr` – Routing daemon (IS-IS).
* Standard Linux/Alpine images used for testing end hosts.

## Quick Start
1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. load topology defined in `topology.clab.yaml` Containerlab

    ```
    containerlab deploy --topo topology.clab.yaml
    ```