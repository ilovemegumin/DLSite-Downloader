from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any
import questionary
from questionary import Choice
from api import WorkInfo

CATEGORIES = {
    "1": ("すべて", "all"),
    "2": ("マンガ・CG", {"MNG", "ICG", "WBT", "SCM", "DNV", "PBC", "VCM", "NRE"}),
    "3": ("音声", {"SOU", "MUS"}),
    "4": ("動画", {"MOV"}),
    "5": ("ゲーム", {"ACN", "ADV", "QIZ", "PZL", "RPG", "STG", "SLN", "TBL", "TYP", "ETC", "ET3"}),
}

def _parse_size(size_str: str) -> float:
    try:
        value_str, unit = size_str.split()
        return float(value_str) * {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    except (ValueError, AttributeError):
        return 0.0


SORT_OPTIONS: dict[str, tuple[str, Callable[[WorkInfo], str | float]]] = {
    "name": ("作品名順", lambda w: w.name.lower()),
    "workno": ("作品番号順", lambda w: w.workno),
    "date": ("購入日順", lambda w: w.sales_date),
    "size_desc": ("サイズ降順", lambda w: _parse_size(w.content_length)),
}


def sort_works(works: list[WorkInfo], sort_key: str) -> list[WorkInfo]:
    entry = SORT_OPTIONS.get(sort_key)
    if entry is None:
        return works
    _, key_func = entry
    return sorted(works, key=key_func, reverse=(sort_key == "size_desc"))


def filter_by_category(works: list[WorkInfo], category: str) -> list[WorkInfo]:
    entry = CATEGORIES.get(category)
    if entry is None:
        return works
    _, codes = entry
    if codes == "all":
        return works
    return [w for w in works if w.work_type in codes]


def search_works(
    works: list[WorkInfo],
    keyword: str,
    work_type_filter: str | None = None,
    downloadable_only: bool = False,
    date_range: tuple[str, str] | None = None,
) -> list[WorkInfo]:
    keyword = keyword.lower()
    results: list[WorkInfo] = []
    for work in works:
        haystack = " ".join(
            [
                work.name.lower(),
                work.maker_name.lower(),
                " ".join(tag.lower() for tag in work.tags),
            ]
        )
        if keyword and keyword not in haystack:
            continue
        if work_type_filter is not None and work.work_type != work_type_filter:
            continue
        if downloadable_only and not work.downloadable:
            continue
        if date_range is not None:
            start, end = date_range
            if work.sales_date < start or work.sales_date > end:
                continue
        results.append(work)
    return results


def parse_selection(input_str: str, total: int) -> list[int]:
    text = input_str.strip().lower()
    if text in ("q", ""):
        return []
    if text == "a":
        return list(range(total))

    indices: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, _, end_str = part.partition("-")
            try:
                start = int(start_str.strip()) - 1
                end = int(end_str.strip()) - 1
            except ValueError:
                continue
            if start < 0 or end >= total or start > end:
                continue
            indices.update(range(start, end + 1))
        else:
            try:
                idx = int(part) - 1
            except ValueError:
                continue
            if 0 <= idx < total:
                indices.add(idx)
    return sorted(indices)


def _format_line(index: int, work: WorkInfo, selected: bool, cursor: bool = False) -> str:
    marker = "[x]" if selected else "[ ]"
    cursor_prefix = "> " if cursor else "  "
    return f"{cursor_prefix}{marker} {index + 1:3}. {work.workno} [{work.work_type}] {work.content_length:>10} {work.name}"


def _read_key() -> str:
    try:
        import msvcrt
    except ImportError:
        return input().strip().lower()

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        if ch2 == "H":
            return "up"
        if ch2 == "P":
            return "down"
        if ch2 == "K":
            return "left"
        if ch2 == "M":
            return "right"
        return ""
    if ch == "\r" or ch == "\n":
        return "enter"
    if ch == " ":
        return "space"
    if ch == "\x1b":
        try:
            seq = msvcrt.getwch()
            if seq == "[":
                seq2 = msvcrt.getwch()
                if seq2 == "A":
                    return "up"
                if seq2 == "B":
                    return "down"
                if seq2 == "C":
                    return "right"
                if seq2 == "D":
                    return "left"
        except (KeyboardInterrupt, EOFError):
            pass
        return ""
    return ch.lower()


def _clear_screen() -> None:
    if os.name == "nt":
        _ = os.system("cls")
    else:
        _ = os.system("clear")


def _draw_page(
    works: list[WorkInfo],
    page: int,
    page_size: int,
    cursor: int,
    selected_global: set[str],
) -> None:
    total = len(works)
    total_pages = (total + page_size - 1) // page_size
    start = page * page_size
    end = min(start + page_size, total)
    page_works = works[start:end]

    _clear_screen()
    print("\n=== 作品選択 ===")
    print(f"ページ {page + 1}/{total_pages}  合計 {total} 件  選択中 {len(selected_global)} 件")
    print("-" * 110)
    for i, work in enumerate(page_works):
        idx = start + i
        print(_format_line(idx, work, work.workno in selected_global, cursor=i == cursor))
    print("-" * 110)
    print("操作: ↑/↓=移動  ←/→=前/次ページ  Space=選択/解除  a=全選択/解除  Enter=確定  q=キャンセル")


def _draw_nav_menu(has_next: bool, has_prev: bool) -> tuple[str, int]:
    options: list[tuple[str, str]] = []
    if has_next:
        options.append(("next", "▶ 次のページへ"))
    if has_prev:
        options.append(("prev", "◀ 前のページへ"))
    options.append(("done", "✓ 選択を確定"))
    options.append(("cancel", "✗ キャンセル"))

    cursor = 0
    while True:
        _clear_screen()
        print("\n=== 操作を選択 ===")
        print("-" * 40)
        for i, (value, label) in enumerate(options):
            prefix = "> " if i == cursor else "  "
            print(f"{prefix}{label}")
        print("-" * 40)
        print("操作: ↑/↓=移動  Enter=決定")

        key = _read_key()
        if key == "up":
            cursor = max(0, cursor - 1)
        elif key == "down":
            cursor = min(len(options) - 1, cursor + 1)
        elif key == "enter":
            return options[cursor][0], cursor


def _paginated_cli_select(works: list[WorkInfo], page_size: int = 20) -> list[str]:
    total = len(works)
    if total == 0:
        return []

    total_pages = (total + page_size - 1) // page_size
    page = 0
    cursor = 0
    selected_global: set[str] = set()

    while True:
        start = page * page_size
        end = min(start + page_size, total)
        page_works = works[start:end]

        _draw_page(works, page, page_size, cursor, selected_global)
        key = _read_key()

        if key == "q" or key == "c":
            return []
        if key == "up":
            cursor = max(0, cursor - 1)
        elif key == "down":
            cursor = min(len(page_works) - 1, cursor + 1)
        elif key == "left":
            if page > 0:
                page -= 1
                cursor = 0
        elif key == "right":
            if end < total:
                page += 1
                cursor = 0
        elif key == "space":
            workno = page_works[cursor].workno
            if workno in selected_global:
                selected_global.discard(workno)
            else:
                selected_global.add(workno)
        elif key == "a":
            page_values = {w.workno for w in page_works}
            if page_values <= selected_global:
                selected_global -= page_values
            else:
                selected_global |= page_values
        elif key == "n":
            if end < total:
                page += 1
                cursor = 0
        elif key == "p":
            if page > 0:
                page -= 1
                cursor = 0
        elif key == "enter":
            action, _ = _draw_nav_menu(end < total, page > 0)
            if action == "next":
                page += 1
                cursor = 0
            elif action == "prev":
                page -= 1
                cursor = 0
            elif action == "done":
                break
            else:
                return []

    return list(selected_global)


def interactive_library(works: list[WorkInfo]) -> list[str]:
    if not works:
        print("作品が見つかりませんでした。")
        return []

    category_choices = [
        Choice(title=f"{key}. {label}", value=key)
        for key, (label, _) in CATEGORIES.items()
    ]
    category = questionary.select(
        "カテゴリを選択してください:",
        choices=category_choices,
    ).ask()
    if category is None:
        return []

    filtered = filter_by_category(works, category)

    keyword = questionary.text("検索キーワード（省略可）:").ask()
    if keyword is None:
        return []
    keyword = keyword.strip()

    sort_choices = [
        Choice(title=label, value=key)
        for key, (label, _) in SORT_OPTIONS.items()
    ]
    sort_key = questionary.select(
        "並べ替え方法を選択してください:",
        choices=sort_choices,
    ).ask()
    if sort_key is None:
        return []

    downloadable_only = questionary.confirm(
        "ダウンロード可能な作品のみ表示しますか?",
        default=False,
    ).ask()
    if downloadable_only is None:
        return []

    work_types = sorted({w.work_type for w in filtered})
    work_type_choices = [Choice(title="すべて", value="")] + [
        Choice(title=wt, value=wt) for wt in work_types
    ]
    work_type_filter = questionary.select(
        "作品タイプで絞り込み:",
        choices=work_type_choices,
    ).ask()
    if work_type_filter is None:
        return []

    filtered = search_works(
        filtered,
        keyword,
        work_type_filter=work_type_filter or None,
        downloadable_only=downloadable_only,
    )
    filtered = sort_works(filtered, sort_key)

    if not filtered:
        print("該当する作品がありません。")
        return []

    return _paginated_cli_select(filtered)
