# Hadoop Agent

A Python CLI agent that installs, configures, verifies, stops and removes **Apache Hadoop 3.3.6** (HDFS + YARN), either on a single local node or across a cluster of machines over SSH.

## Features (`agent/main.py`)

- Interactive menu: **Install**, **Verify**, **Delete**, with a choice of **Local node** or **Cluster**
- Downloads Hadoop 3.3.6 from `archive.apache.org` and extracts it with Python's safe `data` tar filter into `~/Downloads/hadoop/`
- Generates the configuration files: `core-site.xml`, `hdfs-site.xml`, `mapred-site.xml`, `yarn-site.xml`, `workers`, and sets `JAVA_HOME` in `hadoop-env.sh` (detected from the `java` on PATH)
- **Safe NameNode formatting**: formats only a brand-new cluster and only after you type `FORMAT`; refuses if HDFS metadata is missing but storage is not empty
- Starts HDFS and YARN (`start-dfs.sh`, `start-yarn.sh`)
- **Verify**: `hadoop version`, `jps` process check for NameNode, DataNode, ResourceManager and NodeManager, and a check of the web ports 9870 and 8088
- **Delete**: stops the services, then removes either the software only or the software plus HDFS data, after you type `DELETE`; refuses to delete while Hadoop daemons are still running
- **Cluster mode**: uses the system `ssh` and `scp` with key-based authentication (`BatchMode`), copies the agent to every node, prepares each node, then starts the cluster from the first node. Hostnames and usernames are validated before use.

In cluster mode the first node you enter is the master, and every node you enter (including the master) is listed in `workers`. HDFS replication is set to the number of nodes.

## Requirements

- Linux (developed and run on an Ubuntu virtual machine)
- Python 3.12+ (the safe archive extraction needs the `tarfile` data filter)
- A JDK installed, with `java` on PATH
- Internet access to download Hadoop
- Cluster mode: `ssh` and `scp`, and passwordless (key-based) SSH between the nodes. Hadoop's own start scripts need it as well.

`agent/main.py` uses only the Python standard library.

## Usage

```bash
git clone https://github.com/Aswinak8866/Hadoop-agent.git
cd Hadoop-agent
python3 agent/main.py
```

```
========================================
             HADOOP AGENT
========================================
1. Install Hadoop
2. Verify Hadoop
3. Delete Hadoop
4. Exit
```

After choosing an action, pick `1` for the local node or `2` for a cluster. Once Hadoop is running:

| Service | URL |
|---|---|
| HDFS NameNode UI | `http://<master>:9870` |
| YARN ResourceManager UI | `http://<master>:8088` |

Hadoop is installed under `~/Downloads/hadoop/hadoop-3.3.6`, and HDFS data under `~/Downloads/hadoop/data`.
On cluster nodes the same layout is created under the home directory of the SSH user (`~/Downloads/hadoop`).

## Earlier pipeline (`agent/phases`, `agent/rules`, `agent/utils`)

An earlier, multi-phase implementation is kept in the repo for reference. It is **not called** by the current `main.py`.

| Phase | What it does |
|---|---|
| 0 | Builds Docker containers (Ubuntu 22.04, one master and N workers) on a private network |
| 1 | Collects master and worker IPs and SSH credentials |
| 2 | Checks and installs Java and Python on every node |
| 3 | Ensures SSH is running, generates and exchanges SSH keys, updates `/etc/hosts`, tests master to worker SSH |
| 4 | Downloads Hadoop 3.4.2 and installs it on all nodes |
| 5 and 6 | Writes the master and worker configuration files |
| 7 | Formats the NameNode, starts the cluster and verifies the daemons |

It uses `paramiko` for SSH, `rich` for console output, and a small rule engine (`rule_engine.py`) with automatic fixes for missing Java, Python and SSH. Install its dependencies with `pip install -r requirements.txt`. Phase 0 creates throwaway lab containers with a demo password, so do not use them in production.

## Project structure

```
Hadoop-agent/
├── requirements.txt
└── agent/
    ├── main.py              # current agent (Hadoop 3.3.6)
    ├── phases/              # earlier pipeline (phase0 - phase7)
    ├── rules/rule_engine.py
    └── utils/               # logger.py, ssh_client.py
```
