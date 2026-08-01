from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

@dataclass
class HistoryEntry:
    workno: str
    timestamp: str
    file_paths: list[str] = field(default_factory=list)
    success: bool = True
    file_sizes: list[int] = field(default_factory=list)


def _history_path(config_dir: Path) -> Path:
    return config_dir / "download_history.json"


def load_history(config_dir: Path) -> dict[str, HistoryEntry]:
    path = _history_path(config_dir)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    history: dict[str, HistoryEntry] = {}
    if not isinstance(raw, dict):
        return {}
    for workno, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            history[workno] = HistoryEntry(
                workno=str(data.get("workno", workno)),
                timestamp=str(data["timestamp"]),
                file_paths=[str(p) for p in data.get("file_paths", [])],
                success=bool(data.get("success", True)),
                file_sizes=[int(s) for s in data.get("file_sizes", [])],
            )
        except (KeyError, TypeError, ValueError):
            continue
    return history


def save_history(config_dir: Path, history: dict[str, HistoryEntry]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {workno: asdict(entry) for workno, entry in history.items()}
    with _history_path(config_dir).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def record_download(
    config_dir: Path,
    workno: str,
    file_paths: list[Path],
    success: bool = True,
    file_sizes: list[int] | None = None,
) -> None:
    history = load_history(config_dir)
    history[workno] = HistoryEntry(
        workno=workno,
        timestamp=datetime.now(timezone.utc).isoformat(),
        file_paths=[p.as_posix() for p in file_paths],
        success=success,
        file_sizes=file_sizes if file_sizes is not None else [],
    )
    save_history(config_dir, history)


def is_downloaded(config_dir: Path, workno: str) -> bool:
    history = load_history(config_dir)
    entry = history.get(workno)
    return entry is not None and entry.success


def filter_already_downloaded(
    config_dir: Path, worknos: list[str]
) -> tuple[list[str], list[str]]:
    history = load_history(config_dir)
    to_download: list[str] = []
    already_downloaded: list[str] = []
    for workno in worknos:
        if workno in history and history[workno].success:
            already_downloaded.append(workno)
        else:
            to_download.append(workno)
    return to_download, already_downloaded


def clear_history(config_dir: Path) -> None:
    path = _history_path(config_dir)
    if path.exists():
        path.unlink()
