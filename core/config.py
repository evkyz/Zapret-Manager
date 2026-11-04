import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, asdict, field

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ZapretManager"
APP_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = APP_DIR / "settings.json"
SCORES_FILE = APP_DIR / "scores.json"
LOG_FILE = APP_DIR / "app.log"
TG_LOG_FILE = APP_DIR / "tg_proxy.log"


@dataclass
class Settings:
    zapret_dir: str = ""
    auto_update_check: bool = True
    apply_on_start: bool = True
    last_strategy: str = ""
    last_strategy_argv: list = field(default_factory=list)
    last_scan_iso: str = ""

    # TG WS Proxy
    tg_proxy_enabled: bool = False
    tg_proxy_running: bool = False
    tg_host: str = "127.0.0.1"
    tg_port: int = 1443
    tg_secret: str = "0123456789abcdef0123456789abcdef"

    # Автозапуск при входе в систему через Планировщик (с правами админа)
    admin_autostart: bool = False


def is_valid_zapret_dir(p: Path | str | None) -> bool:
    if p is None:
        return False
    try:
        p = Path(p)
        if not p.is_dir():
            return False
        if not (p / "bin" / "winws.exe").is_file():
            return False
        if not any(p.glob("general*.bat")):
            return False
        return True
    except OSError:
        return False


def _candidate_dirs() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent)
        except Exception:
            pass
    try:
        candidates.append(Path(sys.argv[0]).resolve().parent)
    except Exception:
        pass
    try:
        candidates.append(Path.cwd())
    except Exception:
        pass

    seen = set()
    unique: list[Path] = []
    for c in candidates:
        key = str(c).lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def detect_zapret_dir() -> Path | None:
    for c in _candidate_dirs():
        if is_valid_zapret_dir(c):
            return c
    return None


def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return Settings(**{**asdict(Settings()), **data})
        except Exception:
            pass
    return Settings()


def save_settings(s: Settings) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(asdict(s), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_scores() -> dict:
    if SCORES_FILE.exists():
        try:
            return json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_scores(scores: dict) -> None:
    SCORES_FILE.write_text(
        json.dumps(scores, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )