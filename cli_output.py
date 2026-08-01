from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text
from api import WorkInfo

console = Console()
err_console = Console(stderr=True)


def info(msg: str) -> None:
    console.print(Text("[INFO] ", style="cyan"), msg, sep="")


def success(msg: str) -> None:
    console.print(Text("[OK] ", style="green"), msg, sep="")


def warning(msg: str) -> None:
    console.print(Text("[WARN] ", style="yellow"), msg, sep="")


def error(msg: str, hint: str | None = None) -> None:
    err_console.print(Text("[ERROR] ", style="red"), msg, sep="")
    if hint is not None:
        err_console.print(Panel(hint, title="ヒント", border_style="yellow"))


def progress_bar() -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def dry_run_table(rows: list[dict[str, object]]) -> Table:
    table = Table(title="ドライラン対象作品")
    table.add_column("作品番号", style="cyan")
    table.add_column("作品名")
    table.add_column("種類")
    table.add_column("サイズ")
    table.add_column("URL")
    table.add_column("状態")

    for row in rows:
        table.add_row(
            str(row.get("workno", "")),
            str(row.get("name", "")),
            str(row.get("work_type", "")),
            str(row.get("size", "")),
            str(row.get("url", "")),
            str(row.get("status", "")),
        )

    return table


def works_table(works: list[WorkInfo]) -> Table:
    table = Table(title="購入済み作品一覧")
    table.add_column("作品番号", style="cyan")
    table.add_column("作品名")
    table.add_column("サークル")
    table.add_column("種類")
    table.add_column("サイズ")
    table.add_column("販売日")

    for work in works:
        table.add_row(
            work.workno,
            work.name,
            work.maker_name,
            work.work_type,
            work.content_length,
            work.sales_date,
        )

    return table