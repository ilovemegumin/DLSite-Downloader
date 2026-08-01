from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import requests

from constants import DLSITE_PLAY_BASE_URL
import downloader


def get_config_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    return Path.home() / ".config" / "dlsiteplay_downloader"


def _session_pickle_path(config_dir: Path) -> Path:
    return config_dir / "session.pkl"


def save_session(session: requests.Session, config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "cookies": list(session.cookies),
        "headers": dict(session.headers),
    }
    with _session_pickle_path(config_dir).open("wb") as f:
        pickle.dump(data, f)


def load_session(config_dir: Path) -> requests.Session | None:
    path = _session_pickle_path(config_dir)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            data: dict[str, Any] = pickle.load(f)
    except (pickle.PickleError, OSError):
        return None

    session = requests.Session()
    session.headers.update(data.get("headers", {}))
    for cookie in data.get("cookies", []):
        session.cookies.set_cookie(cookie)
    return downloader.configure_session_pool(session)


def session_is_valid(session: requests.Session) -> bool:
    try:
        resp = session.get(f"{DLSITE_PLAY_BASE_URL}/api/v3/content/count?last=0", timeout=30)
        return resp.status_code == 200 and "user" in resp.json()
    except (requests.RequestException, ValueError):
        return False
