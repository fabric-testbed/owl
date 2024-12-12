# OWL(One Way Latency)

Program for measuring one-way latency between nodes. Though it is written 
specifically for FABRIC Testbed, it should work in a general setting with 
minimal edits, if at all.

As outlined below, it can be used either within the Measurement Framework
environment or as a stand-alone application possibly running inside a Docker
contaier.

Under all circumstances, the sender and receiver nodes must have PTP (Precision
time Protocol) service running. To verify this and to look up the PTP clock path, 
run the following command:

```
ps -ef | grep phc2sys
```

####  Tested Python version
3.11

## How to Collect OWL data

### Prerequisites

- PTP (Precision Time Protocol) service
- Docker daemon for running the Docker version
- Directory on the host machine for owl output files (\*.pcap)

### 1. Using the Docker version (recommended)

```
docker pull fabrictestbed/owl
```

```
# sender side
$sudo docker run [--rm] -d \
--network="host"  \
--pid="host" \
--privileged \
fabrictestbed/owl:latest  sock_ops/udp_sender.py [options]

# receiver 
$sudo docker run [--rm] -d \
--mount type=bind,source=<path/to/local/output/dir>,target=/owl_output \
--network="host"  \
--pid="host"
--privileged \
fabrictestbed/owl:latest  sock_ops/udp_capturer.py [options]
```

##### Examples

```
# On Sender Node 

sudo docker run -d \
--network="host"  \
--pid="host" \
--privileged \
fabrictestbed/owl:latest  sock_ops/owl_sender.py  \
--dest-ip "10.0.0.2" 
--frequency 0.1 \
--seq-n 5452 \
--duration 60

# On Receiver Node

sudo docker run -d \
--mount type=bind,source=/tmp/owl/,target=/owl_output \
--network="host"  \
--pid="host" \
--privileged \
fabrictestbed/owl:latest  sock_ops/owl_capturer.py \
--ip "10.0.0.2" \
--port 5005 \
--outfile /owl_output/owl.pcap \
--duration 60
```


### 2. Natively using a virtual environment

#### Prerequisites
- PTP (Precision Time Protocol) service 
- tcpdump
- gcc
- scapy (`pip install --pre scapy[basic]`)
- psutil (`pip install psutil`)
- `ptp_time.so` file placed in the same directory as `ptp_time.c`

In addition, Python scripts must be run with `sudo` privilege to perform 
necessary socket operations.

#### Usage

The simplest experiment can be performed with 

```
# clone the repo and navigate to owl
$ git clone https://github.com/fabric-testbed/owl/tree/main
$ cd owl

# create a shared object file from ptp_time.c
$ gcc -fPIC -shared -o owl/sock_ops/time_ops/ptp_time.so owl/sock_ops/time_ops/ptp_time.c

# (recommended) create and activate a virtual environment. Install libraries
$ python3.11 -m venv .venv
$ source .venv/bin/activate
$ pip install -r requirements.txt

# Run the sender 
sudo python owl/sock_ops/owl_sender.py [options]


# Run the receiver
sudo python owl/sock_ops/owl_capturer.py [options]

```


## How to view live OWL data using InfluxDB

### Prerequisites
- InfluxDB server (either cloud or local instance)
- DB information (url, org, token, bucket)

`send_data.py` reads the pcap file, converts it to ASCII, extract the relevant 
information for one-way latency measurements, and send it to the InfluxDB server.

Once stored on InfluxDB, data can be downloaded in several different formats, 
including csv.


### Usage
On the receiver node, while `owl_capturer.py` is collecting data (or afterwards)
run `send_data.py` as follows:


#### Docker version

```
docker pull fabrictestbed/owl

sudo docker run -d \
--mount type=bind,source=/tmp/owl/,target=/owl_output \
--network="host"
--pid="host" \
--privileged \
fabricteestbed/owl  data_ops/send_data.py \
--pcapfile <file>.pcap \
[--verbose] \
--token "<InfluxDB API token>" \
--org "<InfluxDB org>" \
--url "<InfluxDB url>" \
--desttype "<cloud or local>" \
--bucket "<InfluxDB bucket>"
```

#### Natively

```
python data_ops/send_data.py [--verbose] 
	--pcapfile <file>.pcap 
	--token "<InfluxDB API token>" 
	--org "<InfluxDB org>"
	--url "<InfluxDB url>"
	--desttype "<cloud or local>"
	--bucket "<InfluxDB bucket>"
```


## Current Limitations
- IPV4 only
- Assumes hosts are (non-routing) endpoints.


