
#!/usr/bin/env python3
import os
import re
import sys
import time
import shutil
import socket
import subprocess
import urllib.request
import tarfile
import tempfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree

VERSION = "3.3.6"
ARCHIVE = f"hadoop-{VERSION}.tar.gz"
URL = f"https://archive.apache.org/dist/hadoop/common/hadoop-{VERSION}/{ARCHIVE}"

HOME = Path.home()
BASE = HOME / "hadoopagent" / "installed"
HADOOP = BASE / f"hadoop-{VERSION}"
DATA = BASE / "data"
NN = DATA / "namenode"
DN = DATA / "datanode"
CACHE = HOME / ".hadoop-agent"
TARBALL = CACHE / ARCHIVE

PROCESSES = {
    "NameNode": "org.apache.hadoop.hdfs.server.namenode.NameNode",
    "DataNode": "org.apache.hadoop.hdfs.server.datanode.DataNode",
    "ResourceManager": "org.apache.hadoop.yarn.server.resourcemanager.ResourceManager",
    "NodeManager": "org.apache.hadoop.yarn.server.nodemanager.NodeManager",
}


def execute(cmd, timeout=180, check=True, capture=False):
    return subprocess.run(
        cmd, text=True, timeout=timeout, check=check, capture_output=capture
    )


def java_home():
    java = shutil.which("java")
    if not java:
        raise RuntimeError("Java is not installed. Install Java first.")
    return str(Path(java).resolve().parent.parent)


def download():
    CACHE.mkdir(parents=True, exist_ok=True)
    if TARBALL.exists() and TARBALL.stat().st_size > 1_000_000:
        return
    print("Downloading Hadoop...")
    urllib.request.urlretrieve(URL, TARBALL)


def install_software():
    if (HADOOP / "bin" / "hadoop").exists():
        print(f"Hadoop already installed: {HADOOP}")
        return

    if HADOOP.exists():
        raise RuntimeError(
            f"Invalid installation directory: {HADOOP}. Inspect it manually."
        )

    download()
    BASE.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp:
        # Hadoop's official archive contains legitimate symbolic links.
        # Python's data filter safely restricts extraction when supported.
        with tarfile.open(TARBALL, "r:gz") as tf:
            if not hasattr(tarfile, "data_filter"):
                raise RuntimeError(
                    "Use Python 3.12+ for safe archive extraction."
                )
            tf.extractall(temp, filter="data")

        extracted = Path(temp) / f"hadoop-{VERSION}"
        if not (extracted / "bin" / "hadoop").exists():
            raise RuntimeError("Downloaded archive is invalid.")

        shutil.copytree(extracted, HADOOP)

    print("Hadoop software installed.")


def write_xml(path, properties):
    root = Element("configuration")
    for key, value in properties.items():
        p = SubElement(root, "property")
        SubElement(p, "name").text = key
        SubElement(p, "value").text = str(value)
    ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)


def configure(master, workers):
    conf = HADOOP / "etc" / "hadoop"
    conf.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    NN.mkdir(parents=True, exist_ok=True)
    DN.mkdir(parents=True, exist_ok=True)

    write_xml(conf / "core-site.xml", {
        "fs.defaultFS": f"hdfs://{master}:9000",
        "hadoop.tmp.dir": str(DATA / "tmp"),
    })
    write_xml(conf / "hdfs-site.xml", {
        "dfs.replication": str(max(1, len(workers))),
        "dfs.namenode.name.dir": f"file://{NN}",
        "dfs.datanode.data.dir": f"file://{DN}",
        "dfs.namenode.http-address": f"{master}:9870",
    })
    write_xml(conf / "mapred-site.xml", {
        "mapreduce.framework.name": "yarn",
    })
    write_xml(conf / "yarn-site.xml", {
        "yarn.resourcemanager.hostname": master,
        "yarn.resourcemanager.webapp.address": f"{master}:8088",
        "yarn.nodemanager.aux-services": "mapreduce_shuffle",
    })

    (conf / "workers").write_text(
        "".join(f"{host}\n" for host in workers)
    )

    envfile = conf / "hadoop-env.sh"
    lines = envfile.read_text().splitlines() if envfile.exists() else []
    lines = [x for x in lines if not x.strip().startswith("export JAVA_HOME=")]
    lines.append(f'export JAVA_HOME="{java_home()}"')
    envfile.write_text("\n".join(lines) + "\n")

    (DATA / "tmp").mkdir(parents=True, exist_ok=True)
    print(f"Configuration written. Master: {master}")


def run_hadoop(*args, timeout=120):
    return execute([str(HADOOP / "bin" / "hadoop"), *args], timeout=timeout)


def start_services(master, workers):
    # Format only a brand-new NameNode with empty storage.
    if not (NN / "current" / "VERSION").exists():
        nn_files = list(NN.iterdir())
        dn_files = list(DN.iterdir())
        if nn_files or dn_files:
            raise RuntimeError(
                "HDFS metadata is missing but storage is not empty. "
                "Refusing to format."
            )
        if input("New HDFS cluster. Type FORMAT to format NameNode: ") != "FORMAT":
            raise RuntimeError("Formatting cancelled.")
        run_hadoop("namenode", "-format", "-nonInteractive", timeout=180)

    # The cluster must have passwordless SSH among configured worker nodes
    # for Hadoop's start/stop scripts to work.
    execute([str(HADOOP / "sbin" / "start-dfs.sh")], timeout=180)
    time.sleep(5)
    execute([str(HADOOP / "sbin" / "start-yarn.sh")], timeout=180)
    time.sleep(5)
    print("Service start commands completed.")
    verify_local()


def verify_local():
    result = execute(["jps", "-l"], capture=True, check=False)
    output = result.stdout or ""
    print("\nJava processes:")
    print(output.strip() or "No Java processes found.")

    for name, class_name in PROCESSES.items():
        state = "RUNNING" if class_name in output else "NOT RUNNING"
        print(f"{name}: {state}")

    print("\nLocal web port status:")
    for port in (9870, 8088):
        with socket.socket() as s:
            s.settimeout(1)
            status = "OPEN" if s.connect_ex(("127.0.0.1", port)) == 0 else "CLOSED"
        print(f"Port {port}: {status}")


def stop_local():
    execute([str(HADOOP / "sbin" / "stop-yarn.sh")], check=False)
    execute([str(HADOOP / "sbin" / "stop-dfs.sh")], check=False)
    time.sleep(2)
    verify_local()


def delete_local():
    print(f"Hadoop path: {HADOOP}")
    print(f"HDFS data:   {DATA}")
    print("1. Remove software only")
    print("2. Remove software and HDFS data")
    print("3. Cancel")
    choice = input("Choose: ").strip()

    if choice not in ("1", "2"):
        print("Cancelled.")
        return

    if input("Type DELETE to confirm: ").strip() != "DELETE":
        print("Cancelled.")
        return

    if (HADOOP / "sbin" / "stop-yarn.sh").exists():
        stop_local()

    output = execute(["jps", "-l"], capture=True, check=False).stdout or ""
    if any(cls in output for cls in PROCESSES.values()):
        raise RuntimeError("Hadoop processes remain; refusing deletion.")

    if HADOOP.exists():
        shutil.rmtree(HADOOP)
    if choice == "2" and DATA.exists():
        if DATA.resolve() != (HOME / "hadoopagent" / "installed" / "data").resolve():
            raise RuntimeError("Unexpected data path; refusing deletion.")
        shutil.rmtree(DATA)
    print("Delete operation completed.")


def valid_host(host):
    # Accept IPv4 or a simple hostname; reject shell metacharacters.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host))


def ssh(host, username, *remote_command, timeout=30, check=True, capture=False):
    if not valid_host(host) or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise RuntimeError("Invalid hostname/IP or SSH username.")
    return execute(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         f"{username}@{host}", *remote_command],
        timeout=timeout, check=check, capture=capture
    )


def remote_worker(host, username, action, master, workers):
    target = f"{username}@{host}"
    script = Path(__file__).resolve()
    remote_script = "/tmp/hadoop_agent.py"

    print(f"\nConnecting to {target}...")
    ssh(host, username, "echo", "SSH_OK")

    execute(["scp", str(script), f"{target}:{remote_script}"], timeout=60)

    # Send cluster settings as ordinary command arguments.
    args = [
        "python3", remote_script, "--worker", action,
        "--master", master, "--workers", ",".join(workers),
    ]
    ssh(host, username, *args, timeout=900)


def cluster_operation(action):
    try:
        count = int(input("How many cluster nodes? ").strip())
        if count < 1 or count > 20:
            raise ValueError
    except ValueError:
        print("Enter a node count from 1 to 20.")
        return

    username = input("SSH username for all nodes: ").strip()
    nodes = []
    for i in range(count):
        host = input(f"Node {i + 1} IP address/hostname: ").strip()
        if not valid_host(host):
            print(f"Invalid host: {host}")
            return
        if host in nodes:
            print("Duplicate node. Enter each node only once.")
            return
        nodes.append(host)

    master = nodes[0]
    workers = nodes

    # Verify SSH first, so unreachable nodes are reported before installation.
    connected = []
    for host in nodes:
        try:
            ssh(host, username, "echo", "SSH_OK", timeout=15)
            print(f"{host}: SSH CONNECTED")
            connected.append(host)
        except Exception as exc:
            print(f"{host}: SSH FAILED — {exc}")

    if len(connected) != len(nodes):
        print("Operation cancelled: not all cluster nodes are reachable.")
        return

    # Install/configure each node, then start from the master.
    for host in nodes:
        remote_worker(host, username, "prepare" if action == "install" else action,
                      master, workers)

    if action == "install":
        # Start the cluster scripts from the master node.
        remote_worker(master, username, "start", master, workers)


def worker(action, master, workers):
    if action == "prepare":
        install_software()
        configure(master, workers)
        run_hadoop("version")
    elif action == "install":
        install_software()
        configure(master, workers)
        start_services(master, workers)
    elif action == "start":
        start_services(master, workers)
    elif action == "verify":
        installed = (HADOOP / "bin" / "hadoop").exists()
        print("Hadoop installed:", "YES" if installed else "NO")
        if installed:
            run_hadoop("version")
            verify_local()
    elif action == "stop":
        if HADOOP.exists():
            stop_local()
        else:
            print("Hadoop is not installed here.")
    elif action == "delete":
        if HADOOP.exists():
            delete_local()
        else:
            print("Hadoop software is not installed here.")
    elif action == "ports":
        verify_local()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=["prepare", "install", "start",
                                              "verify", "stop", "delete", "ports"])
    parser.add_argument("--master", default="localhost")
    parser.add_argument("--workers", default="localhost")
    args = parser.parse_args()

    if args.worker:
        try:
            worker(args.worker, args.master, args.workers.split(","))
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    while True:
        print("\n========================================")
        print("             HADOOP AGENT")
        print("========================================")
        print("1. Install Hadoop")
        print("2. Verify Hadoop")
        print("3. Delete Hadoop")
        print("4. Exit")
        choice = input("Choose an option: ").strip()

        if choice == "4":
            print("Exiting.")
            break
        if choice not in ("1", "2", "3"):
            print("Invalid option.")
            continue

        action = {"1": "install", "2": "verify", "3": "delete"}[choice]
        target = input("1. Local node  2. Cluster: ").strip()

        try:
            if target == "1":
                if action == "install":
                    install_software()
                    configure("localhost", ["localhost"])
                    run_hadoop("version")
                    start_services("localhost", ["localhost"])
                elif action == "verify":
                    if not (HADOOP / "bin" / "hadoop").exists():
                        print("Hadoop is NOT installed on this node.")
                    else:
                        run_hadoop("version")
                        verify_local()
                else:
                    delete_local()
            elif target == "2":
                cluster_operation(action)
            else:
                print("Invalid target.")
        except Exception as exc:
            print(f"\nERROR: {exc}")

        input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
