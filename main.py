import sys
import ctypes
from ctypes import wintypes
from pathlib import Path


def resource_path(relative: str) -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parent / relative


def handle_proxy_only_mode() -> None:
    if "--proxy-only" not in sys.argv:
        return

    idx = sys.argv.index("--proxy-only")
    proxy_argv = [sys.argv[0]] + sys.argv[idx + 1:]
    old_argv = sys.argv
    sys.argv = proxy_argv

    try:
        from proxy.tg_ws_proxy import main
        main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[PROXY-ONLY] Ошибка: {e}", file=sys.stderr)
    finally:
        sys.argv = old_argv
    sys.exit(0)


def ensure_admin():
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return False
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable,
        " ".join(f'"{a}"' for a in sys.argv), None, 1
    )
    return False


_mutex_handle = None


def acquire_single_instance(name: str = "Global\\EvKyz.ZapretManager") -> bool:
    global _mutex_handle

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    ERROR_ALREADY_EXISTS = 183

    handle = kernel32.CreateMutexW(None, True, name)
    last_error = ctypes.get_last_error()

    if not handle:
        return True

    if last_error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _mutex_handle = handle
    return True


def show_already_running_message():
    try:
        MB_OK = 0x00000000
        MB_ICONINFORMATION = 0x00000040
        MB_TOPMOST = 0x00040000

        ctypes.windll.user32.MessageBoxW(
            None,
            "Программа уже запущена",
            "Zapret Manager",
            MB_OK | MB_ICONINFORMATION | MB_TOPMOST,
        )
    except Exception:
        pass


if __name__ == "__main__":
    handle_proxy_only_mode()

    if not ensure_admin():
        sys.exit(0)

    if not acquire_single_instance():
        show_already_running_message()
        sys.exit(0)

    sys.path.insert(0, str(Path(__file__).parent))

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    icon_path = resource_path("assets/icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EvKyz.ZapretManager"
        )
    except Exception:
        pass

    w = MainWindow()
    sys.exit(app.exec())