import os
import subprocess
import time
from agent.utils.logger import phase_banner, info, success, error

DOCKERFILE = """
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -y && apt-get install -y \\
    openssh-server openjdk-11-jdk python3 python3-pip \\
    sudo wget curl nano net-tools iputils-ping && \\
    apt-get clean
RUN useradd -m -s /bin/bash vboxuser && \\
    echo "vboxuser:hadoop123" | chpasswd && \\
    usermod -aG sudo vboxuser && \\
    echo "vboxuser ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
RUN mkdir -p /var/run/sshd && \\
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config && \\
    sed -i 's/PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config
RUN echo "export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64" >> /etc/bash.bashrc
EXPOSE 22 9870 9000 8088 8032 8042 19888
CMD ["/usr/sbin/sshd", "-D"]
"""

def run_local(cmd, timeout=300):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()

def is_port_free(port):
    code, out, _ = run_local(f"sudo lsof -i :{port} 2>/dev/null | grep LISTEN")
    return out == ""

def free_port(port):
    run_local(f"sudo fuser -k {port}/tcp 2>/dev/null || true")
    time.sleep(1)

def run_phase0():
    phase_banner(0, "Auto-Create Docker Containers for Hadoop Cluster")

    # ── Ask how many workers ──────────────────────────────────────
    print()
    while True:
        try:
            worker_count = int(input("  How many Worker Nodes do you want? : ").strip())
            if worker_count > 0:
                break
            print("  Please enter a number greater than 0.")
        except ValueError:
            print("  Invalid input. Enter a number.")

    # ── Check Docker is running ───────────────────────────────────
    code, _, _ = run_local("docker info 2>/dev/null")
    if code != 0:
        error("Docker is not running.")
        error("Fix: sudo systemctl start docker")
        return False
    success("Docker is running")

    # ── Stop existing Hadoop if running (frees ports) ─────────────
    info("Stopping any existing Hadoop services to free ports...")
    run_local("/usr/local/hadoop-3.4.2/sbin/stop-all.sh 2>/dev/null || true")
    time.sleep(3)

    # ── Free required ports ───────────────────────────────────────
    info("Checking and freeing required ports...")
    required_ports = [9870, 8088, 9000, 8032, 19888]
    for port in required_ports:
        if not is_port_free(port):
            info(f"Port {port} in use — freeing it...")
            free_port(port)
            if not is_port_free(port):
                error(f"Port {port} still in use. Kill it manually: sudo fuser -k {port}/tcp")
                return False
        success(f"Port {port} is free")

    # ── Remove old containers ─────────────────────────────────────
    info("Removing old Hadoop containers if any...")
    all_names = ["hadoop-master"] + [f"hadoop-worker{i}" for i in range(1, worker_count + 1)]
    for name in all_names:
        run_local(f"docker rm -f {name} 2>/dev/null || true")

    # ── Remove old network ────────────────────────────────────────
    run_local("docker network rm hadoop-net 2>/dev/null || true")
    time.sleep(2)

    # ── Write Dockerfile ──────────────────────────────────────────
    os.makedirs("/tmp/hadoop-docker", exist_ok=True)
    with open("/tmp/hadoop-docker/Dockerfile", "w") as f:
        f.write(DOCKERFILE)
    info("Dockerfile written")

    # ── Build Docker image ────────────────────────────────────────
    info("Building Docker image hadoop-node:latest ... (first time takes 2-3 mins)")
    code, out, err = run_local(
        "docker build -t hadoop-node:latest /tmp/hadoop-docker/",
        timeout=600
    )
    if code != 0:
        error(f"Docker build failed: {err}")
        return False
    success("Docker image hadoop-node:latest built")

    # ── Create Docker network ─────────────────────────────────────
    code, _, err = run_local("docker network create --subnet=192.168.100.0/24 hadoop-net")
    if code != 0 and "already exists" not in err:
        error(f"Network create failed: {err}")
        return False
    success("Docker network hadoop-net ready (192.168.100.0/24)")

    # ── Start Master container ────────────────────────────────────
    master_ip = "192.168.100.10"
    info(f"Starting hadoop-master at {master_ip} ...")
    code, _, err = run_local(
        f"docker run -d "
        f"--name hadoop-master "
        f"--hostname hadoop-master "
        f"--network hadoop-net "
        f"--ip {master_ip} "
        f"-p 9870:9870 "
        f"-p 8088:8088 "
        f"-p 9000:9000 "
        f"-p 19888:19888 "
        f"hadoop-node:latest"
    )
    if code != 0:
        error(f"Failed to start hadoop-master: {err}")
        return False
    success(f"hadoop-master started → IP: {master_ip}")
    success(f"  HDFS UI → http://localhost:9870")
    success(f"  YARN UI → http://localhost:8088")

    # ── Start Worker containers dynamically ───────────────────────
    workers = []
    for i in range(1, worker_count + 1):
        worker_ip     = f"192.168.100.{10 + i}"
        worker_name   = f"hadoop-worker{i}"
        # Each worker gets unique host port for its NodeManager UI
        nm_host_port  = 8100 + i

        info(f"Starting {worker_name} at {worker_ip} ...")
        code, _, err = run_local(
            f"docker run -d "
            f"--name {worker_name} "
            f"--hostname {worker_name} "
            f"--network hadoop-net "
            f"--ip {worker_ip} "
            f"-p {nm_host_port}:8042 "
            f"hadoop-node:latest"
        )
        if code != 0:
            error(f"Failed to start {worker_name}: {err}")
            return False
        success(f"{worker_name} started → IP: {worker_ip}")
        success(f"  NodeManager UI → http://localhost:{nm_host_port}")

        workers.append({
            "id": i,
            "ip": worker_ip,
            "username": "vboxuser",
            "password": "hadoop123",
            "nm_port": nm_host_port
        })

    # ── Copy Hadoop tar into containers ───────────────────────────
    if os.path.exists("/tmp/hadoop-3.4.2.tar.gz"):
        info("Copying Hadoop tar into all containers...")
        all_names = ["hadoop-master"] + [f"hadoop-worker{i}" for i in range(1, worker_count + 1)]
        for name in all_names:
            code, _, err = run_local(
                f"docker cp /tmp/hadoop-3.4.2.tar.gz {name}:/tmp/",
                timeout=180
            )
            if code == 0:
                success(f"Hadoop tar copied to {name}")
            else:
                error(f"Copy failed for {name}: {err}")
    else:
        info("No local Hadoop tar — agent will download inside containers")

    # ── Wait for SSH to be ready ──────────────────────────────────
    info("Waiting for SSH to be ready in containers...")
    time.sleep(8)

    # ── Show running containers ───────────────────────────────────
    code, out, _ = run_local("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
    info(f"Running containers:\n{out}")

    # ── Print summary ─────────────────────────────────────────────
    print()
    success("=" * 60)
    success(f"  hadoop-master   : {master_ip}  (vboxuser / hadoop123)")
    for w in workers:
        success(f"  hadoop-worker{w['id']}  : {w['ip']}  (vboxuser / hadoop123)")
    success("=" * 60)
    success(f"  HDFS NameNode UI → http://localhost:9870")
    success(f"  YARN Resource UI → http://localhost:8088")
    success(f"  All Nodes List   → http://localhost:8088/cluster/nodes")
    for w in workers:
        success(f"  Worker-{w['id']} NM UI  → http://localhost:{w['nm_port']}")
    success("=" * 60)
    print()

    return {
        "master": {
            "ip": master_ip,
            "username": "vboxuser",
            "password": "hadoop123"
        },
        "workers": workers
    }
