from agent.utils.logger import rule_trigger, success, error, info

class RuleEngine:
    def __init__(self, ssh_client):
        self.ssh = ssh_client

    def run(self, command, timeout=120):
        return self.ssh.run(command, timeout=timeout)

    def fix_java(self):
        rule_trigger("JAVA_NOT_FOUND", "Auto-installing OpenJDK 11")
        cmds = [
            "sudo apt update -y",
            "sudo apt install -y openjdk-11-jdk"
        ]
        for cmd in cmds:
            code, out, err = self.run(cmd, timeout=300)
            if code != 0:
                error(f"Failed: {cmd} => {err}")
                return False
        code, out, _ = self.run("java -version 2>&1")
        if code == 0:
            success("Java 11 installed successfully")
            return True
        error("Java install failed after retry")
        return False

    def fix_python(self):
        rule_trigger("PYTHON_NOT_FOUND", "Auto-installing Python 3.10")
        cmds = [
            "sudo apt update -y",
            "sudo apt install -y python3.10"
        ]
        for cmd in cmds:
            code, out, err = self.run(cmd, timeout=300)
            if code != 0:
                error(f"Failed: {cmd} => {err}")
                return False
        code, out, _ = self.run("python3 --version 2>&1")
        if code == 0:
            success("Python installed successfully")
            return True
        return False

    def fix_ssh(self):
        rule_trigger("SSH_NOT_INSTALLED", "Auto-installing OpenSSH Server")
        cmds = [
            "sudo apt update -y",
            "sudo apt install -y openssh-server",
            "sudo systemctl enable ssh",
            "sudo systemctl start ssh"
        ]
        for cmd in cmds:
            self.run(cmd, timeout=180)
        code, out, _ = self.run("systemctl is-active ssh")
        return out.strip() == "active"

    def fix_permissions(self):
        rule_trigger("SSH_PERMISSION_ERROR", "Fixing .ssh directory permissions")
        self.run("chmod 700 /root/.ssh")
        self.run("chmod 600 /root/.ssh/authorized_keys")
        success("Permissions fixed")

    def fix_core_site(self, ssh, master_ip):
        rule_trigger("FS_DEFAULTFS_INVALID",
                     "fs.defaultFS is file:/// — fixing core-site.xml")
        info(f"Setting fs.defaultFS to hdfs://{master_ip}:9000")

    def fix_firewall(self, port):
        rule_trigger("PORT_UNREACHABLE", f"Opening firewall port {port}")
        self.run(f"sudo ufw allow {port}/tcp")
        success(f"Port {port} opened")
