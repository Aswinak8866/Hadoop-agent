from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error
from agent.rules.rule_engine import RuleEngine

BASHRC_CONTENT = """
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
export HADOOP_HOME=/usr/local/hadoop-3.4.2
export HADOOP_INSTALL=$HADOOP_HOME
export HADOOP_MAPRED_HOME=$HADOOP_HOME
export HADOOP_COMMON_HOME=$HADOOP_HOME
export HADOOP_HDFS_HOME=$HADOOP_HOME
export HADOOP_CONF_DIR=$HADOOP_HOME/etc/hadoop
export HADOOP_YARN_HOME=$HADOOP_HOME
export LD_LIBRARY_PATH=$HADOOP_HOME/lib/native:$LD_LIBRARY_PATH
export HADOOP_OPTS="-Djava.library.path=$HADOOP_HOME/lib/native"
export PATH=$PATH:$JAVA_HOME/bin:$HADOOP_HOME/bin:$HADOOP_HOME/sbin
"""

def check_node(node_info, label):
    ssh = SSHClient(node_info["ip"], node_info["username"], node_info["password"])
    try:
        ssh.connect()
        info(f"Connected to {label} ({node_info['ip']})")
        engine = RuleEngine(ssh)

        # Check Java
        code, out, _ = ssh.run("java -version 2>&1")
        if code != 0 or "version" not in out:
            if not engine.fix_java():
                error(f"Java install failed on {label}")
                return False
        else:
            success(f"Java found on {label}: {out.splitlines()[0]}")

        # Check Python
        code, out, _ = ssh.run("python3 --version 2>&1")
        if code != 0:
            if not engine.fix_python():
                error(f"Python install failed on {label}")
                return False
        else:
            success(f"Python found on {label}: {out.strip()}")

        # Set .bashrc paths
        ssh.run(f"grep -q 'HADOOP_HOME' /root/.bashrc || echo '{BASHRC_CONTENT}' >> /root/.bashrc")
        ssh.run("source /root/.bashrc || true")
        success(f"Environment paths set on {label}")

    except Exception as e:
        error(f"Cannot connect to {label}: {e}")
        return False
    finally:
        ssh.close()
    return True

def run_phase2(cluster):
    phase_banner(2, "Check & Install Java + Python on All Nodes")
    all_ok = True

    if not check_node(cluster["master"], "Master"):
        all_ok = False

    for w in cluster["workers"]:
        if not check_node(w, f"Worker-{w['id']}"):
            all_ok = False

    return all_ok
