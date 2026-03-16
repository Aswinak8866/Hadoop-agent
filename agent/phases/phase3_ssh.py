import time
from agent.utils.ssh_client import SSHClient
from agent.utils.logger import phase_banner, info, success, error, warn, rule_trigger
from agent.rules.rule_engine import RuleEngine

def get_ssh_dir(username):
    if username == "root":
        return "/root/.ssh"
    return f"/home/{username}/.ssh"

def ensure_ssh(node, label):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    try:
        ssh.connect()
        engine = RuleEngine(ssh)
        code, out, _ = ssh.run("systemctl is-active ssh 2>&1")
        if out.strip() != "active":
            if not engine.fix_ssh():
                error(f"SSH service failed on {label}")
                return False
        else:
            success(f"SSH service active on {label}")
    except Exception as e:
        error(f"Cannot connect to {label}: {e}")
        return False
    finally:
        ssh.close()
    return True

def generate_key(node, label):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    ssh_dir = get_ssh_dir(node["username"])
    try:
        ssh.connect()
        ssh.run(f"mkdir -p {ssh_dir} && chmod 700 {ssh_dir}")
        # Remove old key if exists to avoid prompt
        ssh.run(f"rm -f {ssh_dir}/id_rsa {ssh_dir}/id_rsa.pub")
        code, out, err = ssh.run(
            f'ssh-keygen -t rsa -N "" -f {ssh_dir}/id_rsa'
        )
        if code != 0:
            error(f"Key gen failed on {label}: {err}")
            return None
        code, pub_key, _ = ssh.run(f"cat {ssh_dir}/id_rsa.pub")
        if code == 0 and pub_key:
            success(f"SSH key generated on {label}")
            return pub_key.strip()
        else:
            error(f"Key read failed on {label}")
            return None
    except Exception as e:
        error(f"Key gen error on {label}: {e}")
        return None
    finally:
        ssh.close()

def add_key_to_node(node, label, pub_key):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    ssh_dir = get_ssh_dir(node["username"])
    try:
        ssh.connect()
        ssh.run(f"mkdir -p {ssh_dir} && chmod 700 {ssh_dir}")
        ssh.run(f'grep -qF "{pub_key}" {ssh_dir}/authorized_keys 2>/dev/null || echo "{pub_key}" >> {ssh_dir}/authorized_keys')
        ssh.run(f"chmod 600 {ssh_dir}/authorized_keys")
        success(f"Key added to {label}")
    except Exception as e:
        error(f"Failed adding key to {label}: {e}")
    finally:
        ssh.close()

def update_hosts(node, label, all_nodes):
    ssh = SSHClient(node["ip"], node["username"], node["password"])
    try:
        ssh.connect()
        for n in all_nodes:
            entry = f"{n['ip']}   {n['hostname']}"
            ssh.run(f'grep -q "{n["ip"]}" /etc/hosts || echo "{entry}" | sudo tee -a /etc/hosts')
        success(f"/etc/hosts updated on {label}")
    except Exception as e:
        error(f"Hosts update failed on {label}: {e}")
    finally:
        ssh.close()

def test_ssh_from_master(master, workers):
    ssh = SSHClient(master["ip"], master["username"], master["password"])
    ssh_dir = get_ssh_dir(master["username"])
    all_ok = True
    try:
        ssh.connect()
        for w in workers:
            code, out, err = ssh.run(
                f'ssh -i {ssh_dir}/id_rsa'
                f' -o StrictHostKeyChecking=no'
                f' -o ConnectTimeout=10'
                f' {w["username"]}@{w["ip"]}'
                f' "echo SSH_OK"'
            )
            if code == 0 and "SSH_OK" in out:
                success(f"Master -> Worker-{w['id']} ({w['ip']}): SSH OK")
            else:
                error(f"Master -> Worker-{w['id']} ({w['ip']}): SSH FAILED — {err}")
                all_ok = False
    except Exception as e:
        error(f"SSH test error: {e}")
        all_ok = False
    finally:
        ssh.close()
    return all_ok

def run_phase3(cluster):
    phase_banner(3, "SSH Check, Key Exchange & Connectivity Test")
    master  = cluster["master"]
    workers = cluster["workers"]
    all_nodes_info = [master] + workers

    # Step 1: Ensure SSH running on all nodes
    for node in all_nodes_info:
        label = "Master" if node == master else f"Worker-{node['id']}"
        ensure_ssh(node, label)

    # Step 2: Generate keys on all nodes
    keys = {}
    for node in all_nodes_info:
        label = "Master" if node == master else f"Worker-{node['id']}"
        key = generate_key(node, label)
        if key:
            keys[node["ip"]] = key

    # Step 3: Add own key to authorized_keys on each node
    for node in all_nodes_info:
        label = "Master" if node == master else f"Worker-{node['id']}"
        if node["ip"] in keys:
            add_key_to_node(node, f"{label} (self)", keys[node["ip"]])

    # Step 4: Cross-distribute all keys to all nodes
    info("Distributing SSH keys across all nodes...")
    for target_node in all_nodes_info:
        target_label = "Master" if target_node == master else f"Worker-{target_node['id']}"
        for source_node in all_nodes_info:
            if source_node["ip"] != target_node["ip"]:
                source_label = "Master" if source_node == master else f"Worker-{source_node['id']}"
                if source_node["ip"] in keys:
                    add_key_to_node(target_node, f"{target_label} <- {source_label}", keys[source_node["ip"]])

    # Step 5: Build hostnames and update /etc/hosts
    hostname_map = []
    hostname_map.append({"ip": master["ip"], "hostname": "hadoop-master"})
    for w in workers:
        hostname_map.append({"ip": w["ip"], "hostname": f"hadoop-worker{w['id']}"})

    for node in all_nodes_info:
        label = "Master" if node == master else f"Worker-{node['id']}"
        update_hosts(node, label, hostname_map)

    # Step 6: Test SSH from master to each worker
    return test_ssh_from_master(master, workers)
