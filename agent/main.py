from rich.console import Console
from rich.panel import Panel

from agent.phases.phase2_prereqs       import run_phase2
from agent.phases.phase3_ssh           import run_phase3
from agent.phases.phase4_install       import run_phase4
from agent.phases.phase5_master_config import run_phase5
from agent.phases.phase6_worker_config import run_phase6
from agent.phases.phase7_start         import run_phase7

console = Console()

def collect_real_machines():
    print()
    master_ip   = input("  Enter Master Node IP address        : ").strip()
    master_user = input("  Enter Master SSH username           : ").strip()
    master_pass = input("  Enter Master SSH password           : ").strip()
    print()
    while True:
        try:
            worker_count = int(input("  How many Worker Nodes do you want?  : ").strip())
            if worker_count > 0:
                break
            print("  Enter a number greater than 0.")
        except ValueError:
            print("  Invalid input.")
    workers = []
    for i in range(1, worker_count + 1):
        print(f"\n  --- Worker Node {i} ---")
        w_ip   = input(f"  Worker {i} IP address   : ").strip()
        w_user = input(f"  Worker {i} SSH username : ").strip()
        w_pass = input(f"  Worker {i} SSH password : ").strip()
        workers.append({"id": i, "ip": w_ip, "username": w_user, "password": w_pass})
    return {
        "master": {"ip": master_ip, "username": master_user, "password": master_pass},
        "workers": workers
    }

def main():
    console.print(Panel.fit(
        "[bold white]HADOOP 3.4.2 CLUSTER INSTALLATION AGENT[/bold white]\n"
        "[cyan]Ubuntu 22.04  |  Auto Rule Engine  |  Real Machines or Docker[/cyan]",
        border_style="bold blue"
    ))

    print()
    console.print("[bold yellow]Choose your deployment type:[/bold yellow]")
    console.print("  [bold cyan]1[/bold cyan] → I have real/separate machines or VMs (I will give IPs)")
    console.print("  [bold cyan]2[/bold cyan] → I have only 1 machine (Agent auto-creates Docker containers)\n")

    while True:
        choice = input("  Enter your choice (1 or 2) : ").strip()
        if choice in ["1", "2"]:
            break
        print("  Please enter 1 or 2.")

    if choice == "1":
        console.print("\n[bold green]Mode: Real Machines[/bold green]\n")
        cluster = collect_real_machines()
    else:
        console.print("\n[bold green]Mode: Docker Containers (Auto)[/bold green]\n")
        from agent.phases.phase0_docker import run_phase0
        cluster = run_phase0()
        if not cluster:
            console.print("[red]Docker setup failed.[/red]")
            console.print("[yellow]Fix: sudo systemctl start docker[/yellow]")
            return

    # Show summary
    print()
    console.print(f"[bold cyan]Master   :[/bold cyan] {cluster['master']['ip']} ({cluster['master']['username']})")
    for w in cluster["workers"]:
        console.print(f"[bold cyan]Worker {w['id']}  :[/bold cyan] {w['ip']} ({w['username']})")
    print()

    # Run all phases
    if not run_phase2(cluster):
        console.print("[red]Phase 2 failed.[/red]")
        return

    if not run_phase3(cluster):
        console.print("[red]Phase 3 failed.[/red]")
        return

    if not run_phase4(cluster):
        console.print("[red]Phase 4 failed.[/red]")
        return

    if not run_phase5(cluster):
        console.print("[red]Phase 5 failed.[/red]")
        return

    run_phase6(cluster)
    run_phase7(cluster)

    master_ip    = cluster["master"]["ip"]
    worker_count = len(cluster["workers"])

    if choice == "2":
        hdfs_url  = "http://localhost:9870"
        yarn_url  = "http://localhost:8088"
        nodes_url = "http://localhost:8088/cluster/nodes"
        worker_urls = "\n".join([
            f"  Worker-{w['id']} UI → http://localhost:{w.get('nm_port', 8100 + w['id'])}"
            for w in cluster["workers"]
        ])
    else:
        hdfs_url  = f"http://{master_ip}:9870"
        yarn_url  = f"http://{master_ip}:8088"
        nodes_url = f"http://{master_ip}:8088/cluster/nodes"
        worker_urls = ""

    console.print(Panel.fit(
        f"[bold green]✔  HADOOP 3.4.2 CLUSTER SETUP COMPLETE[/bold green]\n\n"
        f"[white]Master  : {master_ip}[/white]\n"
        f"[white]Workers : {worker_count} nodes[/white]\n\n"
        f"[bold cyan]HDFS UI    → {hdfs_url}[/bold cyan]\n"
        f"[bold cyan]YARN UI    → {yarn_url}[/bold cyan]\n"
        f"[bold cyan]All Nodes  → {nodes_url}[/bold cyan]\n"
        + (f"\n[cyan]{worker_urls}[/cyan]" if worker_urls else "") +
        f"\n\n[yellow]YARN Active Nodes should show: {worker_count}[/yellow]",
        border_style="green"
    ))

if __name__ == "__main__":
    main()
