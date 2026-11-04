r"""
Автозапуск при входе в систему через Планировщик задач Windows.
Задача создаётся с /RL HIGHEST — при входе в систему приложение
запускается сразу с правами администратора, без UAC.
"""
import sys
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

ADMIN_TASK_NAME = "ZapretManagerAdmin"


def _task_exe_command() -> str:
    """
    Команда для /TR в schtasks.
    В exe-режиме: "C:\path\ZapretManager.exe"
    В dev-режиме: "C:\path\pythonw.exe" "C:\path\main.py"
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'

    project_root = Path(__file__).resolve().parent.parent
    main_py = project_root / "main.py"
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    return f'"{pythonw}" "{main_py}"'


def is_admin_task_enabled() -> bool:
    """Проверяет, существует ли задача ZapretManagerAdmin."""
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", ADMIN_TASK_NAME],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def create_admin_task() -> tuple[bool, str]:
    """
    Создаёт задачу в Планировщике с /RL HIGHEST /SC ONLOGON.
    Возвращает (success, error_message).
    Требует прав администратора.
    """
    try:
        r = subprocess.run(
            [
                "schtasks", "/Create",
                "/TN", ADMIN_TASK_NAME,
                "/TR", _task_exe_command(),
                "/SC", "ONLOGON",
                "/RL", "HIGHEST",
                "/F",
            ],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout or "").strip()
        return False, err or f"schtasks вернул код {r.returncode}"
    except Exception as e:
        return False, str(e)


def delete_admin_task() -> tuple[bool, str]:
    """Удаляет задачу ZapretManagerAdmin. Возвращает (success, error_message)."""
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", ADMIN_TASK_NAME, "/F"],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout or "").strip()
        return False, err or f"schtasks вернул код {r.returncode}"
    except Exception as e:
        return False, str(e)