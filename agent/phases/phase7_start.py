import time
from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error, rule_trigger

def sudo_run(ssh, command, password, timeout=120):
    return ssh.run(f"echo '{password}' | sudo -S {command}", timeout=timeout)

def fix_datanode(node, label, master_ip, password):
    """Rule engine fix for DataNode not starting"""
    rule_trigger("DATANODE_NOT_STARTED",
                 "Fixing: clearing old datanode dir and restarting DataNode")
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    try:
        ssh.connect()
        username = node["username"]
        base_dir = f"/home/{username}/hadoop3-dir"

        # Step 1: Clear old datanode data (clusterID mismatch fix)
        info(f"Clearing old datanode dir on {label}...")
        ssh.run(f"rm -rf {base_dir}/datanode-dir/*")
        ssh.run(f"mkdir -p {base_dir}/datanode-dir")

        # Step 2: Set HADOOP_HOME and start DataNode manually
        info(f"Starting DataNode manually on {label}...")
        code, out, err = ssh.run(
            f"export HADOOP_HOME=/usr/local/hadoop-3.4.2 && "
            f"export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64 && "
            f"export PATH=$PATH:$HADOOP_HOME/bin:$HADOOP_HOME/sbin && "
            f"/usr/local/hadoop-3.4.2/bin/hdfs datanode &",
            timeout=30
        )
        time.sleep(5)

        # Step 3: Verify
        code, out, _ = ssh.run("jps")
        if "DataNode" in out:
            success(f"DataNode started on {label}")
            return True
        else:
            error(f"DataNode still not running on {label}")
            error(f"Check logs: /usr/local/hadoop-3.4.2/logs/")
            return False
    except Exception as e:
        error(f"DataNode fix failed on {label}: {e}")
        return False
    finally:
        ssh.close()

def run_phase7(cluster):
    phase_banner(7, "Format NameNode & Start Hadoop Cluster")
    master   = cluster["master"]
    workers  = cluster["workers"]
    password = master["password"]

    ssh = SSHClient(master["ip"], master["username"], master["password"])
    try:
        ssh.connect()

        # Step 1: Clean old namenode data
        info("Cleaning old NameNode data...")
        username = master["username"]
        base_dir = f"/home/{username}/hadoop3-dir"
        ssh.run(f"rm -rf {base_dir}/namenode-dir/*")
        ssh.run(f"mkdir -p {base_dir}/namenode-dir")

        # Step 2: Format NameNode
        info("Formatting NameNode...")
        code, out, err = ssh.run(
            "export HADOOP_HOME=/usr/local/hadoop-3.4.2 && "
            "export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64 && "
            "export PATH=$PATH:$HADOOP_HOME/bin && "
            "echo 'Y' | /usr/local/hadoop-3.4.2/bin/hdfs namenode -format 2>&1",
            timeout=120
        )
        if "successfully formatted" in out or code == 0:
            success("NameNode formatted successfully")
        else:
            error(f"NameNode format issue: {err}")
            if "file:///" in err or "invalid URI" in err.lower():
                rule_trigger("FS_DEFAULTFS_INVALID",
                             "Fix core-site.xml fs.defaultFS to hdfs://MASTER_IP:9000")
            return False

        # Step 3: Start all services
        info("Starting Hadoop cluster...")
        code, out, err = ssh.run(
            "export HADOOP_HOME=/usr/local/hadoop-3.4.2 && "
            "export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64 && "
            "export PATH=$PATH:$HADOOP_HOME/bin:$HADOOP_HOME/sbin && "
            "cd /usr/local/hadoop-3.4.2 && "
            "sbin/start-all.sh 2>&1",
            timeout=120
        )
        info(out)
        time.sleep(15)

        # Step 4: Verify Master JPS
        code, out, _ = ssh.run("jps")
        info(f"Master JPS:\n{out}")
        master_daemons = ["NameNode", "SecondaryNameNode", "ResourceManager"]
        for d in master_daemons:
            if d in out:
                success(f"  ✔ {d} running on Master")
            else:
                error(f"  ✘ {d} NOT running on Master")
                if d == "NameNode":
                    rule_trigger("NAMENODE_NOT_STARTED",
                                 "Check: /usr/local/hadoop-3.4.2/logs/")

    except Exception as e:
        error(f"Startup failed: {e}")
        return False
    finally:
        ssh.close()

    # Step 5: Verify Workers — fix DataNode if missing
    all_ok = True
    for w in workers:
        label = f"Worker-{w['id']}"
        w_ssh = SSHClient(w["ip"], w["username"], w["password"])
        try:
            w_ssh.connect()

            # Clear worker datanode dir first to avoid clusterID mismatch
            username = w["username"]
            base_dir = f"/home/{username}/hadoop3-dir"
            w_ssh.run(f"rm -rf {base_dir}/datanode-dir/*")
            w_ssh.run(f"mkdir -p {base_dir}/datanode-dir")

            _, w_out, _ = w_ssh.run("jps")
            info(f"{label} JPS:\n{w_out}")

            for d in ["DataNode", "NodeManager"]:
                if d in w_out:
                    success(f"  ✔ {d} on {label}")
                else:
                    error(f"  ✘ {d} NOT on {label}")
                    if d == "DataNode":
                        # Rule engine auto-fix
                        fix_datanode(w, label, master["ip"], w["password"])
                    if d == "NodeManager":
                        rule_trigger("NODEMANAGER_NOT_STARTED",
                                     "Check yarn-site.xml resourcemanager.hostname")
        except Exception as e:
            error(f"Cannot verify {label}: {e}")
        finally:
            w_ssh.close()

    # Step 6: Open firewall ports
    ssh2 = SSHClient(master["ip"], master["username"], master["password"])
    try:
        ssh2.connect()
        for port in [9870, 8088, 9000, 8032]:
            sudo_run(ssh2, f"ufw allow {port}/tcp 2>/dev/null || true", password)
    except:
        pass
    finally:
        ssh2.close()

    print()
    success(f"HDFS Web UI  → http://{master['ip']}:9870")
    success(f"YARN Web UI  → http://{master['ip']}:8088")
    success(f"All Nodes    → http://{master['ip']}:8088/cluster/nodes")
    return True
