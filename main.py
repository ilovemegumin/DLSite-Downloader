from __future__ import annotations

import argparse
import http.cookiejar
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import questionary
import requests
from questionary import Choice

import downloader
from api import fetch_all_purchases
from constants import (
    DEFAULT_WORK_DIR,
    DLSITE_PLAY_BASE_URL,
    DLSITE_SIGN_BASE_URL,
    DOWNLOAD_HEADERS,
    USER_AGENT,
)
from downloader import download_url, download_works_parallel
from library import interactive_library
from login import LoginError, login_with_credentials, login_with_cookies, validate_session
from session import get_config_dir, load_session, save_session


def unique_dest_dir(dest: Path) -> Path:
    if not dest.exists():
        return dest
    parent = dest.parent
    name = dest.name
    idx = 1
    while True:
        candidate = parent / f"{name} ({idx})"
        if not candidate.exists():
            return candidate
        idx += 1


def _extract_zip_encoding(zf: zipfile.ZipFile, dest: Path) -> None:
    for info in zf.infolist():
        encoded_filename = info.filename
        try:
            decoded = encoded_filename.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            try:
                decoded = encoded_filename.encode("cp437").decode("cp932")
            except (UnicodeEncodeError, UnicodeDecodeError):
                decoded = encoded_filename
        info.filename = decoded
        if info.is_dir():
            (dest / decoded).mkdir(parents=True, exist_ok=True)
            continue
        target = dest / decoded
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


def extract_zip(zip_path: Path, dest: Path, dry_run: bool) -> bool:
    print(f"  解凍: {zip_path.name}")
    if dry_run:
        return True
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _extract_zip_encoding(zf, dest)
        return True
    except zipfile.BadZipFile as exc:
        print(f"  エラー: 壊れた zip です: {exc}", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"  エラー: 解凍に失敗しました: {exc}", file=sys.stderr)
        return False


def _is_workno_name(name: str) -> bool:
    return bool(re.fullmatch(r"^[BRV]J\d+", name, re.IGNORECASE))


def _organize_folder(folder: Path, output_dir: Path, dry_run: bool) -> None:
    children = [p for p in folder.iterdir() if p.name not in ("__MACOSX", ".DS_Store")]
    subdirs = [c for c in children if c.is_dir()]
    files = [c for c in children if c.is_file()]

    if len(subdirs) == 1:
        source = subdirs[0]
        if not dry_run:
            for file in files:
                dest_file = source / file.name
                _ = shutil.move(str(file), str(dest_file))
        dest = unique_dest_dir(output_dir / source.name)
        print(f"  移動: {folder.name}/{source.name} -> {dest}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.move(str(source), str(dest))
            try:
                folder.rmdir()
            except OSError:
                pass
    else:
        dest = unique_dest_dir(output_dir / folder.name)
        print(f"  移動: {folder.name} -> {dest}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.move(str(folder), str(dest))


def organize_directory(
    input_dir: Path, output_dir: Path, delete_zip: bool, dry_run: bool
) -> None:
    print(f"移動開始: {input_dir} -> {output_dir}")
    if not input_dir.exists():
        print(f"エラー: 入力ディレクトリが存在しません: {input_dir}", file=sys.stderr)
        return

    directories_before = {p.resolve() for p in input_dir.iterdir() if p.is_dir()}
    zips = sorted(input_dir.glob("*.zip"))
    if zips:
        print(f"発見した zip: {len(zips)} 個")
    extracted_zips: list[Path] = []
    for zip_path in zips:
        ok = extract_zip(zip_path, input_dir, dry_run)
        if ok:
            extracted_zips.append(zip_path)

    if delete_zip and extracted_zips:
        for zip_path in extracted_zips:
            print(f"  zip 削除: {zip_path.name}")
            if not dry_run:
                try:
                    zip_path.unlink()
                except OSError as exc:
                    print(f"  警告: zip の削除に失敗: {exc}", file=sys.stderr)

    candidate_folders = [
        p
        for p in input_dir.iterdir()
        if p.is_dir()
        and (
            _is_workno_name(p.name)
            or (not dry_run and p.resolve() not in directories_before)
        )
    ]
    if candidate_folders:
        print(f"移動対象フォルダ: {len(candidate_folders)} 個")
    else:
        print("移動対象のフォルダは見つかりませんでした。")

    for folder in sorted(candidate_folders, key=lambda p: p.name):
        _organize_folder(folder, output_dir, dry_run)

    print("移動完了")


def load_cookies(cookies_path: str) -> http.cookiejar.MozillaCookieJar:
    cj = http.cookiejar.MozillaCookieJar(cookies_path)
    cj.load(ignore_discard=True, ignore_expires=True)
    return cj


def _find_json_url(data: dict[str, Any]) -> str | None:
    keys = ("download_url", "url", "file_url", "file")
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _extract_zip_url_from_html(html: str, base_url: str) -> str | None:
    matches = re.findall(r'href="([^"]+download\.dlsite\.com/get/[^"]+\.zip[^"]*)"', html)
    if matches:
        return urljoin(base_url, matches[0])
    matches = re.findall(r'https?://download\.dlsite\.com/get/[^\s"\'<>]+\.zip(?:[^\s"\'<>]*)', html)
    if matches:
        return matches[0]
    return None


def _handle_download_redirect(
    session: requests.Session, location: str, html: str | None = None
) -> tuple[str, str | None]:
    page_url = urljoin(DLSITE_PLAY_BASE_URL, location)
    if "/download/=" in location:
        print(f"直接ダウンロードページにリダイレクト: {page_url}")
        page_html = html
        page_resp_url = page_url
        if page_html is None:
            page_resp = session.get(page_url, headers=DOWNLOAD_HEADERS, timeout=60)
            page_html = page_resp.text
            page_resp_url = page_resp.url
        zip_url = _extract_zip_url_from_html(page_html, page_resp_url)
        if zip_url:
            return zip_url, None
        raise RuntimeError("ダウンロードページから zip URL を抽出できませんでした")
    if "/split/" in location:
        raise RuntimeError(f"分割ダウンロードが必要です: {page_url}")
    if "/serial/" in location:
        raise RuntimeError(f"シリアル番号が必要です: {page_url}")
    raise RuntimeError(f"未対応のリダイレクトです: {page_url}")


def resolve_download_url(
    session: requests.Session, workno: str
) -> tuple[str, str | None]:
    sign_url = f"{DLSITE_SIGN_BASE_URL}/api/v3/download/sign/cookie?workno={workno}"
    print(f"DLsite API 確認: {sign_url}")
    resp = session.get(sign_url, headers=DOWNLOAD_HEADERS, allow_redirects=False, timeout=60)

    if resp.status_code == 302:
        location = resp.headers.get("Location", "")
        return _handle_download_redirect(session, location)

    if resp.status_code == 200:
        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type.startswith("text/html"):
            location = resp.url
            return _handle_download_redirect(session, location, html=resp.text)

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("API から予期しない応答が返されました")
        url = _find_json_url(data)
        if url:
            return url, None
        raise RuntimeError("API レスポンスにダウンロード URL が見つかりませんでした")

    raise RuntimeError(f"API リクエストが失敗しました: HTTP {resp.status_code}")


def download_file(
    session: requests.Session, url: str, dest_path: Path, dry_run: bool
) -> Path | None:
    print(f"ダウンロード: {url}")
    print(f"保存先: {dest_path}")
    if dry_run:
        return None
    return downloader.download_file(session, url, dest_path)


def _read_list_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"リストファイルが見つかりません: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _build_session(cookies_path: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if cookies_path:
        cj = load_cookies(cookies_path)
        session.cookies.update(cj)
    return downloader.configure_session_pool(session)


def _load_or_require_session(args: argparse.Namespace) -> requests.Session:
    config_dir_value = getattr(args, "config_dir", None)
    config_dir = get_config_dir(Path(config_dir_value) if config_dir_value else None)
    if args.cookies:
        session = login_with_cookies(args.cookies)
    else:
        session = load_session(config_dir)
        if session is None:
            raise LoginError("セッションがありません。login サブコマンドでログインするか、--cookies を指定してください")
    if not validate_session(session):
        raise LoginError("セッションが無効です。再ログインしてください")
    return session


def _resolve_max_workers(args: argparse.Namespace) -> int:
    max_workers = getattr(args, "max_workers", None)
    if max_workers is not None:
        return max(1, max_workers)
    return max(1, int(os.environ.get("DLSITE_MAX_WORKERS", downloader.DEFAULT_MAX_WORKERS)))


def _configure_download_options(args: argparse.Namespace) -> None:
    use_http2 = bool(getattr(args, "http2", False))
    use_beta = bool(getattr(args, "beta", False))
    use_multi_conn = bool(getattr(args, "multi_conn", False))
    downloader.set_http2(use_http2)
    if use_multi_conn and not use_beta:
        print("警告: --multi-conn は --beta 指定時のみ有効です。無効化します。", file=sys.stderr)
        use_multi_conn = False
    downloader.set_multi_conn(use_multi_conn)


def cmd_organize(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    organize_directory(input_dir, output_dir, args.delete_zip, args.dry_run)
    return 0


def _download_targets(args: argparse.Namespace) -> list[str]:
    targets: list[str] = []
    if args.workno:
        targets.append(args.workno)
    if args.url:
        targets.append(args.url)
    targets.extend(getattr(args, "targets", []) or [])
    list_path = getattr(args, "list", None)
    if list_path:
        targets.extend(_read_list_file(Path(list_path)))
    return targets


def cmd_download(args: argparse.Namespace) -> int:
    _configure_download_options(args)
    targets = _download_targets(args)
    cookies_path: str | None = args.cookies
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not targets:
        print("エラー: --workno、--url、対象リスト、または --list を指定してください", file=sys.stderr)
        return 2
    if args.workno and args.url:
        print("エラー: --workno と --url は同時に指定できません", file=sys.stderr)
        return 2

    has_workno = any(_is_workno_name(t) for t in targets)
    if has_workno and not cookies_path:
        config_dir_value = getattr(args, "config_dir", None)
        config_dir = get_config_dir(Path(config_dir_value) if config_dir_value else None)
        if load_session(config_dir) is None:
            print("エラー: --workno 使用時は --cookies が必須です", file=sys.stderr)
            return 2
        session = _load_or_require_session(args)
    else:
        session = _build_session(cookies_path)

    if args.dry_run:
        for target in targets:
            print(f"[dry-run] ダウンロード対象: {target}")
        return 0

    worknos = [t for t in targets if _is_workno_name(t)]
    urls = [t for t in targets if not _is_workno_name(t)]
    max_workers = _resolve_max_workers(args)
    use_beta_api = bool(getattr(args, "beta", False))

    auto_yes = bool(getattr(args, "yes", False))
    if not _confirm_download(
        worknos, urls, max_workers, use_beta_api=use_beta_api, auto_yes=auto_yes
    ):
        print("ダウンロードをキャンセルしました。")
        return 0

    if worknos:
        _ = download_works_parallel(
            session,
            worknos,
            input_dir,
            max_workers=max_workers,
            download_delay=getattr(args, "download_delay", None),
            use_beta_api=use_beta_api,
        )
    for url in urls:
        try:
            _ = download_url(session, url, input_dir)
        except Exception as exc:
            print(f"エラー: {url} のダウンロードに失敗しました: {exc}", file=sys.stderr)
            continue

    if not args.no_organize:
        _ = organize_directory(input_dir, output_dir, delete_zip=True, dry_run=False)

    return 0


def cmd_login(args: argparse.Namespace) -> int:
    config_dir = get_config_dir(Path(args.config_dir) if args.config_dir else None)
    try:
        if args.cookies:
            session = login_with_cookies(args.cookies)
        elif args.username and args.password:
            session = login_with_credentials(args.username, args.password)
        else:
            print("エラー: --username/--password または --cookies を指定してください", file=sys.stderr)
            return 2

        if not validate_session(session):
            print("エラー: セッションの検証に失敗しました", file=sys.stderr)
            return 1

        save_session(session, config_dir)
        print(f"ログイン成功。セッションを保存しました: {config_dir}")
        return 0
    except LoginError as exc:
        print(f"ログインエラー: {exc}", file=sys.stderr)
        return 1


def _human_readable_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = float(size_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} PB"


def _confirm_download(
    worknos: list[str],
    urls: list[str],
    max_workers: int,
    use_beta_api: bool = False,
    auto_yes: bool = False,
) -> bool:
    print("\n===== ダウンロード確認 =====")
    print(f"並列数: {max_workers}")
    if use_beta_api:
        print("[original] オリジナル zip ダウンロードを使用します")
    for workno in worknos:
        print(f"  [work] {workno}")
    for url in urls:
        print(f"  [url]  {url}")
    print(f"合計作品数: {len(worknos)} 件")
    if urls:
        print(f"合計直接URL数: {len(urls)} 件")
    print("============================\n")
    if auto_yes:
        print("--yes 指定のため確認をスキップします")
        return True
    try:
        return bool(questionary.confirm("これらをダウンロードしますか？", default=False).ask())
    except Exception:
        answer = input("これらをダウンロードしますか？ [y/N]: ").strip().lower()
        return answer in ("y", "yes")


def _download_worknos(
    session: requests.Session,
    worknos: list[str],
    input_dir: Path,
    output_dir: Path,
    max_workers: int,
    delete_zip: bool,
    no_organize: bool,
    dry_run: bool,
    use_beta_api: bool = False,
    auto_yes: bool = False,
    download_delay: float | None = None,
) -> None:
    if dry_run:
        for workno in worknos:
            print(f"[dry-run] ダウンロード対象: {workno}")
        return

    if not _confirm_download(
        worknos, [], max_workers, use_beta_api=use_beta_api, auto_yes=auto_yes
    ):
        print("ダウンロードをキャンセルしました。")
        return

    _ = download_works_parallel(
        session,
        worknos,
        input_dir,
        max_workers=max_workers,
        download_delay=download_delay,
        use_beta_api=use_beta_api,
    )

    if not no_organize:
        _ = organize_directory(
            input_dir, output_dir, delete_zip=delete_zip, dry_run=False
        )


def cmd_library(args: argparse.Namespace) -> int:
    _configure_download_options(args)
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if args.cookies:
        session = login_with_cookies(args.cookies)
    else:
        session = _load_or_require_session(args)

    print("ライブラリを取得中...")
    works = fetch_all_purchases(session)
    print(f"購入済み作品: {len(works)} 件")

    selected = interactive_library(works)
    if not selected:
        print("ダウンロードする作品が選択されませんでした。")
        return 0

    max_workers = _resolve_max_workers(args)
    _download_worknos(
        session,
        selected,
        input_dir,
        output_dir,
        max_workers=max_workers,
        delete_zip=args.delete_zip,
        no_organize=args.no_organize,
        dry_run=args.dry_run,
        use_beta_api=bool(getattr(args, "beta", False)),
        auto_yes=bool(getattr(args, "yes", False)),
        download_delay=getattr(args, "download_delay", None),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dlsite-play-downloader",
        description="DLsite Play 用のダウンロード・移動 CLI",
    )
    parser.add_argument(
        "--config-dir",
        help="設定ディレクトリ（既定: ~/.config/dlsiteplay_downloader）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        help="並列ダウンロード数（既定: 8 または環境変数 DLSITE_MAX_WORKERS）",
    )
    parser.add_argument(
        "--download-delay",
        type=float,
        dest="download_delay",
        help="作品ダウンロード間の待ち秒数（既定: 0.2 または環境変数 DLSITE_DOWNLOAD_DELAY）",
    )
    parser.add_argument(
        "--beta",
        "--original",
        dest="beta",
        action="store_true",
        help="ブラウザーと同じ経路でオリジナル zip を取得する",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        help="HTTP/2 を使用してダウンロードする（httpx[http2] が必要）",
    )
    parser.add_argument(
        "--multi-conn",
        action="store_true",
        dest="multi_conn",
        help="オリジナル zip を複数コネクションで並列ダウンロードする",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="ダウンロード前の確認をスキップする",
    )
    parser.set_defaults(input_dir=DEFAULT_WORK_DIR, output_dir="downloaded")

    subparsers = parser.add_subparsers(dest="command")

    organize_parser = subparsers.add_parser("organize", help="zip を解凍して作品フォルダを移動する")
    organize_parser.add_argument(
        "-i", "--input-dir", default=DEFAULT_WORK_DIR, help="移動対象ディレクトリ（既定: OS の一時ディレクトリ）"
    )
    organize_parser.add_argument(
        "-o", "--output-dir", default="downloaded", help="移動先ディレクトリ（既定: downloaded）"
    )
    organize_parser.add_argument(
        "--delete-zip", action="store_true", help="展開後に zip を削除する"
    )
    organize_parser.add_argument(
        "--dry-run", action="store_true", help="実行せず表示のみ"
    )
    organize_parser.set_defaults(func=cmd_organize)

    login_parser = subparsers.add_parser("login", help="DLsite Play にログインしてセッションを保存する")
    login_parser.add_argument("--username", help="DLsite ログイン ID")
    login_parser.add_argument("--password", help="DLsite ログインパスワード")
    login_parser.add_argument("--cookies", help="Netscape 形式 cookies.txt のパス")
    login_parser.set_defaults(func=cmd_login)

    library_parser = subparsers.add_parser("library", help="ライブラリを閲覧してバッチダウンロードする")
    library_parser.add_argument(
        "-i", "--input-dir", default=DEFAULT_WORK_DIR, help="ダウンロード先/移動元ディレクトリ（既定: OS の一時ディレクトリ）"
    )
    library_parser.add_argument(
        "-o", "--output-dir", default="downloaded", help="移動先ディレクトリ（既定: downloaded）"
    )
    library_parser.add_argument("--cookies", help="Netscape 形式 cookies.txt のパス")
    library_parser.add_argument(
        "--dry-run", action="store_true", help="ダウンロード対象を表示のみ"
    )
    library_parser.add_argument(
        "--no-organize", action="store_true", help="ダウンロード後に移動しない"
    )
    library_parser.add_argument(
        "--delete-zip", action="store_true", help="展開後に zip を削除する"
    )
    library_parser.set_defaults(func=cmd_library)

    download_parser = subparsers.add_parser("download", help="DLsite Play から作品をダウンロードする")
    download_parser.add_argument("--workno", help="作品番号（例: RJ123456）")
    download_parser.add_argument("--url", help="直接ダウンロード URL")
    download_parser.add_argument("targets", nargs="*", help="作品番号または URL（複数可）")
    download_parser.add_argument("--list", dest="list", help="1 行 1 件のリストファイル")
    download_parser.add_argument("--cookies", help="Netscape 形式 cookies.txt のパス")
    download_parser.add_argument(
        "-i", "--input-dir", default=DEFAULT_WORK_DIR, help="ダウンロード先/移動元ディレクトリ（既定: OS の一時ディレクトリ）"
    )
    download_parser.add_argument(
        "-o", "--output-dir", default="downloaded", help="移動先ディレクトリ（既定: downloaded）"
    )
    download_parser.add_argument(
        "--no-organize", action="store_true", help="ダウンロード後に移動しない"
    )
    download_parser.add_argument(
        "--dry-run", action="store_true", help="URL 解決のみ行う"
    )
    download_parser.set_defaults(func=cmd_download)

    return parser


def _prompt_cookies() -> str | None:
    path = questionary.text("cookies.txt のパス（省略可）:").ask()
    if not path:
        return None
    return path.strip()


def _prompt_workdir(default: str = ".") -> Path:
    path = questionary.text("作業ディレクトリ:", default=default).ask()
    if not path:
        path = default
    return Path(path).resolve()


def _prompt_outputdir(default: str = "downloaded") -> Path:
    path = questionary.text("出力ディレクトリ:", default=default).ask()
    if not path:
        path = default
    return Path(path).resolve()


def _prompt_targets() -> list[str]:
    text = questionary.text("作品番号または URL（カンマ/スペース区切り）:").ask()
    if not text:
        return []
    targets: list[str] = []
    for part in re.split(r"[\s,]+", text):
        part = part.strip()
        if part:
            targets.append(part)
    return targets


def _ensure_session_interactive(args: argparse.Namespace) -> requests.Session:
    config_dir = get_config_dir(Path(args.config_dir) if args.config_dir else None)
    session = load_session(config_dir)
    if session is not None and validate_session(session):
        print("保存済みセッションを使用します。")
        return session
    cookies_path = _prompt_cookies()
    if cookies_path:
        session = login_with_cookies(cookies_path)
        save_session(session, config_dir)
        return session
    raise LoginError("ログイン情報が必要です。")


def _interactive_download(session: requests.Session, args: argparse.Namespace) -> int:
    _configure_download_options(args)
    targets = _prompt_targets()
    if not targets:
        print("対象が入力されませんでした。")
        return 0
    input_dir = _prompt_workdir(args.input_dir)
    output_dir = _prompt_outputdir(args.output_dir)
    organize = questionary.confirm("ダウンロード後に移動しますか？", default=True).ask()
    no_organize = not organize
    dry_run = questionary.confirm("ドライランしますか？", default=False).ask()

    if dry_run:
        for target in targets:
            print(f"[dry-run] ダウンロード対象: {target}")
        return 0

    worknos = [t for t in targets if _is_workno_name(t)]
    urls = [t for t in targets if not _is_workno_name(t)]
    max_workers = _resolve_max_workers(args)
    use_beta_api = bool(getattr(args, "beta", False))

    auto_yes = bool(getattr(args, "yes", False))
    if not _confirm_download(
        worknos, urls, max_workers, use_beta_api=use_beta_api, auto_yes=auto_yes
    ):
        print("ダウンロードをキャンセルしました。")
        return 0

    if worknos:
        _ = download_works_parallel(
            session,
            worknos,
            input_dir,
            max_workers=max_workers,
            download_delay=getattr(args, "download_delay", None),
            use_beta_api=use_beta_api,
        )
    for url in urls:
        try:
            _ = download_url(session, url, input_dir)
        except Exception as exc:
            print(f"エラー: {url} のダウンロードに失敗しました: {exc}", file=sys.stderr)
            continue

    if not no_organize:
        _ = organize_directory(input_dir, output_dir, delete_zip=True, dry_run=False)
    return 0


def _interactive_library(args: argparse.Namespace) -> int:
    _configure_download_options(args)
    session = _ensure_session_interactive(args)
    input_dir = _prompt_workdir(args.input_dir)
    output_dir = _prompt_outputdir(args.output_dir)
    print("ライブラリを取得中...")
    works = fetch_all_purchases(session)
    print(f"購入済み作品: {len(works)} 件")
    selected = interactive_library(works)
    if not selected:
        print("ダウンロードする作品が選択されませんでした。")
        return 0
    max_workers = _resolve_max_workers(args)
    _download_worknos(
        session,
        selected,
        input_dir,
        output_dir,
        max_workers=max_workers,
        delete_zip=True,
        no_organize=False,
        dry_run=False,
        auto_yes=False,
        use_beta_api=bool(getattr(args, "beta", False)),
        download_delay=getattr(args, "download_delay", None),
    )
    return 0


def _interactive_organize(args: argparse.Namespace) -> int:
    input_dir = _prompt_workdir(args.input_dir)
    output_dir = _prompt_outputdir(args.output_dir)
    delete_zip = questionary.confirm(
        "zip を削除しますか？", default=False
    ).ask()
    dry_run = questionary.confirm(
        "ドライランしますか？", default=False
    ).ask()
    organize_directory(input_dir, output_dir, delete_zip=bool(delete_zip), dry_run=bool(dry_run))
    return 0


def _interactive_login(args: argparse.Namespace) -> int:
    config_dir = get_config_dir(Path(args.config_dir) if args.config_dir else None)
    cookies_path = _prompt_cookies()
    if cookies_path:
        session = login_with_cookies(cookies_path)
    else:
        username = questionary.text("DLsite ログイン ID:").ask()
        password = questionary.password("DLsite ログインパスワード:").ask()
        if not username or not password:
            print("エラー: ID とパスワードを入力してください", file=sys.stderr)
            return 2
        session = login_with_credentials(username, password)

    if not validate_session(session):
        print("エラー: セッションの検証に失敗しました", file=sys.stderr)
        return 1

    save_session(session, config_dir)
    print(f"ログイン成功。セッションを保存しました: {config_dir}")
    return 0


def run_interactive_menu(args: argparse.Namespace) -> int:
    choices = [
        Choice("1. ログイン", value="login"),
        Choice("2. ライブラリからダウンロード", value="library"),
        Choice("3. 作品番号/URL でダウンロード", value="download"),
        Choice("4. ダウンロード済みファイルを移動", value="organize"),
        Choice("5. 終了", value="quit"),
    ]
    while True:
        print("\n=== DLsite Play Downloader ===")
        print("\n          By h_ypi")
        choice = questionary.select("選択:", choices=choices).ask()
        if choice is None or choice == "quit":
            print("終了します。")
            return 0
        if choice == "login":
            _interactive_login(args)
        elif choice == "library":
            _interactive_library(args)
        elif choice == "download":
            try:
                session = _ensure_session_interactive(args)
                _interactive_download(session, args)
            except LoginError as exc:
                print(f"ログインエラー: {exc}", file=sys.stderr)
        elif choice == "organize":
            _interactive_organize(args)
        else:
            print("無効な選択です。")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            return run_interactive_menu(args)
        return args.func(args)
    finally:
        downloader.close_http2_clients()


if __name__ == "__main__":
    sys.exit(main())
