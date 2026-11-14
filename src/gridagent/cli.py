"""Command-line interface for the Agent Garden (`garden`).

M0 only provides `garden version`. Later milestones add
`list`, `run`, `eval`, `compare` and `new`.
"""

import typer
from rich.console import Console

from gridagent import __version__

app = typer.Typer(
    help="Agent Garden: list, run, evaluate and scaffold agents.", no_args_is_help=True
)
console = Console()


@app.callback()
def main() -> None:
    """Agent Garden CLI."""
    # Keeping a callback means Typer always treats commands as subcommands,
    # even while only one command exists.


@app.command()
def version() -> None:
    """Print the installed gridagent version."""
    console.print(f"gridagent [bold green]{__version__}[/]")
