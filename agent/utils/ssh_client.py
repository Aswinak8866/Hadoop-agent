import paramiko
import time

class SSHClient:
    def __init__(self, host, username, password, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.client = None

    def connect(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host, port=self.port,
            username=self.username, password=self.password,
            timeout=30
        )

    def run(self, command, timeout=120):
        if not self.client:
            self.connect()
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out, err

    def put_file(self, local_path, remote_path):
        sftp = self.client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()

    def write_remote_file(self, remote_path, content):
        sftp = self.client.open_sftp()
        with sftp.open(remote_path, 'w') as f:
            f.write(content)
        sftp.close()

    def close(self):
        if self.client:
            self.client.close()
