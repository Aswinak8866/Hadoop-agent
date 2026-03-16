import os
from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error

HADOOP_VERSION = "3.4.2"
HADOOP_TAR     = f"hadoop-{HADOOP_VERSION}.tar.gz"
HADOOP_URL     = f"https://archive.apache.org/dist/hadoop/common/hadoop-{HADOOP_VERSION}/{HADOOP_TAR}"
LOCAL_TAR      = f"/tmp/{HADOOP_TAR}"
REMOTE_TMP     = f"/tmp/{HADOOP_TAR}"
INSTALL_DIR    = "/usr/local"

def sudo_run(ssh, command, password, timeout=120):
    """Run a sudo command by piping password via -S flag"""
    return ssh.run(f"echo '{password}' | sudo -S {command}", timeout=timeout)

def download_locally():
    if os.path.exists(LOCAL_TAR):
        success(f"Hadoop tar already exists at {LOCAL_TAR}")
        return True

    info(f"Downloading Hadoop {HADOOP_VERSION} on local machine...")
    ret = os.system(f"wget -q --show-progress -O {LOCAL_TAR} {HADOOP_URL}")
    if ret != 0 or not os.path.exists(LOCAL_TAR):
        error("wget failed. Trying curl...")
        ret = os.system(f"curl -L -o {LOCAL_TAR} {HADOOP_URL}")
        if ret != 0 or not os.path.exists(LOCAL_TAR):
            error("Download failed. Please manually place hadoop-3.4.2.tar.gz at /tmp/")
            return False

    success(f"Downloaded to {LOCAL_TAR}")
    return True

def upload_and_extract(node, label):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    password = node["password"]
    try:
        ssh.connect()

        # Check if already installed
        code, _, _ = ssh.run(f"ls {INSTALL_DIR}/hadoop-{HADOOP_VERSION}/bin/hadoop 2>/dev/null")
        if code == 0:
            success(f"Hadoop already installed on {label}")
            return True

        # Step 1: Upload tar to /tmp/ (no permission needed)
        code, _, _ = ssh.run(f"ls {REMOTE_TMP} 2>/dev/null")
        if code != 0:
            info(f"Uploading Hadoop tar to {label} ({node['ip']})... (this may take a while)")
            ssh.put_file(LOCAL_TAR, REMOTE_TMP)
            success(f"Upload complete on {label}")
        else:
            info(f"Tar already in /tmp on {label}, skipping upload")

        # Step 2: Extract in /tmp/
        info(f"Extracting Hadoop on {label}...")
        code, out, err = ssh.run(
            f"cd /tmp && tar -xzf {HADOOP_TAR}",
            timeout=300
        )
        if code != 0:
            error(f"Extract failed on {label}: {err}")
            return False

        # Step 3: Move to /usr/local/ using sudo -S (password via stdin)
        info(f"Moving Hadoop to /usr/local/ on {label}...")
        code, out, err = sudo_run(
            ssh,
            f"mv /tmp/hadoop-{HADOOP_VERSION} {INSTALL_DIR}/",
            password,
            timeout=60
        )
        if code != 0:
            error(f"Move failed on {label}: {err}")
            return False
        success(f"Moved to {INSTALL_DIR}/hadoop-{HADOOP_VERSION} on {label}")

        # Step 4: Set ownership so vboxuser can access it
        sudo_run(
            ssh,
            f"chown -R {node['username']}:{node['username']} {INSTALL_DIR}/hadoop-{HADOOP_VERSION}",
            password,
            timeout=60
        )
        success(f"Ownership set on {label}")

        # Step 5: Verify
        code, _, _ = ssh.run(f"ls {INSTALL_DIR}/hadoop-{HADOOP_VERSION}/bin/hadoop")
        if code != 0:
            error(f"Hadoop binary not found after install on {label}")
            return False

        success(f"Hadoop {HADOOP_VERSION} installed on {label}")
        return True

    except Exception as e:
        error(f"Install error on {label}: {e}")
        return False
    finally:
        ssh.close()

def create_dirs(node, label, is_master):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    username = node["username"]
    base_dir = f"/home/{username}/hadoop3-dir" if username != "root" else "/root/hadoop3-dir"
    try:
        ssh.connect()
        if is_master:
            ssh.run(f"mkdir -p {base_dir}/namenode-dir")
            success(f"NameNode directory created on {label}: {base_dir}/namenode-dir")
        else:
            ssh.run(f"mkdir -p {base_dir}/datanode-dir")
            success(f"DataNode directory created on {label}: {base_dir}/datanode-dir")
    except Exception as e:
        error(f"Dir creation failed on {label}: {e}")
    finally:
        ssh.close()

def run_phase4(cluster):
    phase_banner(4, "Download & Install Hadoop 3.4.2 on All Nodes")
    master  = cluster["master"]
    workers = cluster["workers"]

    # Step 1: Download on local machine
    if not download_locally():
        return False

    # Step 2: Upload and extract on Master
    if not upload_and_extract(master, "Master"):
        return False
    create_dirs(master, "Master", is_master=True)

    # Step 3: Upload and extract on each Worker
    for w in workers:
        label = f"Worker-{w['id']}"
        if not upload_and_extract(w, label):
            error(f"Hadoop install failed on {label}")
        create_dirs(w, label, is_master=False)

    return True
