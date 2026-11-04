import subprocess
import time
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class WinwsRunner:
    def __init__(self, zapret_dir: Path, log_fn=None):
        self.zapret_dir = zapret_dir
        self.bin_dir = zapret_dir / "bin"
        self.exe = self.bin_dir / "winws.exe"
        self.proc: subprocess.Popen | None = None
        self.log_fn = log_fn or (lambda s: None)
        self._stderr_handle = None
        self.stderr_path = self.bin_dir / "_last_winws_stderr.log"

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start_nowait(self, argv: list[str]) -> bool:
        """
        Запускает winws.exe без time.sleep. Возвращает True, если
        Popen успешно создал процесс. Проверку «жив ли» и вывод stderr
        делает вызывающая сторона через check_alive().
        """
        if not self.exe.exists():
            self.log_fn(f"[RUNNER] НЕ НАЙДЕН exe: {self.exe}")
            return False

        if self.is_running():
            self.stop()

        self.log_fn(f"[RUNNER] exe={self.exe}")
        self.log_fn(f"[RUNNER] argv ({len(argv)}): {' '.join(argv[:8])}"
                    + (" ..." if len(argv) > 8 else ""))

        try:
            self._stderr_handle = open(self.stderr_path, "wb")
        except Exception as e:
            self.log_fn(f"[RUNNER] не удалось открыть stderr-лог: {e}")
            self._stderr_handle = None

        kwargs = dict(
            cwd=str(self.bin_dir),
            stdout=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        if self._stderr_handle:
            kwargs["stderr"] = self._stderr_handle
        else:
            kwargs["stderr"] = subprocess.DEVNULL

        try:
            self.proc = subprocess.Popen([str(self.exe), *argv], **kwargs)
        except Exception as e:
            self.log_fn(f"[RUNNER] Popen упал: {e}")
            return False

        return True

    def check_alive(self) -> bool:
        """
        Проверяет, что процесс жив. Если упал — дампит stderr и
        возвращает False.
        """
        if self.proc is None:
            return False
        if self.proc.poll() is None:
            return True
        code = self.proc.returncode
        self._dump_stderr()
        self.log_fn(f"[RUNNER] winws.exe упал с кодом {code}")
        self.proc = None
        self._close_stderr()
        return False

    def _dump_stderr(self):
        if self._stderr_handle:
            self._stderr_handle.flush()
        try:
            if self.stderr_path.exists():
                err = self.stderr_path.read_text(encoding="utf-8", errors="ignore")
                err = err.strip()
                if err:
                    lines = err.splitlines()
                    for line in lines[-15:]:
                        self.log_fn(f"[STDERR] {line}")
                else:
                    self.log_fn("[STDERR] пусто")
        except Exception as e:
            self.log_fn(f"[STDERR] не удалось прочитать: {e}")

    def _close_stderr(self):
        if self._stderr_handle:
            try:
                self._stderr_handle.close()
            except Exception:
                pass
            self._stderr_handle = None

    def stop(self) -> None:
        if self.proc is None:
            self._close_stderr()
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        finally:
            self._close_stderr()
            self.proc = None


def kill_all_winws() -> None:
    subprocess.run(
        ["taskkill", "/IM", "winws.exe", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def stop_zapret_service() -> None:
    for name in ("zapret", "WinDivert", "WinDivert14"):
        subprocess.run(
            ["net", "stop", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )