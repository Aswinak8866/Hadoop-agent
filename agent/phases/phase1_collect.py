from agent.utils.logger import phase_banner, info, success

def collect_cluster_info():
    phase_banner(1, "Collect Cluster Node Information")

    print()
    master_ip       = input("  Enter Master Node IP address        : ").strip()
    master_user     = input("  Enter Master SSH username (e.g. root): ").strip()
    master_password = input("  Enter Master SSH password            : ").strip()

    print()
    while True:
        try:
            worker_count = int(input("  How many Worker Nodes do you want?  : ").strip())
            if worker_count > 0:
                break
            print("  Please enter a number greater than 0.")
        except ValueError:
            print("  Invalid input. Enter a number.")

    workers = []
    for i in range(1, worker_count + 1):
        print(f"\n  --- Worker Node {i} ---")
        w_ip   = input(f"  Worker {i} IP address  : ").strip()
        w_user = input(f"  Worker {i} SSH username: ").strip()
        w_pass = input(f"  Worker {i} SSH password: ").strip()
        workers.append({
            "id": i,
            "ip": w_ip,
            "username": w_user,
            "password": w_pass
        })

    cluster = {
        "master": {
            "ip": master_ip,
            "username": master_user,
            "password": master_password
        },
        "workers": workers
    }

    print()
    success(f"Master: {master_ip}  |  Workers: {worker_count}")
    for w in workers:
        info(f"  Worker {w['id']}: {w['ip']}")

    return cluster
