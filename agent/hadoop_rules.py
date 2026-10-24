#!/usr/bin/env python3
"""Rule engine for the Hadoop 3.3.6 agent (plain pattern rules, no AI).

When an install step fails, the error text is matched against RULES. The agent
explains the cause, applies the known fix (after confirmation) and retries.
"""
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HADOOP_VERSION = "3.3.6"
CONFIG = {"auto": False}   # True = apply safe fixes without asking
CTX = {}                   # paths/helpers, filled in by main.py
_LOCK = threading.Lock()
_WARNED = set()
FIREWALL_PORTS = (9000, 9870, 9864, 9866, 9867, 9868,
                  8088, 8030, 8031, 8032, 8033, 8040, 8042)


# ---------------------------------------------------------------- helpers
def say(message="", prefix=None):
    with _LOCK:
        print(f"[{prefix}] {message}" if prefix else message, flush=True)


class CommandError(RuntimeError):
    def __init__(self, cmd, returncode, output):
        self.cmd, self.returncode, self.output = cmd, returncode, output
        tail = "\n".join(output.strip().splitlines()[-15:])
        super().__init__(f"Command failed ({returncode}): {' '.join(cmd)}\n{tail}")


def run(cmd, timeout=180, check=True, capture=False, prefix=None, env=None):
    """Run a command, stream its output (unless capture) and keep the text."""
    cmd = [str(c) for c in cmd]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace",
                            env=env)
    timed_out = []

    def _kill():
        timed_out.append(True)
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            if not capture:
                say(line.rstrip("\n"), prefix)
        proc.wait()
    finally:
        timer.cancel()
    output, code = "".join(lines), proc.returncode
    if timed_out:
        code = 124
        output += f"\nCommand timed out after {timeout}s: {' '.join(cmd)}\n"
    result = subprocess.CompletedProcess(cmd, code, output, "")
    if check and code != 0:
        raise CommandError(cmd, code, output)
    return result


def sudo(*args):
    """sudo that never waits for a password when there is no terminal."""
    if os.geteuid() == 0:  # root (containers, playgrounds): sudo not needed
        return list(args)
    return ["sudo"] + ([] if sys.stdin.isatty() else ["-n"]) + list(args)


def confirm(question):
    if CONFIG["auto"]:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/n]: ").strip().lower() in ("y", "yes")


def error_text(exc):
    return f"{type(exc).__name__}: {exc}\n{getattr(exc, 'output', '')}"


def set_hadoop_env(name, value):
    """Set (replace) an export line in hadoop-env.sh."""
    conf = Path(CTX["conf"])
    conf.mkdir(parents=True, exist_ok=True)
    env_file = conf / "hadoop-env.sh"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [x for x in lines if not x.strip().startswith(f"export {name}=")]
    lines.append(f'export {name}="{value}"')
    env_file.write_text("\n".join(lines) + "\n")


def local_names():
    names = {"localhost", "127.0.0.1", "0.0.0.0", "::1", socket.gethostname()}
    try:
        names.add(socket.getfqdn())
        names.update(run(["hostname", "-I"], check=False, capture=True).stdout.split())
    except OSError:
        pass
    return names


def _hosts_in(text):
    hosts = set(re.findall(r"connect to host (\S+) port", text))
    hosts |= set(re.findall(r"[\w.-]+@([\w.-]+): Permission denied", text))
    hosts |= set(re.findall(r"Could not resolve hostname ([\w.-]+)", text))
    return hosts


def _all_local(text):
    hosts = _hosts_in(text)
    return not hosts or hosts <= local_names()


# ------------------------------------------------------------------ Java
def _java_major(java_bin):
    try:
        out = run([java_bin, "-version"], check=False, capture=True, timeout=30).stdout
    except OSError:
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
    if not m:
        return None
    major, minor = int(m.group(1)), m.group(2)
    return int(minor) if major == 1 and minor else major


def find_java_home():
    """Pick a JDK for Hadoop 3.3.6: Java 11 first, then 8, else any JDK."""
    homes = []

    def add(path):
        try:
            p = Path(path).resolve()
        except OSError:
            return
        if (p / "bin" / "java").exists() and p not in homes:
            homes.append(p)

    java = shutil.which("java")
    if java:
        add(Path(java).resolve().parent.parent)
    jvm = Path("/usr/lib/jvm")
    if jvm.is_dir():
        for d in sorted(jvm.iterdir()):
            add(d)
    if not homes:
        raise RuntimeError("Java is not installed. Install Java first.")
    found = [(_java_major(h / "bin" / "java"), h) for h in homes]
    jdks = [(m, h) for m, h in found if (h / "bin" / "jps").exists()]
    if not jdks:
        raise RuntimeError("Java JDK not found (jps is missing). Install a JDK.")
    for wanted in (11, 8):
        for major, home in jdks:
            if major == wanted:
                return str(home)
    if "java" not in _WARNED:
        _WARNED.add("java")
        say(f"  WARNING: Java {jdks[0][0]} found. Hadoop {HADOOP_VERSION} "
            "officially supports Java 8 and 11.")
    return str(jdks[0][1])


# ------------------------------------------------------------------ fixes
def apt_install(*packages):
    for attempt in range(1, 7):
        r = run(sudo("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install",
                     "-y", *packages), timeout=900, check=False)
        if r.returncode == 0:
            return True
        out = r.stdout
        if re.search(r"a password is required|terminal is required", out, re.I):
            raise RuntimeError(out)
        if re.search(r"Could not get lock|dpkg frontend lock", out, re.I):
            say("  apt is busy (another update is running). Waiting 20 s...")
            time.sleep(20)
        elif re.search(r"Unable to locate package|no installation candidate",
                       out, re.I) and attempt == 1:
            run(sudo("apt-get", "update"), timeout=600, check=False)
        else:
            return False
    return False


def fix_java(text):
    if not (apt_install("openjdk-11-jdk") or apt_install("openjdk-8-jdk")):
        return False
    set_hadoop_env("JAVA_HOME", find_java_home())
    return True


def fix_apt_lock(text):
    time.sleep(20)
    return True


def fix_apt_update(text):
    return run(sudo("apt-get", "update"), timeout=600, check=False).returncode == 0


def fix_pdsh(text):
    set_hadoop_env("PDSH_RCMD_TYPE", "ssh")
    os.environ["PDSH_RCMD_TYPE"] = "ssh"
    return True


def fix_root_user(text):
    user = getpass.getuser()
    for var in ("HDFS_NAMENODE_USER", "HDFS_DATANODE_USER",
                "HDFS_SECONDARYNAMENODE_USER", "YARN_RESOURCEMANAGER_USER",
                "YARN_NODEMANAGER_USER"):
        set_hadoop_env(var, user)
    return True


def fix_hostname(text):
    host = socket.gethostname()
    wanted = re.findall(r"Could not resolve hostname ([\w.-]+)", text)
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or any(w != host for w in wanted):
        return False
    tee = " ".join(sudo("tee", "-a", "/etc/hosts"))
    run(["bash", "-c", f'grep -qw "{host}" /etc/hosts || '
                       f'echo "127.0.1.1 {host}" | {tee} > /dev/null'])
    return True


def fix_ssh_server(text):
    if not _all_local(text):
        return False
    if not apt_install("openssh-server"):
        return False
    run(sudo("systemctl", "enable", "--now", "ssh"), check=False)
    run(sudo("service", "ssh", "start"), check=False)
    return True


def ensure_local_ssh_key():
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    pub = next((ssh_dir / n for n in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub")
                if (ssh_dir / n).exists()), None)
    if pub is None:
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f",
             str(ssh_dir / "id_ed25519"), "-q"])
        pub = ssh_dir / "id_ed25519.pub"
    return pub.read_text().strip()


def authorize_key(key):
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    auth = ssh_dir / "authorized_keys"
    current = auth.read_text().splitlines() if auth.exists() else []
    if key not in current:
        current.append(key)
        auth.write_text("\n".join(current) + "\n")
    auth.chmod(0o600)


def fix_ssh_keys(text):
    if not _all_local(text):
        return False
    authorize_key(ensure_local_ssh_key())
    return True


def fix_cluster_id(text):
    """Destructive: needs an interactive terminal and a typed confirmation."""
    if CONFIG["auto"] or not sys.stdin.isatty():
        return False
    dn = Path(CTX["datanode_dir"])
    say(f"  This DELETES all HDFS block data inside {dn}")
    if input("  Type DELETE to continue: ").strip() != "DELETE":
        return False
    for item in dn.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    return True


def fix_stop_hadoop(text):
    hadoop = Path(CTX["hadoop"])
    for script in ("stop-yarn.sh", "stop-dfs.sh"):
        if (hadoop / "sbin" / script).exists():
            run([hadoop / "sbin" / script], check=False, env=CTX["env"](), timeout=120)
    time.sleep(3)
    return True


def fix_oom(text):
    set_hadoop_env("HADOOP_HEAPSIZE_MAX", "512m")
    return True


def fix_tarball(text):
    tarball = Path(CTX["tarball"])
    tarball.unlink(missing_ok=True)
    Path(str(tarball) + ".part").unlink(missing_ok=True)
    return True


def fix_permission(text):
    base, user = Path(CTX["base"]), getpass.getuser()
    target = base
    while not target.exists() and target != target.parent:
        target = target.parent
    if target != Path.home() and Path.home() not in target.parents:
        return False
    cmd = (["chown", "-R", f"{user}:{user}", str(base)] if base.exists()
           else ["chown", f"{user}:{user}", str(target)])
    run(sudo(*cmd))
    return True


def fix_firewall(text):
    status = run(sudo("ufw", "status"), check=False, capture=True).stdout
    if "Status: active" not in status:
        return False
    for port in FIREWALL_PORTS:
        run(sudo("ufw", "allow", f"{port}/tcp"), check=False, capture=True)
    return True


def open_firewall():
    try:
        return fix_firewall("")
    except Exception:
        return False


# ------------------------------------------------------------------ rules
class Rule:
    def __init__(self, rid, title, patterns, why, action="", fix=None,
                 manual="", destructive=False):
        self.rid, self.title, self.patterns, self.why = rid, title, patterns, why
        self.action, self.fix, self.manual, self.destructive = action, fix, manual, destructive

    def matches(self, text):
        return any(re.search(p, text, re.I) for p in self.patterns)


RULES = [
    Rule("H001", "sudo needs a password",
         [r"a terminal is required to read the password", r"sudo: a password is required",
          r"no tty present and no askpass"],
         "The agent must install packages with sudo, but sudo asks for a password and "
         "there is no terminal (remote node).",
         manual="On that node run: echo \"$USER ALL=(ALL) NOPASSWD:ALL\" | "
                "sudo tee /etc/sudoers.d/hadoop-agent   (or run the agent in a terminal there)."),
    Rule("H002", "apt is locked by another process",
         [r"Could not get lock", r"Unable to acquire the dpkg frontend lock",
          r"dpkg frontend lock"],
         "A fresh system is usually running automatic updates (unattended-upgrades).",
         "Wait 20 seconds and retry", fix_apt_lock),
    Rule("H003", "apt package list is stale",
         [r"Unable to locate package", r"has no installation candidate"],
         "The package index is empty or outdated on this system.",
         "Run apt-get update and retry", fix_apt_update,
         manual="Run: sudo apt-get update"),
    Rule("H004", "pdsh uses rsh instead of ssh",
         [r"rcmd: socket: Permission denied"],
         "pdsh is installed and Hadoop's start scripts use its default rcmd.",
         "Set PDSH_RCMD_TYPE=ssh in hadoop-env.sh", fix_pdsh),
    Rule("H005", "Running as root without Hadoop user variables",
         [r"there is no (HDFS|YARN)_\w+_USER defined"],
         "Hadoop 3 refuses to start daemons as root unless the *_USER variables are set.",
         "Add HDFS_*_USER and YARN_*_USER to hadoop-env.sh", fix_root_user),
    Rule("H006", "Hostname does not resolve",
         [r"Could not resolve hostname"],
         "This machine's hostname is missing from /etc/hosts.",
         "Add '127.0.1.1 <hostname>' to /etc/hosts", fix_hostname,
         manual="Use IP addresses, or add 'IP hostname' lines to /etc/hosts on every node."),
    Rule("H007", "SSH server is not running",
         [r"connect to host \S+ port 22: Connection refused",
          r"No such file or directory: 'ssh'"],
         "Hadoop's start scripts log in with ssh, and sshd is not installed or not running.",
         "Install openssh-server and start it", fix_ssh_server,
         manual="On the node that refused the connection: sudo apt-get install -y "
                "openssh-server && sudo systemctl enable --now ssh"),
    Rule("H008", "Passwordless SSH is not set up",
         [r"Permission denied \(publickey", r"Permission denied, please try again",
          r"Host key verification failed", r"Too many authentication failures"],
         "Hadoop's start scripts need passwordless ssh to every node (and to localhost).",
         "Create an SSH key and authorize it for this user", fix_ssh_keys,
         manual="From the master run: ssh-copy-id USER@NODE_IP for every node."),
    Rule("H009", "HDFS clusterID mismatch",
         [r"Incompatible clusterIDs", r"clusterID.*(does not match|mismatch)"],
         "The NameNode was formatted again but the DataNode still holds the old cluster's data.",
         "Delete the old DataNode data directory (asks you to type DELETE)",
         fix_cluster_id, destructive=True,
         manual="Stop Hadoop, empty the datanode directory, then start again."),
    Rule("H010", "NameNode is not formatted",
         [r"NameNode is not formatted", r"storage directory does not exist or is not accessible",
          r"is in an inconsistent state"],
         "The NameNode metadata directory is missing, empty or damaged.",
         manual="Re-run Install. The agent formats only when the metadata and data "
                "folders are both empty; otherwise back up and empty them first."),
    Rule("H011", "Hadoop port in use / daemon already running",
         [r"java\.net\.BindException", r"Address already in use",
          r"is running as process \d+", r"Port in use"],
         "An old Hadoop daemon (or another program) still holds the port or pid file.",
         "Stop all Hadoop daemons and retry", fix_stop_hadoop,
         manual="Find the program: sudo ss -ltnp | grep :PORT   then stop it."),
    Rule("H012", "Not enough memory for the JVM",
         [r"java\.lang\.OutOfMemoryError", r"Cannot allocate memory",
          r"Native memory allocation .* failed", r"There is insufficient memory"],
         "The machine has too little free RAM for the default Hadoop heap size.",
         "Set HADOOP_HEAPSIZE_MAX=512m in hadoop-env.sh", fix_oom,
         manual="Give the machine at least 2 GB RAM (4 GB for a comfortable cluster)."),
    Rule("H013", "Disk is full",
         [r"No space left on device", r"Errno 28", r"Not enough disk space"],
         "Hadoop needs about 3 GB free for the archive plus the extracted files.",
         manual="Free space (check with: df -h ~) and run the agent again."),
    Rule("H014", "Downloaded archive is corrupt or incomplete",
         [r"Checksum mismatch", r"Compressed file ended before", r"unexpected end of data",
          r"not a gzip file", r"invalid stored block", r"while decompressing data",
          r"CRC check failed", r"Downloaded archive is invalid"],
         "The download was interrupted or damaged.",
         "Delete the cached archive and download it again", fix_tarball),
    Rule("H015", "Download failed (network)",
         [r"Download failed", r"urlopen error", r"Temporary failure in name resolution",
          r"Network is unreachable"],
         "This machine cannot reach archive.apache.org (no internet, DNS or proxy problem).",
         manual="Check: ping -c 2 8.8.8.8  and  curl -I https://archive.apache.org  "
                "(behind a proxy, set https_proxy first)."),
    Rule("H016", "No permission to write the install folder",
         [r"PermissionError", r"Permission denied: '", r"cannot create directory"],
         "The Hadoop folder (or its parent) is owned by another user, often root.",
         "Give the current user ownership of the Hadoop folder", fix_permission),
    Rule("H017", "Java / JDK is missing",
         [r"Java is not installed", r"JAVA_HOME is not set", r"JAVA_HOME.*could not be found",
          r"Java JDK not found", r"No such file or directory: 'jps'",
          r"jps: command not found", r"java: command not found"],
         f"Hadoop {HADOOP_VERSION} needs a JDK (Java 8 or 11) and the agent uses jps.",
         "Install OpenJDK 11 (or 8) with apt and set JAVA_HOME", fix_java,
         manual="Install a JDK: sudo apt-get install -y openjdk-11-jdk"),
    Rule("H018", "Java version is not supported",
         [r"InaccessibleObjectException", r"UnsupportedClassVersionError",
          r"Unsupported Java version"],
         f"Hadoop {HADOOP_VERSION} supports Java 8 and 11 only.",
         "Install OpenJDK 11 and point JAVA_HOME to it", fix_java,
         manual="Install openjdk-11-jdk and set JAVA_HOME in hadoop-env.sh."),
    Rule("H019", "DataNode/NodeManager cannot reach the NameNode",
         [r"Retrying connect to server", r"ConnectException: Connection refused",
          r"Call From .* failed on connection exception"],
         "A firewall blocks the Hadoop ports, or the NameNode is not up yet.",
         "Open the Hadoop ports in ufw (if ufw is active)", fix_firewall,
         manual="Open ports 9000, 9870, 9864-9868, 8030-8033, 8040, 8042, 8088 "
                "(firewall / cloud security group) and check the master IP."),
    Rule("H020", "Old HADOOP_HOME in the shell",
         [r"Cannot execute \S+/libexec/hadoop-config\.sh"],
         "An old HADOOP_HOME export (for example in ~/.bashrc) points to a folder that "
         "no longer exists.",
         manual="Edit ~/.bashrc and remove or fix the old HADOOP_* exports, then open "
                "a new terminal. (The agent itself ignores them.)"),
]


def match(text):
    for rule in RULES:
        if rule.matches(text):
            return rule
    return None


def show(rule, step=""):
    say("")
    say(f"[RULE {rule.rid}] {rule.title}")
    if step:
        say(f"  Step  : {step}")
    say(f"  Cause : {rule.why}")
    if rule.action:
        say(f"  Fix   : {rule.action}")


def manual_help(rule):
    if rule.manual:
        say(f"  Manual: {rule.manual}")


def apply(rule, text):
    """Apply the rule's fix. True = fixed (caller may retry)."""
    if rule.fix is None:
        manual_help(rule)
        return False
    if not rule.destructive and not confirm(f"  Apply fix '{rule.action}'?"):
        say("  Fix skipped.")
        manual_help(rule)
        return False
    try:
        done = bool(rule.fix(text))
    except Exception as exc:
        say(f"  Fix failed: {str(exc).strip().splitlines()[-1] if str(exc).strip() else exc}")
        extra = match(error_text(exc))
        if extra and extra is not rule:
            show(extra)
            manual_help(extra)
        done = False
    if not done:
        manual_help(rule)
    return done


def guarded(step, fn, attempts=3):
    """Run fn(); on a known error apply the rule's fix and retry."""
    for n in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            text = error_text(exc)
            rule = match(text)
            if rule is None:
                raise
            show(rule, step)
            if n == attempts or not apply(rule, text):
                raise
            say(f"  Fix applied. Retrying '{step}' ({n + 1}/{attempts})...")
