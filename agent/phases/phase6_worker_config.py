from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error

CONF_PATH = "/usr/local/hadoop-3.4.2/etc/hadoop"

def get_home_dir(username):
    return "/root" if username == "root" else f"/home/{username}"

def get_hdfs_site_worker(username):
    home = get_home_dir(username)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property>
    <name>dfs.datanode.data.dir</name>
    <value>{home}/hadoop3-dir/datanode-dir</value>
  </property>
  <property>
    <name>dfs.replication</name>
    <value>2</value>
  </property>
  <property>
    <name>ipc.maximum.data.length</name>
    <value>134217728</value>
  </property>
  <property>
    <name>dfs.webhdfs.enabled</name>
    <value>true</value>
  </property>
</configuration>"""

def run_phase6(cluster):
    phase_banner(6, "Configure Hadoop on All Worker Nodes")
    master    = cluster["master"]
    master_ip = master["ip"]

    from agent.phases.phase5_master_config import (
        get_hadoop_env, get_core_site, get_yarn_site, get_mapred_site
    )

    for w in cluster["workers"]:
        label    = f"Worker-{w['id']}"
        username = w["username"]
        ssh = SSHClient(w["ip"], w["username"], w["password"])
        try:
            ssh.connect()
            hadoop_env = get_hadoop_env().replace("{user}", username)
            files = {
                f"{CONF_PATH}/hadoop-env.sh":   hadoop_env,
                f"{CONF_PATH}/core-site.xml":   get_core_site(master_ip),
                f"{CONF_PATH}/hdfs-site.xml":   get_hdfs_site_worker(username),
                f"{CONF_PATH}/yarn-site.xml":   get_yarn_site(master_ip),
                f"{CONF_PATH}/mapred-site.xml": get_mapred_site(),
            }
            for path, content in files.items():
                ssh.write_remote_file(path, content)
            success(f"All config files written on {label}")
        except Exception as e:
            error(f"Worker config failed on {label}: {e}")
        finally:
            ssh.close()
    return True
