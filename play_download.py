
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from tqdm import tqdm

from constants import DLSITE_SIGN_BASE_URL, DOWNLOAD_HEADERS
import downloader
from scramble import descramble

logger = logging.getLogger(__name__)


@dataclass
class DownloadToken:

    url: str
    cookies: dict[str, str]
    expires: str


@dataclass
class PlayFileEntry:

    relative_path: str
    file_type: str
    optimized_name: str | None
    crypt: bool
    width: int | None
    height: int | None
    length: int


def is_play_content_url(url: str) -> bool:
    return url.endswith("/") and "/content/work/" in url


def fetch_download_token(session: requests.Session, workno: str) -> DownloadToken:
    url = f"{DLSITE_SIGN_BASE_URL}/api/v3/download/sign/cookie?workno={workno}"
    resp = downloader._retry_request(
        session,
        url,
        headers=DOWNLOAD_HEADERS,
        allow_redirects=False,
        timeout=60,
    )

    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type.startswith("text/html"):
        return DownloadToken(url=resp.url, cookies={}, expires="")

    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("API レスポンスが予期しない形式です")
    return DownloadToken(
        url=data["url"],
        cookies=data["cookies"],
        expires=data["expires"],
    )


def set_cloudfront_cookies(session: requests.Session, token: DownloadToken) -> None:
    for name, value in token.cookies.items():
        session.cookies.set(name, value, domain=".dlsite.com", path="/")


def fetch_ziptree(session: requests.Session, token: DownloadToken) -> dict[str, Any]:
    url = f"{token.url}ziptree.json"
    resp = downloader._retry_request(
        session,
        url,
        headers=DOWNLOAD_HEADERS,
        timeout=60,
    )
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("ziptree.json が予期しない形式です")
    return data


def _build_playfile_entry(
    hashname: str,
    name: str,
    parent: str,
    playfile_dict: dict[str, Any],
) -> PlayFileEntry | None:
    entry_data = playfile_dict.get(hashname)
    if entry_data is None:
        return None

    file_type = entry_data.get("type", "")
    type_data = entry_data.get(file_type) or {}
    optimized = type_data.get("optimized") or {}

    optimized_name = optimized.get("name") or type_data.get("name")
    if optimized_name is None:
        return None

    relative_path = _collapse_repeated_folder_sequences(
        f"{parent}/{name}" if parent else name
    )
    crypt = bool(optimized.get("crypt", False))
    width = int(optimized["width"]) if optimized.get("width") is not None else None
    height = int(optimized["height"]) if optimized.get("height") is not None else None
    length = int(
        optimized.get("length") or type_data.get("length") or entry_data.get("length", 0)
    )

    return PlayFileEntry(
        relative_path=relative_path,
        file_type=file_type,
        optimized_name=optimized_name,
        crypt=crypt,
        width=width,
        height=height,
        length=length,
    )


def _collapse_repeated_folder_sequences(relative_path: str) -> str:
    parts = [part for part in relative_path.split("/") if part]
    if len(parts) < 3:
        return "/".join(parts)

    folders = parts[:-1]

    def comparison_key(part: str) -> str:
        return unicodedata.normalize("NFC", part).casefold()

    start = 0
    while start < len(folders):
        remaining = len(folders) - start
        collapsed = False
        for width in range(1, remaining // 2 + 1):
            left = folders[start : start + width]
            right = folders[start + width : start + 2 * width]
            if [comparison_key(part) for part in left] == [
                comparison_key(part) for part in right
            ]:
                del folders[start + width : start + 2 * width]
                collapsed = True
                break
        if not collapsed:
            start += 1

    return "/".join([*folders, parts[-1]])


def _strip_root_folder(
    tree: list[dict[str, Any]], parent: str = ""
) -> tuple[list[dict[str, Any]], str]:
    if len(tree) != 1:
        return tree, parent
    root = tree[0]
    if root.get("type") != "folder":
        return tree, parent

    root_path = root.get("path", "")
    new_parent = f"{parent}/{root_path}" if parent else root_path
    children = root.get("children", [])

    if (
        len(children) == 1
        and children[0].get("type") == "folder"
        and children[0].get("path") == root_path
    ):
        return _strip_root_folder(children, parent=parent)

    return children, new_parent


def _walk_tree(
    tree: list[dict[str, Any]],
    playfile: dict[str, Any],
    parent: str = "",
) -> list[PlayFileEntry]:
    entries: list[PlayFileEntry] = []
    for entry in tree:
        entry_type = entry.get("type", "")
        path = entry.get("path", "")
        name = entry.get("name", "")
        if entry_type == "folder":
            children = entry.get("children", [])
            new_parent = f"{parent}/{path}" if parent else path
            entries.extend(_walk_tree(children, playfile, parent=new_parent))
        elif entry_type == "file":
            file_entry = _build_playfile_entry(
                hashname=entry.get("hashname", ""),
                name=name,
                parent=parent,
                playfile_dict=playfile,
            )
            if file_entry is not None:
                entries.append(file_entry)
    return entries


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _add_hls_query(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("v") == "00000000-0000-7000-8000-000000000000":
        return url
    query["v"] = "00000000-0000-7000-8000-000000000000"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _parse_m3u8_variants(playlist: str, base_url: str) -> list[tuple[str, str, int, int]]:
    variants: list[tuple[str, str, int, int]] = []
    lines = playlist.strip().splitlines()
    current_bandwidth = ""
    current_resolution = ""
    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            if "BANDWIDTH=" in line:
                bw = line.split("BANDWIDTH=")[1].split(",")[0].split("\"")[0]
                current_bandwidth = bw
            if "RESOLUTION=" in line:
                res = line.split("RESOLUTION=")[1].split(",")[0].split("\"")[0]
                current_resolution = res
        elif line and not line.startswith("#"):
            variant_url = _add_hls_query(urljoin(base_url, line))
            label = current_resolution or current_bandwidth or "unknown"
            height_score = 0
            bandwidth_score = int(current_bandwidth) if current_bandwidth.isdigit() else 0
            if current_resolution and "x" in current_resolution:
                try:
                    height_score = int(current_resolution.split("x")[1])
                except ValueError:
                    height_score = 0
            variants.append((variant_url, label, height_score, bandwidth_score))
            current_bandwidth = ""
            current_resolution = ""
    return variants


def _download_hls(
    session: requests.Session,
    playlist_url: str,
    output_path: Path,
    position: int = 0,
    desc: str | None = None,
    disable_progress: bool = False,
) -> Path:
    if not _has_ffmpeg():
        raise RuntimeError("HLS 動画の変換には ffmpeg が必要です")

    playlist_url = _add_hls_query(playlist_url)
    resp = downloader._retry_request(session, playlist_url, timeout=60)
    playlist = resp.text
    base_url = playlist_url.rsplit("/", 1)[0] + "/"

    if "#EXT-X-STREAM-INF" in playlist:
        variants = _parse_m3u8_variants(playlist, base_url)
        if not variants:
            raise RuntimeError("HLS バリアントが見つかりません")
        variants.sort(key=lambda x: (x[2], x[3]), reverse=True)
        playlist_url = variants[0][0]
        resp = downloader._retry_request(session, playlist_url, timeout=60)
        playlist = resp.text
        base_url = playlist_url.rsplit("/", 1)[0] + "/"

    tmp_dir = Path(tempfile.mkdtemp(prefix="dlsite_hls_"))
    try:
        local_playlist = tmp_dir / "playlist.m3u8"
        local_playlist.write_text(playlist, encoding="utf-8")

        segment_urls: list[tuple[int, str]] = []
        seg_idx = 0
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                segment_urls.append((seg_idx, _add_hls_query(urljoin(base_url, line))))
                seg_idx += 1

        if not segment_urls:
            raise RuntimeError("HLS セグメントが見つかりません")

        segment_files: list[Path] = []

        def _download_segment(args: tuple[int, str]) -> Path:
            idx, seg_url = args
            segment_session = downloader.clone_session(session)
            seg_path = tmp_dir / f"seg_{idx:05d}.ts"
            downloader.download_file(
                segment_session,
                seg_url,
                seg_path,
                position=position,
                desc=f"{desc or ''} seg{idx}",
                disable_progress=True,
            )
            return seg_path

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_segment = {
                executor.submit(_download_segment, segment): segment
                for segment in segment_urls
            }
            with tqdm(
                total=len(segment_urls),
                unit="seg",
                desc=f"{desc or output_path.name} HLS",
                position=position,
                leave=False,
                dynamic_ncols=True,
            ) as hls_progress:
                for future in as_completed(future_to_segment):
                    segment_files.append(future.result())
                    hls_progress.update(1)
        segment_files.sort(key=lambda p: int(p.stem.split("_")[1]))

        new_lines: list[str] = []
        seg_idx = 0
        for line in playlist.splitlines():
            line = line.rstrip()
            if line and not line.startswith("#"):
                new_lines.append(str(segment_files[seg_idx].name))
                seg_idx += 1
            else:
                new_lines.append(line)
        local_playlist.write_text("\n".join(new_lines), encoding="utf-8")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(local_playlist),
            "-c", "copy",
            str(output_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


def download_playfile(
    session: requests.Session,
    base_url: str,
    entry: PlayFileEntry,
    dest_path: Path,
    position: int = 0,
    desc: str | None = None,
    disable_progress: bool = False,
) -> Path:
    if entry.optimized_name is None:
        raise ValueError("optimized_name が None のファイルはダウンロードできません")

    if entry.optimized_name.endswith(".m3u8"):
        mp4_path = dest_path.with_suffix(".mp4")
        url = f"{base_url}optimized/{entry.optimized_name}"
        return _download_hls(
            session,
            url,
            mp4_path,
            position=position,
            desc=desc,
            disable_progress=disable_progress,
        )

    url_candidates = [
        f"{base_url}optimized/{entry.optimized_name}",
        f"{base_url}{entry.optimized_name}",
    ]
    last_exc: Exception | None = None
    for url in url_candidates:
        try:
            download_kwargs: dict[str, Any] = {
                "position": position,
                "desc": desc,
                "file_size": entry.length,
            }
            if disable_progress:
                download_kwargs["disable_progress"] = True
            result_path = downloader.download_file(session, url, dest_path, **download_kwargs)
            break
        except Exception as exc:
            last_exc = exc
            continue
    else:
        raise RuntimeError(
            f"{entry.relative_path} のダウンロードに失敗しました"
        ) from last_exc

    if entry.crypt and entry.width is not None and entry.height is not None:
        descramble(result_path, entry.optimized_name, entry.width, entry.height)

    return result_path


def download_play_work(
    session: requests.Session,
    token: DownloadToken,
    workno: str,
    dest_dir: Path,
    position: int = 0,
) -> list[Path]:
    set_cloudfront_cookies(session, token)

    ziptree = fetch_ziptree(session, token)
    tree, root_parent = _strip_root_folder(ziptree.get("tree", []))
    entries = _walk_tree(
        tree,
        ziptree.get("playfile", {}),
        parent=root_parent,
    )

    work_dir = dest_dir / workno
    work_dir.mkdir(parents=True, exist_ok=True)

    total_entries = len(entries)

    def _download_entry(args: tuple[int, PlayFileEntry]) -> Path:
        index, entry = args
        entry_session = downloader.clone_session(session)
        dest_path = work_dir / entry.relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        desc = f"{workno} ({index + 1}/{total_entries})"
        return download_playfile(
            entry_session,
            token.url,
            entry,
            dest_path,
            position=position,
            desc=desc,
            disable_progress=total_entries > 1,
        )

    if total_entries == 0:
        return []
    if total_entries == 1:
        return [_download_entry((0, entries[0]))]

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_entry = {
            executor.submit(_download_entry, item): item
            for item in enumerate(entries)
        }
        paths = []
        with tqdm(
            total=total_entries,
            unit="file",
            desc=workno,
            position=position,
            leave=False,
            dynamic_ncols=True,
        ) as work_progress:
            for future in as_completed(future_to_entry):
                paths.append(future.result())
                work_progress.update(1)
    return paths
