from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import requests
from constants import DLSITE_PLAY_BASE_URL

@dataclass
class WorkInfo:

    workno: str
    name: str
    maker_id: str
    maker_name: str
    work_type: str
    tags: list[str]
    content_length: str
    downloadable: bool
    sales_date: str


def _human_size(size_bytes: int) -> str:
    if size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.2f} {units[unit_index]}"


def _parse_work(raw: dict[str, Any], sales_date: str = "") -> WorkInfo:
    name_data: dict[str, Any] = raw.get("name") or {}
    name = name_data.get("ja_JP") or name_data.get("ja") or str(raw.get("workno", ""))

    maker_data: dict[str, Any] = raw.get("maker") or {}
    maker_id = str(maker_data.get("id", ""))
    maker_name_obj: dict[str, Any] = maker_data.get("name") or {}
    maker_name = maker_name_obj.get("ja_JP") or maker_name_obj.get("ja") or ""

    tags = [str(tag.get("name", "")) for tag in (raw.get("tags") or []) if tag.get("name")]

    return WorkInfo(
        workno=str(raw.get("workno", "")),
        name=name,
        maker_id=maker_id,
        maker_name=maker_name,
        work_type=str(raw.get("work_type", "")),
        tags=tags,
        content_length=_human_size(int(raw.get("content_length", 0) or 0)),
        downloadable=bool(raw.get("downloadable", False)),
        sales_date=sales_date,
    )


def fetch_works_metadata(
    session: requests.Session, worknos: list[str], batch_size: int = 100
) -> list[WorkInfo]:
    results: list[WorkInfo] = []
    if not worknos:
        return results

    url = f"{DLSITE_PLAY_BASE_URL}/api/v3/content/works"
    for start in range(0, len(worknos), batch_size):
        batch = worknos[start : start + batch_size]
        resp = session.post(url, json=batch, timeout=30)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        for raw in data.get("works", []):
            results.append(_parse_work(raw))

    return results


def fetch_all_purchases(session: requests.Session) -> list[WorkInfo]:
    count_resp = session.get(f"{DLSITE_PLAY_BASE_URL}/api/v3/content/count?last=0", timeout=30)
    count_resp.raise_for_status()
    count_data: dict[str, Any] = count_resp.json()
    total = int(count_data.get("user", 0))
    page_limit = int(count_data.get("page_limit", 50))

    sales_items: list[dict[str, Any]] = []
    last = 0
    while len(sales_items) < total:
        sales_resp = session.get(
            f"{DLSITE_PLAY_BASE_URL}/api/v3/content/sales?last={last}", timeout=30
        )
        sales_resp.raise_for_status()
        page: list[dict[str, Any]] = sales_resp.json()
        if not page:
            break
        sales_items.extend(page)
        last += page_limit

    worknos = [str(item.get("workno", "")) for item in sales_items if item.get("workno")]
    metadata = fetch_works_metadata(session, worknos, batch_size=100)

    sales_dates = {str(item.get("workno", "")): str(item.get("sales_date", "")) for item in sales_items}
    for info in metadata:
        info.sales_date = sales_dates.get(info.workno, info.sales_date)

    return metadata
