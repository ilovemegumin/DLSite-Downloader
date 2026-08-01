from __future__ import annotations

import http.cookiejar
import re
from typing import Any
import requests
from urllib3.util.retry import Retry
from constants import DLSITE_PLAY_BASE_URL
import downloader

class LoginError(Exception):
    pass

def _create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
    )
    return downloader.configure_session_pool(session, max_retries=retries)


def _get_login_token(session: requests.Session) -> str:
    resp = session.get("https://login.dlsite.com/login", timeout=30)
    resp.raise_for_status()

    xsrf = session.cookies.get("XSRF-TOKEN")
    if not xsrf:
        raise LoginError("XSRF-TOKEN の取得に失敗しました")

    match = re.search(r'name="_token" value="([^"]+)"', resp.text)
    if not match:
        raise LoginError("ログイントークンが見つかりませんでした")
    return match.group(1)


def _post_login(
    session: requests.Session, login_id: str, password: str, token: str
) -> None:
    data = {
        "_token": token,
        "login_id": login_id,
        "password": password,
    }
    resp = session.post("https://login.dlsite.com/login", data=data, timeout=30)
    resp.raise_for_status()

    if "PHPSESSID" not in [c.name for c in session.cookies]:
        raise LoginError("ログインに失敗しました。ID またはパスワードを確認してください")


def _authorize_play(session: requests.Session) -> None:
    mypage_resp = session.get("https://ssl.dlsite.com/home/mypage", timeout=30)
    mypage_resp.raise_for_status()

    session.cookies.set("adultchecked", "1", domain=".dlsite.com")

    play_resp = session.get(f"{DLSITE_PLAY_BASE_URL}/", timeout=30)
    play_resp.raise_for_status()

    login_resp = session.get(f"{DLSITE_PLAY_BASE_URL}/login/", allow_redirects=True, timeout=30)
    login_resp.raise_for_status()

    auth_resp = session.get(
        f"{DLSITE_PLAY_BASE_URL}/api/authorize",
        headers={"Referer": f"{DLSITE_PLAY_BASE_URL}/"},
        timeout=30,
    )
    auth_resp.raise_for_status()


def login_with_credentials(username: str, password: str) -> requests.Session:
    session = _create_session()
    token = _get_login_token(session)
    _post_login(session, username, password, token)
    _authorize_play(session)
    return session


def login_with_cookies(cookies_path: str) -> requests.Session:
    session = _create_session()
    cj = http.cookiejar.MozillaCookieJar(cookies_path)
    cj.load(ignore_discard=True, ignore_expires=True)
    session.cookies.update(cj)
    return session


def validate_session(session: requests.Session) -> bool:
    try:
        resp = session.get(
            f"{DLSITE_PLAY_BASE_URL}/api/v3/content/count?last=0", timeout=30
        )
        if resp.status_code != 200:
            return False
        data: dict[str, Any] = resp.json()
        return isinstance(data.get("user"), int)
    except (requests.RequestException, ValueError):
        return False


def ensure_session_valid(session: requests.Session) -> bool:
    try:
        if validate_session(session):
            return True
        _authorize_play(session)
        return validate_session(session)
    except Exception:
        return False
