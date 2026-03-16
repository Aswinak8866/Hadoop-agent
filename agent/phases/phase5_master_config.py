from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error

CONF_PATH    = "/usr/local/hadoop-3.4.2/etc/hadoop"
HADOOP_HOME  = "/usr/local/hadoop-3.4.2"

def get_home_dir(username):
    return "/root" if username == "root" else f"/home/{username}"

def get_hadoop_env():
    return """export LD_LIBRARY_PATH=$HADOOP_HOME/lib/native:$LD_LIBRARY_PATH
export HADOOP_OPTS="-Djava.library.path=$HADOOP_HOME/lib"
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64
export HADOOP_CONF_DIR=$HADOOP_HOME/etc/hadoop
export HDFS_NAMENODE_USER={user}
export HADOOP_USER_NAME={user}
export HDFS_DATANODE_USER={user}
export HDFS_SECONDARYNAMENODE_USER={user}
export YARN_NODEMANAGER_USER={user}
export YARN_RESOURCEMANAGER_USER={user}
export HADOOP_HEAPSIZE=4096
"""

def get_core_site(master_ip):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property>
    <name>fs.defaultFS</name>
    <value>hdfs://{master_ip}:9000</value>
  </property>
  <property>
    <name>dfs.namenode.http-address</name>
    <value>http://{master_ip}:9870</value>
  </property>
  <property>
    <name>ipc.maximum.data.length</name>
    <value>134217728</value>
  </property>
  <property>
    <name>hadoop.home.dir</name>
    <value>/usr/local/hadoop-3.4.2</value>
  </property>
</configuration>"""

def get_hdfs_site_master(username, workers):
    home = get_home_dir(username)
    worker_blocks = ""
    for w in workers:
        worker_blocks += f"""
  <property>
    <name>dfs.datanode.hostname</name>
    <value>{w['ip']}</value>
  </property>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property>
    <name>dfs.namenode.name.dir</name>
    <value>{home}/hadoop3-dir/namenode-dir</value>
  </property>
  <property>
    <name>dfs.replication</name>
    <value>2</value>
  </property>
  <property>
    <name>dfs.webhdfs.enabled</name>
    <value>true</value>
  </property>
  <property>
    <name>dfs.namenode.http.address</name>
    <value>0.0.0.0:9870</value>
  </property>
  <property>
    <name>ipc.maximum.data.length</name>
    <value>134217728</value>
  </property>
  <property>
    <name>dfs.namenode.handler.count</name>
    <value>100</value>
  </property>{worker_blocks}
</configuration>"""

def get_yarn_site(master_ip):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property>
    <name>yarn.nodemanager.aux-services</name>
    <value>mapreduce_shuffle</value>
  </property>
  <property>
    <name>yarn.nodemanager.aux-services.mapreduce.shuffle.class</name>
    <value>org.apache.hadoop.mapred.ShuffleHandler</value>
  </property>
  <property>
    <name>yarn.resourcemanager.hostname</name>
    <value>{master_ip}</value>
  </property>
  <property>
    <name>yarn.resourcemanager.address</name>
    <value>{master_ip}:8032</value>
  </property>
  <property>
    <name>yarn.scheduler.minimum-allocation-mb</name>
    <value>512</value>
  </property>
  <property>
    <name>yarn.scheduler.maximum-allocation-mb</name>
    <value>16384</value>
  </property>
  <property>
    <name>yarn.nodemanager.resource.memory-mb</name>
    <value>24576</value>
  </property>
  <property>
    <name>yarn.nodemanager.resource.cpu-vcores</name>
    <value>12</value>
  </property>
  <property>
    <name>yarn.scheduler.maximum-allocation-vcores</name>
    <value>10</value>
  </property>
  <property>
    <name>yarn.resourcemanager.scheduler.class</name>
    <value>org.apache.hadoop.yarn.server.resourcemanager.scheduler.capacity.CapacityScheduler</value>
  </property>
</configuration>"""

def get_mapred_site():
    return """<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <property>
    <name>mapreduce.framework.name</name>
    <value>yarn</value>
  </property>
</configuration>"""

def get_workers_file(workers):
    return "\n".join([w["ip"] for w in workers]) + "\n"

def run_phase5(cluster):
    phase_banner(5, "Configure Hadoop on Master Node")
    master    = cluster["master"]
    workers   = cluster["workers"]
    master_ip = master["ip"]
    username  = master["username"]

    ssh = SSHClient(master["ip"], master["username"], master["password"])
    try:
        ssh.connect()

        hadoop_env = get_hadoop_env().replace("{user}", username)

        files = {
            f"{CONF_PATH}/hadoop-env.sh":   hadoop_env,
            f"{CONF_PATH}/core-site.xml":   get_core_site(master_ip),
            f"{CONF_PATH}/hdfs-site.xml":   get_hdfs_site_master(username, workers),
            f"{CONF_PATH}/yarn-site.xml":   get_yarn_site(master_ip),
            f"{CONF_PATH}/mapred-site.xml": get_mapred_site(),
            f"{CONF_PATH}/workers":         get_workers_file(workers),
        }
        for path, content in files.items():
            ssh.write_remote_file(path, content)
            success(f"Written: {path}")

    except Exception as e:
        error(f"Master config failed: {e}")
        return False
    finally:
        ssh.close()
    return True
