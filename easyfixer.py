
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath


PathValue = Path | PurePosixPath


@dataclass(frozen=True)
class Move:
    source: PathValue
    destination: PathValue
    size: int


def _comparison_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def collapse_repeated_folders(relative_path: PathValue) -> PathValue:
    parts = list(relative_path.parts)
    if len(parts) < 3:
        return relative_path

    folders = parts[:-1]
    start = 0
    while start < len(folders):
        remaining = len(folders) - start
        collapsed = False
        for width in range(1, remaining // 2 + 1):
            left = folders[start : start + width]
            right = folders[start + width : start + 2 * width]
            if list(map(_comparison_key, left)) == list(map(_comparison_key, right)):
                del folders[start + width : start + 2 * width]
                collapsed = True
                break
        if not collapsed:
            start += 1

    path_type = type(relative_path)
    return path_type(*folders, parts[-1])


def plan_moves(root: Path) -> list[Move]:
    moves: list[Move] = []
    for source in root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(root)
        destination = root / collapse_repeated_folders(relative)
        if destination != source:
            moves.append(Move(source, destination, source.stat().st_size))
    return moves


def plan_remote_moves(
    root: PurePosixPath, files: Mapping[PurePosixPath, int]
) -> list[Move]:
    moves: list[Move] = []
    for source, size in files.items():
        relative = source.relative_to(root)
        destination = root / collapse_repeated_folders(relative)
        if destination != source:
            moves.append(Move(source, destination, size))
    return moves


def _path_exists(path: PathValue) -> bool:
    return isinstance(path, Path) and path.exists()


def find_collisions(moves: list[Move]) -> dict[PathValue, list[PathValue]]:
    destinations: dict[tuple[str, ...], list[tuple[PathValue, PathValue]]] = {}
    for move in moves:
        key = _path_key(PurePath(move.destination))
        destinations.setdefault(key, []).append((move.destination, move.source))

    return {
        destination: [source for _, source in entries]
        for _, entries in destinations.items()
        if _path_exists(entries[0][0]) or len(entries) > 1
        for destination, _ in [entries[0]]
    }


def _path_key(path: PurePath) -> tuple[str, ...]:
    return tuple(_comparison_key(part) for part in path.parts)


def find_remote_collisions(
    moves: list[Move], existing_files: set[PurePosixPath]
) -> dict[PurePosixPath, list[PurePosixPath]]:
    existing_keys = {_path_key(path) for path in existing_files}
    destinations: dict[
        tuple[str, ...], tuple[PurePosixPath, list[PurePosixPath]]
    ] = {}
    for move in moves:
        source = PurePosixPath(move.source)
        destination = PurePosixPath(move.destination)
        key = _path_key(destination)
        if key not in destinations:
            destinations[key] = (destination, [])
        destinations[key][1].append(source)

    return {
        destination: sources
        for key, (destination, sources) in destinations.items()
        if key in existing_keys or len(sources) > 1
    }


class AdbClient:

    def __init__(self, executable: str = "adb") -> None:
        self.executable = executable

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")

    def _run(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [self.executable, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("adb が見つかりません。Android platform-tools を確認してください。") from exc
        if check and result.returncode != 0:
            detail = self._decode(result.stderr).strip() or self._decode(
                result.stdout
            ).strip()
            raise RuntimeError(f"ADBコマンドに失敗しました: {detail}")
        return result

    def verify_device(self) -> None:
        state = self._decode(self._run("get-state").stdout).strip()
        if state != "device":
            raise RuntimeError(f"ADB端末を利用できません: {state or '未接続'}")

    def is_directory(self, path: PurePosixPath) -> bool:
        command = f"test -d {shlex.quote(str(path))}"
        return self._run("shell", command, check=False).returncode == 0

    def list_files(self, root: PurePosixPath) -> dict[PurePosixPath, int]:
        command = f"find {shlex.quote(str(root))} -type f -print0"
        output = self._run("exec-out", command).stdout
        files: dict[PurePosixPath, int] = {}
        for raw_path in output.split(b"\0"):
            if not raw_path:
                continue
            path = PurePosixPath(raw_path.decode("utf-8", errors="replace"))
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"対象外のパスが返されました: {path}") from exc
            files[path] = 0
        return files

    def move(self, source: PurePosixPath, destination: PurePosixPath) -> None:
        source_arg = shlex.quote(str(source))
        destination_arg = shlex.quote(str(destination))
        parent_arg = shlex.quote(str(destination.parent))
        command = (
            f"test ! -e {destination_arg} && mkdir -p {parent_arg} "
            f"&& mv {source_arg} {destination_arg}"
        )
        self._run("shell", command)

    def rollback_move(
        self, source: PurePosixPath, destination: PurePosixPath
    ) -> None:
        source_arg = shlex.quote(str(source))
        destination_arg = shlex.quote(str(destination))
        parent_arg = shlex.quote(str(source.parent))
        command = (
            f"test -e {destination_arg} && test ! -e {source_arg} "
            f"&& mkdir -p {parent_arg} && mv {destination_arg} {source_arg}"
        )
        self._run("shell", command, check=False)

    def remove_empty_directories(self, root: PurePosixPath) -> int:
        command = f"find {shlex.quote(str(root))} -depth -type d -print0"
        output = self._run("exec-out", command).stdout
        directories = [
            PurePosixPath(value.decode("utf-8", errors="replace"))
            for value in output.split(b"\0")
            if value
        ]
        removed = 0
        for directory in directories:
            if directory == root:
                continue
            command = f"rmdir {shlex.quote(str(directory))}"
            if self._run("shell", command, check=False).returncode == 0:
                removed += 1
        return removed


def execute_remote_moves(client: AdbClient, moves: list[Move]) -> None:
    attempted: list[Move] = []
    try:
        for index, move in enumerate(moves, start=1):
            attempted.append(move)
            client.move(
                PurePosixPath(move.source), PurePosixPath(move.destination)
            )
            if index % 100 == 0 or index == len(moves):
                print(f"  ADB移動: {index}/{len(moves)}")
    except Exception:
        print(
            "ADB移動でエラーが発生したため、完了済みの移動を元に戻します。",
            file=sys.stderr,
        )
        for move in reversed(attempted):
            client.rollback_move(
                PurePosixPath(move.source), PurePosixPath(move.destination)
            )
        raise


def remove_empty_directories(root: Path) -> int:
    removed = 0
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def execute_moves(moves: list[Move]) -> None:
    completed: list[Move] = []
    try:
        for move in moves:
            source = Path(move.source)
            destination = Path(move.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            completed.append(Move(source, destination, move.size))
    except Exception:
        print("エラーが発生したため、完了済みの移動を元に戻します。", file=sys.stderr)
        for move in reversed(completed):
            if Path(move.destination).exists() and not Path(move.source).exists():
                Path(move.source).parent.mkdir(parents=True, exist_ok=True)
                Path(move.destination).replace(Path(move.source))
        raise


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A/B/A/B のように重複したフォルダ階層を A/B に整理します。"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="整理するフォルダ。省略すると対話入力します。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイルを移動せず、変更予定だけを表示します。",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="すべての移動予定を表示します。",
    )
    parser.add_argument(
        "--keep-empty-dirs",
        action="store_true",
        help="移動後に空フォルダを削除しません。",
    )
    parser.add_argument(
        "--adb",
        action="store_true",
        help="接続中のAndroid端末内の絶対パスを整理します。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    directory = args.directory
    if not directory:
        directory = input("整理するフォルダを入力してください: ").strip().strip('"')
    if not directory:
        print("フォルダが指定されていません。", file=sys.stderr)
        return 2

    if args.adb:
        root = PurePosixPath(directory)
        if not root.is_absolute() or ".." in root.parts:
            print("ADBでは端末内の安全な絶対パスを指定してください。", file=sys.stderr)
            return 2
        client = AdbClient()
        try:
            client.verify_device()
            if not client.is_directory(root):
                print(f"端末内フォルダが見つかりません: {root}", file=sys.stderr)
                return 2
            remote_files = client.list_files(root)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        moves = plan_remote_moves(root, remote_files)
        collisions = find_remote_collisions(moves, set(remote_files))
    else:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            print(f"フォルダが見つかりません: {root}", file=sys.stderr)
            return 2
        client = None
        moves = plan_moves(root)
        collisions = find_collisions(moves)

    if collisions:
        print("移動先が重複するため、何も変更しません。", file=sys.stderr)
        for destination, sources in collisions.items():
            print(f"  移動先: {destination}", file=sys.stderr)
            for source in sources:
                print(f"    元: {source}", file=sys.stderr)
        return 3

    total_size = sum(move.size for move in moves)
    print(f"対象: {root}")
    if args.adb:
        print(f"移動ファイル: {len(moves)} 個 (容量取得は省略)")
    else:
        print(f"移動ファイル: {len(moves)} 個 ({_format_size(total_size)})")
    print("上書き衝突: 0 個")

    if args.verbose:
        for move in moves:
            print(
                f"  {move.source.relative_to(root)}"
                f" -> {move.destination.relative_to(root)}"
            )

    if args.dry_run:
        print("ドライランのため、変更していません。")
        return 0
    if not moves:
        print("修正が必要な重複フォルダはありません。")
        return 0

    if args.adb:
        assert client is not None
        try:
            execute_remote_moves(client, moves)
            removed = (
                0
                if args.keep_empty_dirs
                else client.remove_empty_directories(PurePosixPath(root))
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 4
    else:
        assert isinstance(root, Path)
        execute_moves(moves)
        removed = 0 if args.keep_empty_dirs else remove_empty_directories(root)
    print(f"完了: {len(moves)} ファイルを移動、空フォルダ {removed} 個を削除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
