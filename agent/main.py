#!/usr/bin/env python3
import argparse
import hashlib
import os
import re
import shutil
import socket
import sys
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree

try:
    import hadoop_rules as rules
except ImportError:  # started as a package
    from agent import hadoop_rules as rules

VERSION = "3.3.6"
ARCHIVE = f"hadoop-{VERSION}.tar.gz"
URL = f"https://archive.apache.org/dist/hadoop/common/hadoop-{VERSION}/{ARCHIVE}"

HOME = Path.home()
BASE = HOME / "Downloads" / "hadoop"
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
ROLES_ALL = ("NameNode", "DataNode", "ResourceManager", "NodeManager")
ROLES_WORKER = ("DataNode", "NodeManager")
DAEMON_CMD = {
    "NameNode": ("hdfs", "namenode"),
    "DataNode": ("hdfs", "datanode"),
    "ResourceManager": ("yarn", "resourcemanager"),
    "NodeManager": ("yarn", "nodemanager"),
}

execute = rules.run
guarded = rules.guarded


def hadoop_env():
    """Clean environment so an old HADOOP_HOME in the shell cannot interfere."""
    env = dict(os.environ)
    for key in ("HADOOP_HOME", "HADOOP_COMMON_HOME", "HADOOP_HDFS_HOME",
                "HADOOP_MAPRED_HOME", "HADOOP_YARN_HOME", "YARN_HOME",
                "HADOOP_LIBEXEC_DIR", "HADOOP_CONF_DIR", "HADOOP_INSTALL"):
        env.pop(key, None)
    env["HADOOP_HOME"] = str(HADOOP)
    env["HADOOP_CONF_DIR"] = str(HADOOP / "etc" / "hadoop")
    env["PDSH_RCMD_TYPE"] = "ssh"
    env["PATH"] = f"{HADOOP / 'bin'}:{HADOOP / 'sbin'}:" + env.get("PATH", "")
    try:
        env["JAVA_HOME"] = rules.find_java_home()
    except RuntimeError:
        pass
    return env


rules.CTX.update(base=BASE, hadoop=HADOOP, conf=HADOOP / "etc" / "hadoop",
                 tarball=TARBALL, datanode_dir=DN, env=hadoop_env)


def ask_yes_no(question):
    return input(f"{question} [y/n]: ").strip().lower() in ("y", "yes")


# ------------------------------------------------------------ pre-checks
def check_disk(min_gb=3):
    free = shutil.disk_usage(HOME).free
    if free < min_gb * 1024 ** 3:
        raise RuntimeError(f"Not enough disk space: {free / 1024 ** 3:.1f} GB free, "
                           f"{min_gb} GB needed.")


def preflight():
    guarded("disk space check", check_disk)
    guarded("Java check", rules.find_java_home)


# --------------------------------------------------------- download/install
def expected_sha512():
    try:
        with urllib.request.urlopen(URL + ".sha512", timeout=30) as resp:
            found = re.findall(r"\b[0-9a-fA-F]{128}\b", resp.read().decode(errors="replace"))
        return found[-1].lower() if found else None
    except Exception:
        return None


def sha512_of(path):
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _progress(blocks, size, total):
    if total > 0:
        pct = min(100, blocks * size * 100 // total)
        if pct % 10 == 0 and pct != _progress.last:
            _progress.last = pct
            print(f"  downloaded {pct}%", flush=True)


_progress.last = -1


def download():
    CACHE.mkdir(parents=True, exist_ok=True)
    want = expected_sha512()
    if TARBALL.exists():
        good = (sha512_of(TARBALL) == want) if want else TARBALL.stat().st_size > 500_000_000
        if good:
            print("Using the cached Hadoop archive.")
            return
        TARBALL.unlink()
    part = Path(str(TARBALL) + ".part")
    socket.setdefaulttimeout(60)
    last = None
    for attempt in range(1, 4):
        _progress.last = -1
        try:
            print(f"Downloading Hadoop {VERSION} (attempt {attempt}/3)...")
            urllib.request.urlretrieve(URL, part, reporthook=_progress)
        except Exception as exc:
            last = exc
            if isinstance(exc, OSError) and exc.errno == 28:
                raise
            time.sleep(5 * attempt)
            continue
        if want and sha512_of(part) != want:
            part.unlink(missing_ok=True)
            last = "Checksum mismatch"
            continue
        part.replace(TARBALL)
        return
    raise RuntimeError(f"Download failed: {last}")


def _download_and_extract():
    download()
    BASE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=BASE) as temp:
        if hasattr(tarfile, "data_filter"):
            with tarfile.open(TARBALL, "r:gz") as tf:
                tf.extractall(temp, filter="data")
        else:
            execute(["tar", "-xzf", TARBALL, "-C", temp], timeout=600, capture=True)
        extracted = Path(temp) / f"hadoop-{VERSION}"
        if not (extracted / "bin" / "hadoop").exists():
            raise RuntimeError("Downloaded archive is invalid.")
        extracted.rename(HADOOP)


def install_software():
    if (HADOOP / "bin" / "hadoop").exists():
        print(f"Hadoop already installed: {HADOOP}")
        return
    if HADOOP.exists():
        raise RuntimeError(f"Invalid installation directory: {HADOOP}. Inspect it manually.")
    guarded("download and extract Hadoop", _download_and_extract)
    print("Hadoop software installed.")


def write_xml(path, properties):
    root = Element("configuration")
    for key, value in properties.items():
        prop = SubElement(root, "property")
        SubElement(prop, "name").text = key
        SubElement(prop, "value").text = str(value)
    ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)


def configure(master, workers):
    conf = HADOOP / "etc" / "hadoop"
    for folder in (conf, DATA, NN, DN, DATA / "tmp"):
        folder.mkdir(parents=True, exist_ok=True)

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
        "mapreduce.application.classpath":
            f"{HADOOP}/share/hadoop/mapreduce/*:{HADOOP}/share/hadoop/mapreduce/lib/*",
        "yarn.app.mapreduce.am.env": f"HADOOP_MAPRED_HOME={HADOOP}",
        "mapreduce.map.env": f"HADOOP_MAPRED_HOME={HADOOP}",
        "mapreduce.reduce.env": f"HADOOP_MAPRED_HOME={HADOOP}",
    })
    write_xml(conf / "yarn-site.xml", {
        "yarn.resourcemanager.hostname": master,
        "yarn.resourcemanager.webapp.address": f"{master}:8088",
        "yarn.nodemanager.aux-services": "mapreduce_shuffle",
        "yarn.nodemanager.env-whitelist":
            "JAVA_HOME,HADOOP_COMMON_HOME,HADOOP_HDFS_HOME,HADOOP_CONF_DIR,"
            "CLASSPATH_PREPEND_DISTCACHE,HADOOP_YARN_HOME,HADOOP_HOME,HADOOP_MAPRED_HOME",
    })
    (conf / "workers").write_text("".join(f"{host}\n" for host in workers))

    rules.set_hadoop_env("JAVA_HOME", rules.find_java_home())
    rules.set_hadoop_env("PDSH_RCMD_TYPE", "ssh")
    if os.geteuid() == 0:
        rules.fix_root_user("")
    print(f"Configuration written. Master: {master}")


def run_hadoop(*args, timeout=120):
    return execute([HADOOP / "bin" / "hadoop", *args], timeout=timeout, env=hadoop_env())


# ------------------------------------------------------------- start/verify
def ssh_check(host):
    return execute(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                    "-o", "ConnectTimeout=8", host, "true"],
                   check=False, capture=True, timeout=30)


def ensure_ssh_ready(workers):
    """Hadoop's start scripts need passwordless ssh to every worker and this host."""
    for host in dict.fromkeys([*workers, socket.gethostname()]):
        for attempt in (1, 2):
            result = ssh_check(host)
            if result.returncode == 0:
                break
            problem = f"Passwordless SSH to {host} failed:\n{result.stdout.strip()}"
            rule = rules.match(result.stdout)
            if rule is None:
                raise RuntimeError(problem)
            rules.show(rule, f"SSH check for {host}")
            if attempt == 2 or not rules.apply(rule, result.stdout):
                raise RuntimeError(problem)


def format_if_new(confirm_format=False):
    if (NN / "current" / "VERSION").exists():
        return
    if any(NN.iterdir()) or any(DN.iterdir()):
        raise RuntimeError("HDFS metadata is missing but storage is not empty. "
                           "Refusing to format.")
    allowed = confirm_format
    if not allowed:
        if not sys.stdin.isatty():
            raise RuntimeError("New HDFS cluster needs formatting: run again with "
                               "--confirm-format.")
        allowed = input("New HDFS cluster. Type FORMAT to format NameNode: ").strip() == "FORMAT"
    if not allowed:
        raise RuntimeError("Formatting cancelled.")
    run_hadoop("namenode", "-format", "-nonInteractive", timeout=180)


def _run_script(name):
    result = execute([HADOOP / "sbin" / name], timeout=180, check=False, env=hadoop_env())
    if result.returncode != 0 or rules.match(result.stdout):
        raise RuntimeError(f"{name} reported errors:\n{result.stdout}")


def _start_scripts():
    for name in ("start-dfs.sh", "start-yarn.sh"):
        guarded(name, lambda n=name: _run_script(n))
        time.sleep(5)


def jps_output():
    try:
        jps = str(Path(rules.find_java_home()) / "bin" / "jps")
    except RuntimeError:
        jps = "jps"
    return execute([jps, "-l"], capture=True, check=False, timeout=30).stdout or ""


def missing_daemons(expected=ROLES_ALL):
    out = jps_output()
    return [name for name in expected if PROCESSES[name] not in out]


def restart_daemons(missing):
    for name in missing:
        tool, daemon = DAEMON_CMD[name]
        execute([HADOOP / "bin" / tool, "--daemon", "start", daemon],
                check=False, env=hadoop_env(), timeout=120)
    time.sleep(8)


def daemon_log_text(names):
    chunks = []
    for name in names:
        for pattern in (f"*{name.lower()}*.log", f"*{name.lower()}*.out"):
            files = sorted((HADOOP / "logs").glob(pattern), key=lambda p: p.stat().st_mtime)
            if files:
                tail = files[-1].read_text(errors="replace").splitlines()[-80:]
                chunks.append("\n".join(tail))
    return "\n".join(chunks)


def diagnose_after_start(expected=ROLES_ALL):
    """If a daemon is missing, read its log, match a rule, fix, restart it."""
    for attempt in (1, 2):
        missing = missing_daemons(expected)
        if not missing:
            print("All expected Hadoop daemons are running.")
            return True
        print(f"Not running: {', '.join(missing)}")
        text = daemon_log_text(missing)
        rule = rules.match(text)
        if rule is None:
            print(f"No known rule matches. Check the logs in {HADOOP / 'logs'}")
            return False
        rules.show(rule, "daemon start-up")
        if attempt == 2 or not rules.apply(rule, text):
            return False
        print("Fix applied. Restarting the missing daemons...")
        restart_daemons(missing)
    return False


def start_services(master, workers, confirm_format=False):
    ensure_ssh_ready(workers)
    format_if_new(confirm_format)
    _start_scripts()
    diagnose_after_start(ROLES_ALL)
    verify_local()


def verify_local():
    output = jps_output()
    print("\nJava processes:")
    print(output.strip() or "No Java processes found.")
    for name, class_name in PROCESSES.items():
        print(f"{name}: {'RUNNING' if class_name in output else 'NOT RUNNING'}")
    print("\nLocal web port status:")
    for port in (9870, 8088):
        with socket.socket() as sock:
            sock.settimeout(1)
            status = "OPEN" if sock.connect_ex(("127.0.0.1", port)) == 0 else "CLOSED"
        print(f"Port {port}: {status}")


def hdfs_report(expected):
    live = 0
    for _ in range(8):
        result = execute([HADOOP / "bin" / "hdfs", "dfsadmin", "-report"], check=False,
                         capture=True, env=hadoop_env(), timeout=60)
        found = re.search(r"Live datanodes \((\d+)\)", result.stdout)
        live = int(found.group(1)) if found else 0
        if live >= expected:
            break
        time.sleep(5)
    print(f"HDFS report: {live}/{expected} DataNodes are live.")
    print(f"HDFS_LIVE={live}")


def stop_local():
    execute([HADOOP / "sbin" / "stop-yarn.sh"], check=False, env=hadoop_env())
    execute([HADOOP / "sbin" / "stop-dfs.sh"], check=False, env=hadoop_env())
    time.sleep(2)
    verify_local()


def delete_local(mode=None):
    print(f"Hadoop path: {HADOOP}")
    print(f"HDFS data:   {DATA}")
    if mode is None:
        print("1. Remove software only")
        print("2. Remove software and HDFS data")
        print("3. Cancel")
        mode = {"1": "software", "2": "all"}.get(input("Choose: ").strip())
        if mode is None or input("Type DELETE to confirm: ").strip() != "DELETE":
            print("Cancelled.")
            return
    if (HADOOP / "sbin" / "stop-yarn.sh").exists():
        stop_local()
    if any(cls in jps_output() for cls in PROCESSES.values()):
        raise RuntimeError("Hadoop processes remain; refusing deletion.")
    if HADOOP.exists():
        shutil.rmtree(HADOOP)
    if mode == "all" and DATA.exists():
        if DATA.resolve() != (BASE / "data").resolve():
            raise RuntimeError("Unexpected data path; refusing deletion.")
        shutil.rmtree(DATA)
    print("Delete operation completed.")


# --------------------------------------------------------------- cluster
def valid_host(host):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host))


def ssh(host, username, *remote_command, timeout=30, check=True, capture=False, prefix=None):
    if not valid_host(host) or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise RuntimeError("Invalid hostname/IP or SSH username.")
    return execute(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    "-o", "ServerAliveInterval=30", f"{username}@{host}", *remote_command],
                   timeout=timeout, check=check, capture=capture, prefix=prefix)


def remote_worker(host, username, action, master, workers, extra=(), prefix=None):
    """Copy this agent + its rule engine to a node and run one action there."""
    target = f"{username}@{host}"
    here = Path(__file__).resolve()
    scp = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    execute([*scp, here, f"{target}:/tmp/hadoop_agent.py"], timeout=60, capture=True)
    execute([*scp, here.with_name("hadoop_rules.py"), f"{target}:/tmp/hadoop_rules.py"],
            timeout=60, capture=True)
    args = ["python3", "/tmp/hadoop_agent.py", "--worker", action,
            "--master", master, "--workers", ",".join(workers), *extra]
    return ssh(host, username, *args, timeout=3600, prefix=prefix)


def run_parallel(hosts, job):
    results = {}
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = {pool.submit(job, host): host for host in hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                future.result()
                results[host] = "OK"
            except Exception as exc:
                tail = [x for x in getattr(exc, "output", "").splitlines() if x.strip()]
                results[host] = "FAILED: " + (tail[-1] if tail else str(exc))
    return results


def setup_cluster_ssh(username, master, nodes):
    """Let the master log in to every node (and itself) without a password."""
    gen = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
           "{ [ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N '' "
           "-f ~/.ssh/id_ed25519 -q; } && cat ~/.ssh/id_ed25519.pub")
    key = ssh(master, username, gen, capture=True, timeout=60).stdout.strip().splitlines()[-1]
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+( [\w.@-]+)?", key):
        raise RuntimeError("Could not read the master's public key.")
    add = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
           f"chmod 600 ~/.ssh/authorized_keys && (grep -qxF '{key}' ~/.ssh/authorized_keys "
           f"|| echo '{key}' >> ~/.ssh/authorized_keys)")
    for host in nodes:
        ssh(host, username, add, capture=True, timeout=60)
        test = (f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                f"-o ConnectTimeout=8 {host} true")
        ssh(master, username, test, capture=True, timeout=60)
        print(f"  master -> {host}: passwordless SSH OK")


def cluster_install(username, master, nodes):
    auto = ask_yes_no("Let the agent auto-fix known errors on all nodes?")
    print("If this is a NEW cluster the NameNode is formatted (HDFS data is erased).")
    fmt = input("Type FORMAT to allow formatting, or press Enter to skip: ").strip() == "FORMAT"
    extra = ["--auto-fix"] if auto else []

    print(f"\nPreparing {len(nodes)} node(s) in parallel...")
    results = run_parallel(nodes, lambda h: remote_worker(
        h, username, "prepare", master, nodes, extra, prefix=h))
    print("\nNode preparation summary:")
    for host in nodes:
        print(f"  {host}: {results[host]}")
    if any(r != "OK" for r in results.values()):
        print("Fix the failed node(s) (see the RULE messages above) and run again.")
        return

    print("\nSetting up passwordless SSH from the master...")
    setup_cluster_ssh(username, master, nodes)

    print("\nStarting HDFS and YARN from the master...")
    remote_worker(master, username, "start", master, nodes,
                  extra + (["--confirm-format"] if fmt else []), prefix=master)

    def live_count():
        out = remote_worker(master, username, "report", master, nodes, prefix=master).stdout
        found = re.search(r"HDFS_LIVE=(\d+)", out)
        return int(found.group(1)) if found else 0

    live = live_count()
    if live < len(nodes):
        print(f"\nOnly {live}/{len(nodes)} DataNodes are live. Diagnosing the nodes...")
        run_parallel(nodes, lambda h: remote_worker(
            h, username, "diagnose", master, nodes, extra, prefix=h))
        live = live_count()
    print(f"\nCluster result: {live}/{len(nodes)} DataNodes live.")
    print(f"HDFS UI : http://{master}:9870")
    print(f"YARN UI : http://{master}:8088")


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
    reachable = []
    for host in nodes:
        try:
            ssh(host, username, "echo", "SSH_OK", timeout=15, capture=True)
            print(f"{host}: SSH CONNECTED")
            reachable.append(host)
        except Exception as exc:
            print(f"{host}: SSH FAILED - {exc}")
    if len(reachable) != len(nodes):
        print("Operation cancelled: not all cluster nodes are reachable.")
        return

    if action == "install":
        cluster_install(username, master, nodes)
    elif action == "verify":
        for host in nodes:
            remote_worker(host, username, "verify", master, nodes, prefix=host)
    elif action == "delete":
        print("1. Remove software only\n2. Remove software and HDFS data\n3. Cancel")
        mode = {"1": "software", "2": "all"}.get(input("Choose: ").strip())
        if mode is None or input("Type DELETE to confirm: ").strip() != "DELETE":
            print("Cancelled.")
            return
        remote_worker(master, username, "stop", master, nodes, prefix=master)
        for host in nodes:
            remote_worker(host, username, "delete", master, nodes,
                          ["--delete-mode", mode], prefix=host)


# ----------------------------------------------------------------- entry
def worker(action, master, workers, confirm_format=False, delete_mode=None):
    if action == "prepare":
        preflight()
        install_software()
        configure(master, workers)
        run_hadoop("version")
        if master not in rules.local_names() or len(workers) > 1:
            rules.open_firewall()
    elif action == "install":
        preflight()
        install_software()
        configure(master, workers)
        run_hadoop("version")
        start_services(master, workers, confirm_format)
    elif action == "start":
        start_services(master, workers, confirm_format)
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
            delete_local(delete_mode)
        else:
            print("Hadoop software is not installed here.")
    elif action == "ports":
        verify_local()
    elif action == "report":
        hdfs_report(len(workers))
    elif action == "diagnose":
        is_master = master in rules.local_names()
        diagnose_after_start(ROLES_ALL if is_master else ROLES_WORKER)


def report_error(exc):
    print(f"\nERROR: {str(exc).strip()}")
    rule = rules.match(rules.error_text(exc))
    if rule:
        rules.show(rule, "error analysis")
        rules.manual_help(rule)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", choices=["prepare", "install", "start", "verify", "stop",
                                              "delete", "ports", "report", "diagnose"])
    parser.add_argument("--master", default="localhost")
    parser.add_argument("--workers", default="localhost")
    parser.add_argument("--auto-fix", action="store_true")
    parser.add_argument("--confirm-format", action="store_true")
    parser.add_argument("--delete-mode", choices=["software", "all"])
    args = parser.parse_args()

    if args.worker:
        rules.CONFIG["auto"] = args.auto_fix
        try:
            worker(args.worker, args.master, args.workers.split(","),
                   args.confirm_format, args.delete_mode)
        except Exception as exc:
            report_error(exc)
            print(f"FAILED: {str(exc).strip().splitlines()[0]}", file=sys.stderr)
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
                    rules.CONFIG["auto"] = ask_yes_no(
                        "Auto-apply fixes for known errors without asking each time?")
                    worker("install", "localhost", ["localhost"])
                elif action == "verify":
                    worker("verify", "localhost", ["localhost"])
                else:
                    delete_local()
            elif target == "2":
                cluster_operation(action)
            else:
                print("Invalid target.")
        except Exception as exc:
            report_error(exc)

        input("\nPress Enter to continue...")


if __name__ == "__main__":
    main()
