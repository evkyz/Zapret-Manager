import os
import subprocess
import sys
import time
from pathlib import Path

from core.config import TG_LOG_FILE

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class TgProxyRunner:
    def __init__(self, log_fn=None):
        self.log_fn = log_fn or (lambda s: None)
        self.proc: subprocess.Popen | None = None
        self.proxy_link: str | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 1443
        self.secret: str = ""
        self._log_path: Path | None = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _build_argv(self, host: str, port: int, secret: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--proxy-only",
                "--port", str(port),
                "--host", host,
                "--secret", secret,
                "-v",
            ]
        return [
            sys.executable, "-m", "proxy.tg_ws_proxy",
            "--port", str(port),
            "--host", host,
            "--secret", secret,
            "-v",
        ]

    def _kill_stale_on_port(self) -> None:
        try:
            r = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.log_fn(f"[TG-PROXY] netstat упал: {e}")
            return

        my_pid = os.getpid()

        pids: set[str] = set()
        needle = f":{self.port}"
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[1]
            state = parts[3]
            pid = parts[4]
            if needle not in local:
                continue
            if state != "LISTENING":
                continue
            if pid == "0":
                continue
            if pid == str(my_pid):
                continue
            pids.add(pid)

        for pid in pids:
            self.log_fn(f"[TG-PROXY] Убиваю висящий процесс PID={pid} "
                        f"на порту {self.port}")
            try:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as e:
                self.log_fn(f"[TG-PROXY] taskkill PID={pid}: {e}")
        if pids:
            time.sleep(0.5)

    def start(self, host: str = "127.0.0.1", port: int = 1443,
              secret: str = "", wait: float = 1.5) -> bool:
        if self.is_running():
            self.log_fn("[TG-PROXY] Уже запущен, повторный запуск пропущен")
            return True

        self.host = host
        self.port = port
        self.secret = secret
        self.proxy_link = (
            f"tg://proxy?server={host}&port={port}&secret={secret}"
        )

        self._kill_stale_on_port()

        argv = self._build_argv(host, port, secret)
        self.log_fn(f"[TG-PROXY] Запуск: {' '.join(argv)}")

        try:
            self._log_path = TG_LOG_FILE
            log_handle = open(self._log_path, "wb")
        except Exception as e:
            self.log_fn(f"[TG-PROXY] Не удалось открыть лог-файл: {e}")
            log_handle = subprocess.DEVNULL

        try:
            self.proc = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            self.log_fn(f"[TG-PROXY] Popen упал: {e}")
            self.proc = None
            return False
        finally:
            if log_handle is not subprocess.DEVNULL:
                try:
                    log_handle.close()
                except Exception:
                    pass

        time.sleep(wait)

        if self.proc.poll() is not None:
            code = self.proc.returncode
            self.log_fn(f"[TG-PROXY] Процесс упал с кодом {code}")
            self._dump_log()
            self.proc = None
            return False

        self.log_fn(f"[TG-PROXY] Запущен. Ссылка: {self.proxy_link}")
        return True

    def restart(self, host: str, port: int, secret: str,
                wait: float = 1.5) -> bool:
        self.log_fn("[TG-PROXY] Перезапуск...")
        self.stop()
        return self.start(host=host, port=port, secret=secret, wait=wait)

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception as e:
            self.log_fn(f"[TG-PROXY] Ошибка остановки: {e}")
        finally:
            self.proc = None
        self.log_fn("[TG-PROXY] Остановлен")

    def _dump_log(self):
        if not self._log_path or not self._log_path.exists():
            return
        try:
            text = self._log_path.read_text(
                encoding="utf-8", errors="ignore"
            ).strip()
            for line in text.splitlines()[-15:]:
                self.log_fn(f"[TG-PROXY] {line}")
        except Exception:
            pass