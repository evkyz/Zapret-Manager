import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
import requests


VERSION_URL = "https://raw.githubusercontent.com/Flowseal/zapret-discord-youtube/main/.service/version.txt"
RELEASE_API_URL = "https://api.github.com/repos/Flowseal/zapret-discord-youtube/releases/latest"


def get_temp_dir() -> Path:
    """Системная папка %TEMP%."""
    return Path(tempfile.gettempdir())


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
    Берём только .zip.
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
            return None
        data = r.json()
    except Exception:
        return None

    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None

    assets = data.get("assets") or []
    for asset in assets:
        name = asset.get("name", "")
        if not name.endswith(".zip"):
            continue
        url = asset.get("browser_download_url")
        if url:
            return {
                "version": tag,
                "download_url": url,
                "asset_name": name,
            }
    return None


def extract_zip(zip_path: Path, extract_to: Path) -> Path | None:
    """
    Распаковывает zip в extract_to.
    Возвращает путь к папке, содержимое которой нужно скопировать
    в zapret_dir:
    - если внутри одна корневая папка (zapret-discord-youtube-X.Y.Z) — её путь;
    - если несколько файлов/папок в корне — extract_to.
    Возвращает None при ошибке.
    """
    try:
        extract_to.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_to)
    except Exception:
        return None

    entries = [p for p in extract_to.iterdir() if p.name != "__MACOSX"]
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]

    if len(dirs) == 1 and not files:
        return dirs[0]
    return extract_to


def safe_rmtree(path: Path) -> None:
    """Удаляет дерево, игнорируя ошибки (файл заблокирован и т.п.)."""
    shutil.rmtree(path, ignore_errors=True)