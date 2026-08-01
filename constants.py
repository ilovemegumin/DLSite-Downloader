from __future__ import annotations

import os
import tempfile

DLSITE_PLAY_BASE_URL = "https://play.dlsite.com"
DLSITE_SIGN_BASE_URL = "https://play.dl.dlsite.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.0 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0"
)
DOWNLOAD_HEADERS = {
    "Referer": f"{DLSITE_PLAY_BASE_URL}/",
    "Origin": DLSITE_PLAY_BASE_URL,
}
CHUNK_SIZE = 256 * 1024
DEFAULT_MAX_WORKERS = min(8, (os.cpu_count() or 4) * 2)
DEFAULT_WORK_DIR = tempfile.gettempdir()
