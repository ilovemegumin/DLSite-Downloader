from __future__ import annotations

import os
import queue
import re
import sys
import time
from copy import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from constants import (
    CHUNK_SIZE,
    DEFAULT_MAX_WORKERS,
    DLSITE_PLAY_BASE_URL,
    DLSITE_SIGN_BASE_URL,
    DOWNLOAD_HEADERS,
    USER_AGENT,
)

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None
    _HTTPX_AVAILABLE = False

try:
    import h2

    _HTTP2_AVAILABLE = True
except ImportError:
    h2 = None
    _HTTP2_AVAILABLE = False

HTTP2_ENABLED = False
_MULTI_CONN_ENABLED = False
_HTTP2_CLIENTS: dict[int, Any] = {}


class _NoProgress:
    total: int | None = None

    def update(self, _amount: int) -> None:
        return None

    def refresh(self) -> None:
        return None

    def close(self) -> None:
        return None


def set_http2(enabled: bool = True) -> None:
    global HTTP2_ENABLED
    if enabled and not _HTTPX_AVAILABLE:
        raise RuntimeError("httpx がインストールされていません。pip install httpx[http2] を実行してください")
    if enabled and not _HTTP2_AVAILABLE:
        raise RuntimeError("HTTP/2 サポートが有効ではありません。pip install httpx[http2] を実行してください")
    HTTP2_ENABLED = enabled


def set_multi_conn(enabled: bool = True) -> None:
    global _MULTI_CONN_ENABLED
    _MULTI_CONN_ENABLED = enabled


def configure_session_pool(
    session: requests.Session,
    max_retries: Any | None = None,
) -> requests.Session:
    adapter_kwargs: dict[str, Any] = {
        "pool_connections": DEFAULT_MAX_WORKERS * 2,
        "pool_maxsize": DEFAULT_MAX_WORKERS * 4,
    }
    if max_retries is not None:
        adapter_kwargs["max_retries"] = max_retries
    adapter = HTTPAdapter(**adapter_kwargs)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def clone_session(session: requests.Session) -> requests.Session:
    if not isinstance(session, requests.Session):
        return session
    cloned = requests.Session()
    cloned.headers.update(session.headers)
    for cookie in session.cookies:
        cloned.cookies.set_cookie(copy(cookie))
    return configure_session_pool(cloned)


def _normalize_httpx_headers(headers: Mapping[str, str | bytes]) -> dict[str, str]:
    return {
        name: value.decode("latin-1") if isinstance(value, bytes) else value
        for name, value in headers.items()
    }


def _get_httpx_client(session: requests.Session) -> Any:
    assert httpx is not None
    key = id(session)
    session_headers = _normalize_httpx_headers(session.headers)
    if key not in _HTTP2_CLIENTS:
        _HTTP2_CLIENTS[key] = httpx.Client(
            http2=True,
            headers=session_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(60.0),
        )
    client = _HTTP2_CLIENTS[key]
    client.headers.update(session_headers)
    client.cookies.clear()
    for cookie in session.cookies:
        client.cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain or "",
            path=cookie.path or "/",
        )
    return client


def close_http2_clients() -> None:
    for client in list(_HTTP2_CLIENTS.values()):
        client.close()
    _HTTP2_CLIENTS.clear()


def _default_session(session: requests.Session | None = None) -> requests.Session:
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return configure_session_pool(s)


def _retry_request(
    session: requests.Session,
    url: str,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 10.0,
    method: str = "GET",
    **kwargs: object,
) -> requests.Response:
    last_exc: Exception | None = None
    request_method = getattr(session, method.lower())
    for attempt in range(max_retries + 1):
        try:
            resp = request_method(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise last_exc
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code
            if status < 500 and status != 429:
                raise
            if attempt == max_retries:
                raise last_exc
        delay = min(initial_delay * (2**attempt), max_delay)
        time.sleep(delay)
    raise RuntimeError("Unexpected end of retry loop")


def _extract_split_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    division = soup.select_one("#download_division_file")
    if division is None:
        return []
    anchors = division.select(".work_download")
    urls: list[str] = []
    for anchor in anchors:
        href = anchor.get("href")
        if isinstance(href, str):
            urls.append(urljoin(base_url, href))
    return urls


def _extract_zip_url_from_html(html: str, base_url: str) -> str | None:
    matches = re.findall(r'href="([^"]+download\.dlsite\.com/get/[^"]+\.zip[^"]*)"', html)
    if matches:
        return urljoin(base_url, matches[0])
    matches = re.findall(
        r'https?://download\.dlsite\.com/get/[^\s"\'<>]+\.zip(?:[^\s"\'<>]*)', html
    )
    if matches:
        return matches[0]
    return None


def _filename_from_headers(resp: requests.Response, fallback: str) -> str:
    disposition = resp.headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^"]+)"?', disposition)
    if match:
        return match.group(1)
    return fallback


def get_download_urls(session: requests.Session, workno: str) -> list[str]:
    url = f"{DLSITE_SIGN_BASE_URL}/api/v3/download/sign/cookie?workno={workno}"
    resp = _retry_request(
        session,
        url,
        headers=DOWNLOAD_HEADERS,
        allow_redirects=False,
        timeout=60,
    )

    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type.startswith("text/html"):
        urls = _extract_split_urls(resp.text, resp.url)
        if not urls:
            raise RuntimeError("分割ダウンロードページから URL を抽出できませんでした")
        return urls

    data = resp.json()
    if isinstance(data, dict):
        signed_url = data.get("url")
        if isinstance(signed_url, str):
            return [signed_url]
    raise RuntimeError("API レスポンスにダウンロード URL が見つかりませんでした")


def get_download_urls_beta(session: requests.Session, workno: str) -> list[str]:
    url = f"{DLSITE_PLAY_BASE_URL}/api/v3/download?workno={workno}"
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Referer": f"{DLSITE_PLAY_BASE_URL}/work/{workno}",
        "User-Agent": USER_AGENT,
    }
    resp = _retry_request(
        session,
        url,
        headers=headers,
        allow_redirects=True,
        timeout=60,
        stream=True,
    )
    try:
        final_url = resp.url
        if final_url and ".zip" in final_url:
            return [final_url]

        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type.startswith("text/html"):
            urls = _extract_split_urls(resp.text, resp.url)
            if urls:
                return urls
            direct = _extract_zip_url_from_html(resp.text, resp.url)
            if direct:
                return [direct]
            raise RuntimeError("ベータAPIのHTMLから URL を抽出できませんでした")

        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("url", "download_url", "file_url", "file"):
                value = data.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return [value]
            urls = data.get("urls") or data.get("files") or []
            if isinstance(urls, list) and urls:
                return [u for u in urls if isinstance(u, str) and u.startswith("http")]
        raise RuntimeError("ベータAPIレスポンスにダウンロード URL が見つかりませんでした")
    finally:
        resp.close()


def _download_work_urls(
    session: requests.Session,
    workno: str,
    dest_dir: Path,
    urls: list[str],
    position: int,
    use_multi_conn: bool,
) -> list[Path]:
    paths: list[Path] = []
    for index, url in enumerate(urls):
        fallback = f"{workno}.part{index + 1}.zip" if len(urls) > 1 else f"{workno}.zip"
        dest_path = _unique_dest(dest_dir / fallback)
        file_desc = f"{workno} ({index + 1}/{len(urls)})"
        paths.append(
            download_file(
                session,
                url,
                dest_path,
                position=position,
                desc=file_desc,
                use_multi_conn=use_multi_conn,
            )
        )
    return paths


def _filename_from_response(
    resp: requests.Response | Any, fallback: str
) -> str:
    disposition = resp.headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^"]+)"?', disposition)
    if match:
        return match.group(1)
    return fallback


def _download_file_requests(
    session: requests.Session,
    url: str,
    dest_path: Path,
    position: int = 0,
    desc: str | None = None,
    file_size: int | None = None,
    disable_progress: bool = False,
) -> Path:
    progress = (
        _NoProgress()
        if disable_progress
        else tqdm(
            total=file_size if file_size else None,
            unit="B",
            unit_scale=True,
            desc=desc or dest_path.name,
            position=position,
            leave=False,
            dynamic_ncols=True,
        )
    )
    try:
        with _retry_request(
            session,
            url,
            stream=True,
            timeout=60,
            headers=DOWNLOAD_HEADERS,
        ) as resp:
            if file_size is None:
                total = resp.headers.get("Content-Length")
                if total:
                    progress.total = int(total)
                    progress.refresh()
            with dest_path.open("wb") as out:
                for raw_chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if raw_chunk:
                        chunk: bytes = raw_chunk
                        _ = out.write(chunk)
                        progress.update(len(chunk))
            actual_name = _filename_from_response(resp, dest_path.name)
            if actual_name != dest_path.name:
                actual_path = _unique_dest(dest_path.parent / actual_name)
                _ = dest_path.rename(actual_path)
                return actual_path
    finally:
        progress.close()
    return dest_path


def _download_file_httpx(
    session: requests.Session,
    url: str,
    dest_path: Path,
    position: int = 0,
    desc: str | None = None,
    file_size: int | None = None,
    disable_progress: bool = False,
) -> Path:
    assert httpx is not None
    client = _get_httpx_client(session)
    progress = (
        _NoProgress()
        if disable_progress
        else tqdm(
            total=file_size if file_size else None,
            unit="B",
            unit_scale=True,
            desc=desc or dest_path.name,
            position=position,
            leave=False,
            dynamic_ncols=True,
        )
    )
    try:
        with client.stream(
            "GET",
            url,
            headers=DOWNLOAD_HEADERS,
        ) as resp:
            resp.raise_for_status()
            if file_size is None:
                total = resp.headers.get("Content-Length")
                if total:
                    progress.total = int(total)
                    progress.refresh()
            with dest_path.open("wb") as out:
                for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                    if chunk:
                        _ = out.write(chunk)
                        progress.update(len(chunk))
            actual_name = _filename_from_response(resp, dest_path.name)
            if actual_name != dest_path.name:
                actual_path = _unique_dest(dest_path.parent / actual_name)
                _ = dest_path.rename(actual_path)
                return actual_path
    finally:
        progress.close()
    return dest_path


def _download_file_multiconn(
    session: requests.Session,
    url: str,
    dest_path: Path,
    position: int = 0,
    desc: str | None = None,
    num_connections: int = 4,
    disable_progress: bool = False,
) -> Path:
    resp = _retry_request(
        session,
        url,
        method="HEAD",
        timeout=60,
        headers=DOWNLOAD_HEADERS,
        allow_redirects=True,
    )
    accept_ranges = resp.headers.get("Accept-Ranges", "")
    if accept_ranges.lower() != "bytes":
        raise RuntimeError("サーバーが Range リクエストに対応していないためマルチコネクションを使用できません")

    total = resp.headers.get("Content-Length")
    if not total:
        raise RuntimeError("ファイルサイズが取得できないためマルチコネクションを使用できません")
    total_size = int(total)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    progress = (
        _NoProgress()
        if disable_progress
        else tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            desc=desc or dest_path.name,
            position=position,
            leave=False,
            dynamic_ncols=True,
        )
    )

    effective_connections = max(1, min(num_connections, total_size))
    chunk_size = (total_size + effective_connections - 1) // effective_connections
    ranges: list[tuple[int, int, Path]] = []
    tmp_dir = dest_path.parent / f"{dest_path.name}.parts"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for i in range(effective_connections):
        start = i * chunk_size
        if start >= total_size:
            break
        end = min(start + chunk_size - 1, total_size - 1)
        part_path = tmp_dir / f"part_{i:03d}"
        ranges.append((start, end, part_path))

    def _download_range(args: tuple[int, int, Path]) -> Path:
        start, end, part_path = args
        range_session = clone_session(session)
        headers = dict(DOWNLOAD_HEADERS)
        headers["Range"] = f"bytes={start}-{end}"
        with _retry_request(
            range_session,
            url,
            stream=True,
            timeout=60,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            if getattr(resp, "status_code", 206) != 206:
                raise RuntimeError("サーバーが Range リクエストを部分応答として処理しませんでした")
            with part_path.open("wb") as out:
                for raw_chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if raw_chunk:
                        chunk: bytes = raw_chunk
                        written = out.write(chunk)
                        progress.update(written)
        return part_path

    try:
        with ThreadPoolExecutor(max_workers=effective_connections) as executor:
            part_paths = list(executor.map(_download_range, ranges))

        with dest_path.open("wb") as out:
            for part_path in part_paths:
                with part_path.open("rb") as part_in:
                    while True:
                        chunk = part_in.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)

        actual_name = _filename_from_response(resp, dest_path.name)
        if actual_name != dest_path.name:
            actual_path = _unique_dest(dest_path.parent / actual_name)
            _ = dest_path.rename(actual_path)
            return actual_path
    finally:
        progress.close()
        if tmp_dir.exists():
            for part_path in tmp_dir.iterdir():
                part_path.unlink(missing_ok=True)
            tmp_dir.rmdir()

    return dest_path


def download_file(
    session: requests.Session,
    url: str,
    dest_path: Path,
    position: int = 0,
    desc: str | None = None,
    file_size: int | None = None,
    use_multi_conn: bool = False,
    disable_progress: bool = False,
) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if use_multi_conn and _MULTI_CONN_ENABLED:
        try:
            return _download_file_multiconn(
                session,
                url,
                dest_path,
                position=position,
                desc=desc,
                disable_progress=disable_progress,
            )
        except Exception as exc:
            print(f"[multi-conn] {exc} 通常ダウンロードにフォールバックします。")
    if HTTP2_ENABLED:
        return _download_file_httpx(
            session,
            url,
            dest_path,
            position=position,
            desc=desc,
            file_size=file_size,
            disable_progress=disable_progress,
        )
    return _download_file_requests(
        session,
        url,
        dest_path,
        position=position,
        desc=desc,
        file_size=file_size,
        disable_progress=disable_progress,
    )


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    parent = dest.parent
    stem = dest.stem
    suffix = dest.suffix
    idx = 1
    while True:
        candidate = parent / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def download_work(
    session: requests.Session,
    workno: str,
    dest_dir: Path,
    position: int = 0,
    use_beta_api: bool = False,
) -> list[Path]:
    from play_download import (
        fetch_download_token,
        is_play_content_url,
        download_play_work,
    )

    if use_beta_api:
        original_urls = get_download_urls_beta(session, workno)
        return _download_work_urls(
            session,
            workno,
            dest_dir,
            original_urls,
            position,
            use_multi_conn=True,
        )

    token = fetch_download_token(session, workno)
    if is_play_content_url(token.url):
        try:
            return download_play_work(session, token, workno, dest_dir, position)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 404:
                raise
            print(f"[play] {workno} の ziptree.json が見つかりません。通常/βダウンロードにフォールバックします。")
    urls = get_download_urls(session, workno)
    return _download_work_urls(
        session,
        workno,
        dest_dir,
        urls,
        position,
        use_multi_conn=False,
    )


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if ".zip" in path:
        idx = path.find(".zip")
        candidate = path[: idx + 4]
        name = Path(candidate).name
        if name.endswith(".zip"):
            return name
    return "download.zip"


def download_url(session: requests.Session | None, url: str, dest_dir: Path) -> Path:
    s = _default_session(session)
    dest_path = _unique_dest(dest_dir / _filename_from_url(url))
    return download_file(s, url, dest_path)


def download_works_parallel(
    session: requests.Session,
    worknos: list[str],
    dest_dir: Path,
    max_workers: int = DEFAULT_MAX_WORKERS,
    download_delay: float | None = None,
    use_beta_api: bool = False,
) -> list[Path]:
    from login import ensure_session_valid

    if download_delay is None:
        download_delay = float(os.environ.get("DLSITE_DOWNLOAD_DELAY", "0.2"))

    if use_beta_api:
        print("[original] オリジナル zip ダウンロードを使用します")

    if not ensure_session_valid(session):
        print("警告: セッションが無効です。ダウンロードを続行しますが、個別に失敗する可能性があります。", file=sys.stderr)

    dest_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    failures: list[tuple[str, str]] = []
    completed = 0
    total = len(worknos)

    position_pool: queue.Queue[int] = queue.Queue()
    for pos in range(max_workers):
        position_pool.put(pos)

    def _download_with_position(workno: str) -> list[Path]:
        pos = position_pool.get()
        try:
            worker_session = clone_session(session)
            paths = download_work(
                worker_session, workno, dest_dir, position=pos, use_beta_api=use_beta_api
            )
            time.sleep(download_delay)
            return paths
        finally:
            position_pool.put(pos)

    overall_position = max_workers
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_workno = {
            executor.submit(_download_with_position, workno): workno
            for workno in worknos
        }
        with tqdm(
            total=total,
            unit="work",
            desc="進捗",
            position=overall_position,
            leave=True,
            dynamic_ncols=True,
        ) as overall:
            for future in as_completed(future_to_workno):
                workno = future_to_workno[future]
                completed += 1
                try:
                    paths = future.result()
                    results.extend(paths)
                    overall.write(f"完了 ({completed}/{total}): {workno}")
                except Exception as exc:
                    failures.append((workno, str(exc)))
                    overall.write(f"失敗 ({completed}/{total}): {workno} - {exc}")
                overall.update(1)

    if failures:
        print(f"\n{len(failures)} 件のダウンロードに失敗しました:", file=sys.stderr)
        for workno, err in failures:
            print(f"  - {workno}: {err}", file=sys.stderr)

    return results
