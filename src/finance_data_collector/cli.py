"""Command line interface: `finance-data-collector --help`."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from finance_data_collector.nse_option_chain import IST, Collector, CycleResult, market_open

console = Console()
log = logging.getLogger("nse.cli")

app = typer.Typer(
    add_completion=False,
    help="Download NSE option chain snapshots as LZMA-compressed CSVs, on a schedule.",
)


def setup_logging(log_dir: Path, verbose: bool) -> Path:
    """Console gets rich, colourised output; the log file gets everything
    (including DEBUG retries) in plain text, rotated at midnight, 30 days kept."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "collector.log"

    file_handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=30, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    console_handler = RichHandler(console=console, show_path=False, rich_tracebacks=True, markup=False)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))

    root = logging.getLogger("nse")
    root.setLevel(logging.DEBUG)
    root.handlers[:] = [file_handler, console_handler]
    root.propagate = False
    return log_file


def print_summary(result: CycleResult) -> None:
    table = Table(title=f"Cycle {result.started:%Y-%m-%d %H:%M:%S} IST ({result.seconds:.0f}s)", title_justify="left")
    table.add_column("Symbols", justify="right")
    table.add_column("Saved", justify="right", style="green")
    table.add_column("Unchanged", justify="right", style="cyan")
    table.add_column("Skipped", justify="right", style="yellow")
    table.add_column("Failed", justify="right", style="red")
    table.add_row(*(str(n) for n in (result.total, result.saved, result.unchanged, result.skipped, len(result.failed))))
    console.print(table)
    for sym, err in result.failed.items():
        console.print(f"  [red]✗ {sym}[/red]: {err}")


def run_cycle_with_progress(collector: Collector) -> CycleResult:
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Fetching symbol list", total=None)
        result = collector.run_cycle(
            on_start=lambda n: progress.update(task, total=n, description="Downloading"),
            on_symbol=lambda sym: progress.update(task, advance=1, description=f"Done {sym}"),
        )
    print_summary(result)
    return result


def _stop(signum, frame) -> None:
    raise KeyboardInterrupt


@app.command()
def main(
    out: Annotated[Path, typer.Option(help="Folder the CSVs are written to.")] = Path("data"),
    symbols: Annotated[
        Optional[list[str]],
        typer.Option("--symbol", "-s", help="Only this symbol; repeat for more (e.g. -s NIFTY -s ABB). Default: all."),
    ] = None,
    interval: Annotated[int, typer.Option(min=10, help="Seconds between cycle starts.")] = 300,
    delay: Annotated[float, typer.Option(min=0, help="Seconds to wait between requests to NSE.")] = 1.0,
    once: Annotated[bool, typer.Option("--once", help="Run a single cycle and exit.")] = False,
    ignore_market_hours: Annotated[
        bool, typer.Option("--ignore-market-hours", help="Also run outside Mon-Fri 09:15-15:30 IST.")
    ] = False,
    log_dir: Annotated[Path, typer.Option(help="Folder for the log file (collector.log).")] = Path("logs"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug messages on screen too.")] = False,
) -> None:
    """Download option chains for NSE indices and F&O stocks."""
    log_file = setup_logging(log_dir, verbose)
    signal.signal(signal.SIGTERM, _stop)  # `kill` / service stop behaves like Ctrl+C
    signal.signal(signal.SIGINT, signal.default_int_handler)  # even when started in the background
    chosen = [s.upper() for s in symbols] if symbols else None
    collector = Collector(out, chosen, delay)

    log.info(
        "Started: out=%s symbols=%s interval=%ss delay=%ss once=%s ignore_market_hours=%s",
        out.resolve(), ",".join(chosen) if chosen else "ALL", interval, delay, once, ignore_market_hours,
    )
    console.print(f"Writing CSVs to [bold]{out.resolve()}[/bold], log file [bold]{log_file.resolve()}[/bold]")

    try:
        if once:
            result = run_cycle_with_progress(collector)
            raise typer.Exit(code=1 if result.failed and not result.saved else 0)

        was_open: bool | None = None
        while True:
            start = time.monotonic()
            is_open = ignore_market_hours or market_open(datetime.now(IST))
            if is_open != was_open and not is_open:
                log.info("Market closed; waiting for Mon-Fri 09:15 IST")
            was_open = is_open
            if is_open:
                try:
                    run_cycle_with_progress(collector)
                except Exception:
                    log.exception("Cycle failed")
                wait = max(5.0, interval - (time.monotonic() - start))
                console.print(f"[dim]Next cycle in {wait:.0f}s (Ctrl+C to stop)[/dim]")
                time.sleep(wait)
            else:
                time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped by user")
