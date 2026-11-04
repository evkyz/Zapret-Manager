import os
import re
from pathlib import Path
import requests


VERSION_URL = "https://raw.githubusercontent.com/Flowseal/zapret-discord-youtube/main/.service/version.txt"
RELEASE_API_URL = "https://api.github.com/repos/Flowseal/zapret-discord-youtube/releases/latest"


def get_downloads_dir() -> Path:
    """
    Системная папка «Загрузки».
    Пробует User Shell Folders и Shell Folders.
    Fallback — %USERPROFILE%\\Downloads.
    """
    import winreg

    guid = "{374DE290-123F-4565-9164-39C4925E467B}"

    locations = [
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"),
    ]

    for hive, subkey in locations:
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
            try:
                raw, _ = winreg.QueryValueEx(key, guid)
                expanded = os.path.expandvars(raw)
                p = Path(expanded)
                if p.is_dir():
                    return p
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass

    fallback = Path.home() / "Downloads"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fallback


def get_local_zapret_version(zapret_dir: Path | None) -> str | None:
    """Локальная версия zapret из service.bat или .service/version.txt."""
    if not zapret_dir:
        return None

    service_bat = Path(zapret_dir) / "service.bat"
    if service_bat.is_file():
        try:
            text = service_bat.read_text(encoding="utf-8", errors="ignore")
            m = re.search(
                r'set\s+"?LOCAL_VERSION=([0-9]+\.[0-9]+\.[0-9]+)"?',
                text,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)
        except Exception:
            pass

    version_txt = Path(zapret_dir) / ".service" / "version.txt"
    if version_txt.is_file():
        try:
            v = version_txt.read_text(encoding="utf-8", errors="ignore").strip()
            if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v):
                return v
        except Exception:
            pass

    return None


def check_latest_version(timeout: float = 5.0) -> str | None:
    """Последняя версия zapret с GitHub (только номер, без скачивания)."""
    try:
        r = requests.get(
            VERSION_URL,
            timeout=timeout,
            headers={"Cache-Control": "no-cache"},
        )
        if r.status_code == 200:
            return r.text.strip()
    except requests.RequestException:
        pass
    return None


def get_latest_release_info(timeout: float = 10.0) -> dict | None:
    """
    Данные последнего релиза с GitHub API:
    version, download_url, asset_name. None при ошибке.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ZapretManager/1.0.0.0",
        "Cache-Control": "no-cache",
    }

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.get(
            RELEASE_API_URL,
            timeout=timeout,
            headers=headers,
        )
        if r.status_code != 200:
            print(f"[updater] GitHub API вернул {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
    except Exception as e:
        print(f"[updater] Ошибка запроса к GitHub API: {e}")
        return None

    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None

    assets = data.get("assets") or []
    for ext in (".rar", ".zip", ".tar.gz"):
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(ext):
                url = asset.get("browser_download_url")
                if url:
                    return {
                        "version": tag,
                        "download_url": url,
                        "asset_name": name,
                    }
    return None