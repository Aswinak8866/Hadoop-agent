from rich.console import Console
from rich.panel import Panel
from rich.text import Text
import datetime

console = Console()

def info(msg):
    console.print(f"[cyan][INFO][/cyan]  {msg}")

def success(msg):
    console.print(f"[green][OK][/green]    {msg}")

def error(msg):
    console.print(f"[red][ERROR][/red] {msg}")

def warn(msg):
    console.print(f"[yellow][WARN][/yellow]  {msg}")

def rule_trigger(rule_name, action):
    console.print(Panel(
        f"[bold yellow]Rule:[/bold yellow] {rule_name}\n[bold white]Action:[/bold white] {action}",
        title="⚙️  RULE ENGINE",
        border_style="yellow"
    ))

def phase_banner(phase_num, title):
    console.print(Panel(
        f"[bold white]{title}[/bold white]",
        title=f"[bold blue]PHASE {phase_num}[/bold blue]",
        border_style="blue"
    ))

def section(title):
    console.rule(f"[bold cyan]{title}[/bold cyan]")
