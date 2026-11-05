import sys
import os
import secrets
import shutil
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import (
    Qt, QThread, Signal, QEvent, QTimer, QPoint, QRect, QSize,
)
from PySide6.QtGui import QAction, QColor, QPainter, QBrush
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QListWidget, QListWidgetItem, QCheckBox, QFileDialog,
    QMessageBox, QProgressBar, QPlainTextEdit, QSystemTrayIcon, QMenu,
    QApplication, QToolButton, QTabWidget, QLineEdit, QFormLayout,
    QStyledItemDelegate, QStyle,
)

from core.config import (
    load_settings, save_settings, load_scores, save_scores,
    detect_zapret_dir, is_valid_zapret_dir, LOG_FILE,
)
from core.selector import scan_all
from core.updater import (
    check_latest_version, get_local_zapret_version,
    get_latest_release_info, get_temp_dir,
    extract_zip, safe_rmtree,
)
from core.runner import WinwsRunner, kill_all_winws, stop_zapret_service
from gui.autostart import (
    is_admin_task_enabled, create_admin_task, delete_admin_task,
)


STRATEGY_ARGV_ROLE = Qt.ItemDataRole.UserRole + 1
STRATEGY_NAME_ROLE = Qt.ItemDataRole.UserRole + 2
RUNNING_ROLE = Qt.ItemDataRole.UserRole + 3

APP_VERSION = "v.0.8.0"
APP_COPYRIGHT = "© EvKyz, 2026"

RUNNING_MARK = "✓"

try:
    from core.tg_proxy_runner import TgProxyRunner
except ImportError:
    TgProxyRunner = None


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0, 0, 0)


def _display_name(full_name: str) -> str:
    if full_name.lower().endswith(".bat"):
        return full_name[:-4]
    return full_name


def ask_yes_no(parent, title: str, text: str, default_yes: bool = True) -> bool:
    msg = QMessageBox(parent)
    msg.setWindowTitle(title)
    msg.setIcon(QMessageBox.Question)
    msg.setText(text)

    btn_yes = msg.addButton("Да", QMessageBox.YesRole)
    btn_no = msg.addButton("Нет", QMessageBox.NoRole)
    msg.setDefaultButton(btn_yes if default_yes else btn_no)

    msg.exec()
    return msg.clickedButton() == btn_yes


class RunningMarkDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if not index.data(RUNNING_ROLE):
            return
        painter.save()
        try:
            rect = option.rect
            margin = 6
            mark_w = 20
            mark_rect = QRect(
                rect.right() - mark_w - margin,
                rect.top(),
                mark_w,
                rect.height(),
            )
            if option.state & QStyle.StateFlag.State_Selected:
                painter.setPen(Qt.GlobalColor.white)
            else:
                painter.setPen(Qt.GlobalColor.darkGreen)
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                mark_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                RUNNING_MARK,
            )
        finally:
            painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width() + 28, s.height())


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    finished_ = Signal(list)
    log_signal = Signal(str)

    def __init__(self, zapret_dir: Path):
        super().__init__()
        self.zapret_dir = zapret_dir
        self.stop_event = None

    def run(self):
        import threading
        self.stop_event = threading.Event()

        def on_result(strat, score, checks):
            try:
                cache = load_scores()
                cache[strat.name] = {"score": score, "argv": strat.argv}
                save_scores(cache)
            except Exception as e:
                self.log_signal.emit(f"[CACHE] не удалось сохранить: {e}")

        try:
            results = scan_all(
                self.zapret_dir,
                progress=self.progress.emit,
                log=self.log_signal.emit,
                stop_event=self.stop_event,
                debug_argv=False,
                on_result=on_result,
            )
        except Exception as e:
            self.log_signal.emit(f"[FATAL] {e}")
            results = []
        self.finished_.emit(results)

    def request_stop(self):
        if self.stop_event:
            self.stop_event.set()


class DownloadWorker(QThread):
    progress = Signal(int)
    finished_ = Signal(bool, str)

    def __init__(self, url: str, target_path: Path):
        super().__init__()
        self.url = url
        self.target_path = target_path
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        import requests
        try:
            self.target_path.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(self.url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                done = 0
                with open(self.target_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if self._stop:
                            raise RuntimeError("cancelled")
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)
                            if total > 0:
                                self.progress.emit(int(done * 100 / total))
            self.finished_.emit(True, str(self.target_path))
        except Exception as e:
            try:
                if self.target_path.exists():
                    self.target_path.unlink()
            except Exception:
                pass
            self.finished_.emit(False, str(e))


class _DotWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._color = QColor(220, 40, 40)

    def set_color(self, color: QColor):
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(self._color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 12, 12)


class StatusDotLabel(QWidget):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(20)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.dot = _DotWidget(self)
        layout.addWidget(self.dot, 0)

        self.label = QLabel(text, self)
        self.label.setStyleSheet("color: gray;")
        layout.addWidget(self.label, 1)

    def set_state(self, running: bool, text: str):
        color = QColor(0, 180, 0) if running else QColor(220, 40, 40)
        self.dot.set_color(color)
        self.label.setText(text)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zapret Manager")
        self.resize(440, 660)

        self.settings = load_settings()

        saved_str = self.settings.zapret_dir or ""
        saved_path = Path(saved_str) if saved_str else None

        if saved_path and is_valid_zapret_dir(saved_path):
            self.zapret_dir: Path | None = saved_path
        else:
            detected = detect_zapret_dir()
            if detected:
                self.zapret_dir = detected
                self.settings.zapret_dir = str(detected)
                save_settings(self.settings)
            else:
                self.zapret_dir = None

        self.runner: WinwsRunner | None = (
            WinwsRunner(self.zapret_dir, log_fn=None)
            if self.zapret_dir else None
        )

        self.tg_runner = TgProxyRunner(log_fn=None) if TgProxyRunner else None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        # ---------- Вкладка winws ----------
        winws_tab = QWidget()
        layout = QVBoxLayout(winws_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        path_row = QHBoxLayout()
        self.path_label = QLabel(
            str(self.zapret_dir) if self.zapret_dir
            else "Папка не найдена — укажите вручную"
        )
        self.path_label.setWordWrap(False)
        btn_pick = QPushButton("...")
        btn_pick.setFixedWidth(28)
        btn_pick.clicked.connect(self.pick_dir)
        path_row.addWidget(QLabel("Папка:"))
        path_row.addWidget(self.path_label, 1)
        path_row.addWidget(btn_pick)
        layout.addLayout(path_row)

        self.cb_updates = QCheckBox("Проверять обновления zapret")
        self.cb_updates.setChecked(self.settings.auto_update_check)
        self.cb_updates.toggled.connect(self.on_toggle_updates)
        layout.addWidget(self.cb_updates)

        self.cb_admin_task = QCheckBox("Автозапуск при входе в систему")
        try:
            actual_task = is_admin_task_enabled()
        except Exception:
            actual_task = False
        if actual_task != self.settings.admin_autostart:
            self.settings.admin_autostart = actual_task
            save_settings(self.settings)
        self.cb_admin_task.setChecked(actual_task)
        self.cb_admin_task.toggled.connect(self.on_toggle_admin_task)
        layout.addWidget(self.cb_admin_task)

        level_row = QHBoxLayout()
        level_row.setSpacing(12)

        self.cb_apply_on_start = QCheckBox("Запускать winws.exe при запуске")
        self.cb_apply_on_start.setChecked(self.settings.apply_on_start)
        self.cb_apply_on_start.toggled.connect(self.on_toggle_apply_on_start)
        level_row.addWidget(self.cb_apply_on_start)

        self.cb_tg_proxy = QCheckBox("Активировать TG Proxy")
        self.cb_tg_proxy.setChecked(self.settings.tg_proxy_enabled)
        self.cb_tg_proxy.toggled.connect(self.on_toggle_tg_proxy)
        if TgProxyRunner is None:
            self.cb_tg_proxy.setEnabled(False)
        level_row.addWidget(self.cb_tg_proxy)

        level_row.addStretch(1)
        layout.addLayout(level_row)

        scan_row = QHBoxLayout()
        scan_row.setSpacing(6)

        self.btn_scan = QPushButton("Сканировать")
        self.btn_scan.setFixedWidth(95)
        self.btn_scan.clicked.connect(self.on_scan_button_clicked)
        self.btn_scan.setEnabled(self.zapret_dir is not None)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)

        scan_row.addWidget(self.btn_scan)
        scan_row.addWidget(self.progress, 1)
        layout.addLayout(scan_row)

        layout.addWidget(QLabel("Результаты:"))

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setItemDelegate(RunningMarkDelegate(self.list_widget))
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(
            self._on_list_context_menu
        )
        layout.addWidget(self.list_widget, 1)

        self.tabs.addTab(winws_tab, "winws")

        # ---------- Вкладка TG Proxy ----------
        self.tg_tab = QWidget()
        tg_layout = QVBoxLayout(self.tg_tab)
        tg_layout.setContentsMargins(10, 10, 10, 10)
        tg_layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        self.tg_host_edit = QLineEdit(self.settings.tg_host)
        self._attach_russian_context_menu(self.tg_host_edit)
        form.addRow("Host:", self.tg_host_edit)

        self.tg_port_edit = QLineEdit(str(self.settings.tg_port))
        self._attach_russian_context_menu(self.tg_port_edit)
        form.addRow("Port:", self.tg_port_edit)

        self.tg_secret_edit = QLineEdit(self.settings.tg_secret)
        self.tg_secret_edit.setMaxLength(32)
        self._attach_russian_context_menu(self.tg_secret_edit)
        form.addRow("Secret (32 hex):", self.tg_secret_edit)

        self.tg_link_edit = QLineEdit()
        self.tg_link_edit.setReadOnly(True)
        self.tg_link_edit.setPlaceholderText(
            "Ссылка появится после запуска прокси"
        )
        self._attach_russian_context_menu(self.tg_link_edit)
        form.addRow("Прокси-ссылка:", self.tg_link_edit)

        self.tg_status_widget = StatusDotLabel("Прокси остановлен")
        self.tg_status_widget.set_state(False, "Прокси остановлен")
        form.addRow("Статус:", self.tg_status_widget)

        tg_layout.addLayout(form)

        control_row = QHBoxLayout()
        control_row.setSpacing(6)

        self.btn_tg_toggle = QPushButton("Включить прокси")
        self.btn_tg_toggle.clicked.connect(self.on_toggle_tg_run)
        control_row.addWidget(self.btn_tg_toggle, 1)

        btn_restart_tg = QPushButton("Перезапустить прокси")
        btn_restart_tg.clicked.connect(self.on_restart_tg_proxy)
        control_row.addWidget(btn_restart_tg, 1)

        tg_layout.addLayout(control_row)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)

        btn_gen = QPushButton("Сгенерировать ключ")
        btn_gen.clicked.connect(self.on_generate_tg_settings)
        actions_row.addWidget(btn_gen, 1)

        self.btn_copy_link = QPushButton("Копировать ссылку")
        self.btn_copy_link.clicked.connect(self.on_copy_tg_link)
        self.btn_copy_link.setEnabled(False)
        actions_row.addWidget(self.btn_copy_link, 1)

        tg_layout.addLayout(actions_row)
        tg_layout.addStretch(1)

        self.tabs.addTab(self.tg_tab, "TG Proxy")

        # ---------- Лог ----------
        log_header = QHBoxLayout()
        log_header.setContentsMargins(0, 0, 0, 0)
        log_header.setSpacing(4)

        self.btn_toggle_log = QToolButton()
        self.btn_toggle_log.setText("▸ Лог")
        self.btn_toggle_log.setCheckable(True)
        self.btn_toggle_log.setChecked(False)
        self.btn_toggle_log.setAutoRaise(True)
        self.btn_toggle_log.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_toggle_log.clicked.connect(self._toggle_log_visibility)
        log_header.addWidget(self.btn_toggle_log)
        log_header.addStretch(1)
        outer.addLayout(log_header)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setVisible(False)
        self.log.setContextMenuPolicy(Qt.CustomContextMenu)
        self.log.customContextMenuRequested.connect(self._on_log_context_menu)
        outer.addWidget(self.log)

        # ---------- Нижняя строка ----------
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)

        self.status_label = QLabel(APP_VERSION)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        copyright_label = QLabel(APP_COPYRIGHT)
        copyright_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        copyright_label.setStyleSheet("color: gray;")

        footer.addWidget(self.status_label, 1)
        footer.addWidget(copyright_label, 0)
        outer.addLayout(footer)

        # ---------- Инициализация ----------
        if self.runner:
            self.runner.log_fn = self.log_line
        if self.tg_runner:
            self.tg_runner.log_fn = self.log_line

        self.tray = QSystemTrayIcon(self)
        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.instance().windowIcon()
        if not icon.isNull():
            self.tray.setIcon(icon)
        self.tray.setToolTip("Zapret Manager")

        self.tray_menu = QMenu()

        self.act_show = QAction("Показать", self)
        font = self.act_show.font()
        font.setBold(True)
        self.act_show.setFont(font)
        self.act_show.triggered.connect(self.show_from_tray)

        self.act_toggle_winws = QAction("Остановить winws.exe", self)
        self.act_toggle_winws.triggered.connect(self.on_toggle_winws_clicked)

        self.act_restart_tg = QAction("Перезапустить прокси", self)
        self.act_restart_tg.triggered.connect(self.on_restart_tg_proxy)

        self.act_tg_open = QAction("Открыть прокси в Telegram", self)
        self.act_tg_open.triggered.connect(self.on_open_tg_proxy)
        self.act_tg_open.setEnabled(False)

        self.act_view_log = QAction("Посмотреть лог", self)
        self.act_view_log.triggered.connect(self.on_view_log)

        self.act_quit = QAction("Выход", self)
        self.act_quit.triggered.connect(self.quit_app)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.best_result = None
        self.worker: ScanWorker | None = None
        self.downloader: DownloadWorker | None = None
        self._really_quit = False

        self._tray_click_timer = QTimer(self)
        self._tray_click_timer.setSingleShot(True)
        self._tray_click_timer.setInterval(80)
        self._tray_click_timer.timeout.connect(self.show_from_tray)

        self._tg_status_timer = QTimer(self)
        self._tg_status_timer.setInterval(1500)
        self._tg_status_timer.timeout.connect(self._refresh_tg_status_widget)
        self._tg_status_timer.start()

        self._winws_check_timer: QTimer | None = None

        self._apply_tg_proxy_tab_state(self.settings.tg_proxy_enabled)
        self._rebuild_tray_menu()

        self.load_cached()
        self.maybe_check_updates()
        self._refresh_winws_menu()
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._refresh_tg_status_widget()

        QTimer.singleShot(0, self._maybe_apply_on_start)

    # ---------- Русское контекстное меню для QLineEdit ----------

    def _attach_russian_context_menu(self, line_edit: QLineEdit):
        """
        Заменяет стандартное английское контекстное меню
        (Cut / Copy / Paste / Select All) на русское.
        Для readonly-полей «Вырезать» и «Вставить» скрываются
        автоматически по флагам действий.
        """
        line_edit.setContextMenuPolicy(Qt.CustomContextMenu)

        def show_menu(pos):
            menu = QMenu(line_edit)

            act_undo = QAction("Отменить", line_edit)
            act_undo.setEnabled(line_edit.isUndoAvailable()
                                and not line_edit.isReadOnly())
            act_undo.triggered.connect(line_edit.undo)
            menu.addAction(act_undo)

            act_redo = QAction("Повторить", line_edit)
            act_redo.setEnabled(line_edit.isRedoAvailable()
                                and not line_edit.isReadOnly())
            act_redo.triggered.connect(line_edit.redo)
            menu.addAction(act_redo)

            menu.addSeparator()

            act_cut = QAction("Вырезать", line_edit)
            act_cut.setEnabled(line_edit.hasSelectedText()
                               and not line_edit.isReadOnly())
            act_cut.triggered.connect(line_edit.cut)
            menu.addAction(act_cut)

            act_copy = QAction("Копировать", line_edit)
            act_copy.setEnabled(line_edit.hasSelectedText())
            act_copy.triggered.connect(line_edit.copy)
            menu.addAction(act_copy)

            act_paste = QAction("Вставить", line_edit)
            act_paste.setEnabled(
                not line_edit.isReadOnly()
                and bool(QApplication.clipboard().text())
            )
            act_paste.triggered.connect(line_edit.paste)
            menu.addAction(act_paste)

            menu.addSeparator()

            act_select_all = QAction("Выбрать всё", line_edit)
            act_select_all.triggered.connect(line_edit.selectAll)
            menu.addAction(act_select_all)

            menu.exec(line_edit.mapToGlobal(pos))

        line_edit.customContextMenuRequested.connect(show_menu)

    # ---------- Видимость/доступность вкладки TG Proxy ----------

    def _apply_tg_proxy_tab_state(self, enabled: bool):
        idx = self.tabs.indexOf(self.tg_tab)
        if idx < 0:
            return
        self.tabs.setTabVisible(idx, True)
        if not enabled:
            if self.tabs.currentIndex() == idx:
                self.tabs.setCurrentIndex(0)
            self.tabs.setTabEnabled(idx, False)
        else:
            self.tabs.setTabEnabled(idx, True)

    def on_toggle_tg_proxy(self, checked: bool):
        if checked and TgProxyRunner is None:
            self.cb_tg_proxy.blockSignals(True)
            self.cb_tg_proxy.setChecked(False)
            self.cb_tg_proxy.blockSignals(False)
            self.log_line("[TG-PROXY] Модуль не собран в этой сборке")
            return

        self.settings.tg_proxy_enabled = checked

        if not checked:
            self.log_line("TG Proxy деактивирован — останавливаю прокси")
            self._stop_tg_proxy(force=True)
            self.settings.tg_proxy_running = False
        else:
            self.log_line("TG Proxy активирован")
            if self.settings.tg_proxy_running:
                self._start_tg_proxy()
            else:
                self._refresh_tg_status_widget()

        save_settings(self.settings)

        self._apply_tg_proxy_tab_state(checked)
        self._rebuild_tray_menu()
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._update_tray_tooltip()

    def _start_tg_proxy(self):
        if not self.tg_runner:
            return False
        ok = self.tg_runner.start(
            host=self.settings.tg_host,
            port=self.settings.tg_port,
            secret=self.settings.tg_secret,
        )
        if ok:
            self.settings.tg_proxy_running = True
            save_settings(self.settings)
            self.log_line(
                f"TG WS Proxy работает на "
                f"{self.settings.tg_host}:{self.settings.tg_port}"
            )
            self._refresh_tg_menu()
        else:
            self.settings.tg_proxy_running = False
            save_settings(self.settings)
            self.log_line("TG WS Proxy не удалось запустить")
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._refresh_tg_status_widget()
        self._update_tray_tooltip()
        return ok

    def _stop_tg_proxy(self, force: bool = False):
        if self.tg_runner:
            self.tg_runner.stop()
        if not force:
            self.settings.tg_proxy_running = False
            save_settings(self.settings)
        self._refresh_tg_menu()
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._refresh_tg_status_widget()
        self._update_tray_tooltip()

    def _refresh_tg_toggle_button(self):
        running = bool(self.tg_runner and self.tg_runner.is_running())
        self.btn_tg_toggle.setText(
            "Выключить прокси" if running else "Включить прокси"
        )

    def _refresh_tg_status_widget(self):
        running = bool(self.tg_runner and self.tg_runner.is_running())
        if running:
            self.tg_status_widget.set_state(True, "Прокси запущен")
        else:
            self.tg_status_widget.set_state(False, "Прокси остановлен")

    def _refresh_tg_link(self):
        if self.tg_runner and self.tg_runner.proxy_link:
            self.tg_link_edit.setText(self.tg_runner.proxy_link)
            self.btn_copy_link.setEnabled(True)
            self.act_tg_open.setEnabled(True)
            self.act_tg_open.setText("Открыть прокси в Telegram")
        else:
            self.tg_link_edit.clear()
            self.btn_copy_link.setEnabled(False)
            self.act_tg_open.setEnabled(False)
            self.act_tg_open.setText("Открыть прокси в Telegram (ожидание...)")

    # ---------- Кнопка Включить/Выключить прокси ----------

    def on_toggle_tg_run(self):
        if not self.tg_runner:
            return
        if self.tg_runner.is_running():
            self.tg_runner.stop()
            self.settings.tg_proxy_running = False
            save_settings(self.settings)
            self.log_line("TG WS Proxy остановлен вручную")
            self._refresh_tg_menu()
            self._refresh_tg_link()
            self._refresh_tg_toggle_button()
            self._refresh_tg_status_widget()
            self._update_tray_tooltip()
        else:
            self._start_tg_proxy()

    def on_generate_tg_settings(self):
        self.tg_secret_edit.setText(secrets.token_hex(16))
        self.tg_host_edit.setText("127.0.0.1")
        self.tg_port_edit.setText("1443")
        self.log_line(
            "Сгенерирован новый ключ. Перезапускаю прокси и открываю Telegram..."
        )
        ok = self.tg_runner.restart(
            host="127.0.0.1",
            port=1443,
            secret=self.tg_secret_edit.text(),
        ) if self.tg_runner else False
        if ok:
            self.settings.tg_host = "127.0.0.1"
            self.settings.tg_port = 1443
            self.settings.tg_secret = self.tg_secret_edit.text()
            self.settings.tg_proxy_running = True
            save_settings(self.settings)
            self._refresh_tg_menu()
        else:
            self.settings.tg_proxy_running = False
            save_settings(self.settings)
            self.log_line("Не удалось перезапустить прокси с новым ключом")
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._refresh_tg_status_widget()
        self._update_tray_tooltip()
        self.on_open_tg_proxy()

    def on_copy_tg_link(self):
        link = self.tg_link_edit.text().strip()
        if not link:
            return
        try:
            QApplication.clipboard().setText(link)
            self.log_line(f"[TG-PROXY] Ссылка скопирована: {link}")
            self.tray.showMessage(
                "Zapret Manager",
                "Прокси-ссылка скопирована в буфер обмена",
                QSystemTrayIcon.Information,
                3000,
            )
        except Exception as e:
            self.log_line(f"[TG-PROXY] Не удалось скопировать: {e}")

    # ---------- Контекстное меню лога ----------

    def _on_log_context_menu(self, pos: QPoint):
        menu = QMenu(self)

        act_copy = QAction("Копировать", self)
        act_copy.triggered.connect(self.log.copy)
        menu.addAction(act_copy)

        act_select_all = QAction("Выбрать всё", self)
        act_select_all.triggered.connect(self.log.selectAll)
        menu.addAction(act_select_all)

        menu.addSeparator()

        act_save = QAction("Сохранить лог", self)
        act_save.triggered.connect(self.on_save_log)
        menu.addAction(act_save)

        menu.exec(self.log.mapToGlobal(pos))

    def on_save_log(self):
        start_dir = str(Path.home())
        default_name = f"zapret_manager_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить лог",
            str(Path(start_dir) / default_name),
            "Text files (*.txt);;All files (*.*)",
        )
        if not path:
            return
        try:
            content = self.log.toPlainText()
            Path(path).write_text(content, encoding="utf-8")
            self.log_line(f"Лог сохранён: {path}")
            self.tray.showMessage(
                "Zapret Manager",
                f"Лог сохранён: {path}",
                QSystemTrayIcon.Information,
                4000,
            )
        except Exception as e:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось сохранить лог:\n{e}"
            )

    def on_view_log(self):
        try:
            if not LOG_FILE.exists():
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOG_FILE.write_text("", encoding="utf-8")
            os.startfile(str(LOG_FILE))
        except Exception as e:
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть файл лога:\n{LOG_FILE}\n\n{e}"
            )

    # ---------- Меню трея ----------

    def _rebuild_tray_menu(self):
        self.tray_menu.clear()
        self.tray_menu.addAction(self.act_show)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_toggle_winws)
        if self.settings.tg_proxy_enabled:
            self.tray_menu.addSeparator()
            self.tray_menu.addAction(self.act_tg_open)
            self.tray_menu.addAction(self.act_restart_tg)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_view_log)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.act_quit)

    # ---------- Сворачивание лога ----------

    def _toggle_log_visibility(self):
        visible = self.btn_toggle_log.isChecked()
        self.log.setVisible(visible)
        self.btn_toggle_log.setText("▾ Лог" if visible else "▸ Лог")

    # ---------- Тултип трея ----------

    def _update_tray_tooltip(self):
        if self.runner and self.runner.is_running():
            winws_name = _display_name(self.settings.last_strategy) or "стратегия"
            winws_line = f"winws.exe работает ({winws_name})"
        else:
            winws_line = "winws.exe остановлен"

        if self.settings.tg_proxy_enabled:
            tg_running = bool(self.tg_runner and self.tg_runner.is_running())
            tg_line = "TG Proxy запущен" if tg_running else "TG Proxy остановлен"
            self.tray.setToolTip(f"{winws_line}\n{tg_line}")
        else:
            self.tray.setToolTip(winws_line)

    # ---------- Галочка запущенной стратегии ----------

    def _update_running_mark(self):
        running_name = None
        if self.runner and self.runner.is_running():
            running_name = self.settings.last_strategy or None

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            name = item.data(STRATEGY_NAME_ROLE) or ""
            is_running = bool(running_name and name == running_name)
            item.setData(RUNNING_ROLE, is_running)

        self.list_widget.viewport().update()

    # ---------- TG WS Proxy ----------

    def _read_tg_fields(self) -> tuple[str, int, str] | None:
        host = self.tg_host_edit.text().strip()
        port_str = self.tg_port_edit.text().strip()
        secret = self.tg_secret_edit.text().strip().lower()

        if not host:
            QMessageBox.warning(self, "Ошибка", "Host не может быть пустым")
            return None
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            QMessageBox.warning(self, "Ошибка", "Port должен быть числом 1..65535")
            return None
        if len(secret) != 32 or not all(c in "0123456789abcdef" for c in secret):
            QMessageBox.warning(
                self, "Ошибка", "Secret должен быть 32 hex-символа"
            )
            return None
        return host, int(port_str), secret

    def on_restart_tg_proxy(self):
        if not self.tg_runner:
            return
        fields = self._read_tg_fields()
        if not fields:
            return
        host, port, secret = fields

        self.settings.tg_host = host
        self.settings.tg_port = port
        self.settings.tg_secret = secret
        save_settings(self.settings)

        ok = self.tg_runner.restart(host=host, port=port, secret=secret)
        if ok:
            self.settings.tg_proxy_running = True
            save_settings(self.settings)
            self._refresh_tg_menu()
            self.tray.showMessage(
                "Zapret Manager",
                f"Прокси перезапущен на {host}:{port}",
                QSystemTrayIcon.Information,
                4000,
            )
        else:
            self.settings.tg_proxy_running = False
            save_settings(self.settings)
            self.tray.showMessage(
                "Zapret Manager",
                "Не удалось перезапустить прокси. Смотрите лог.",
                QSystemTrayIcon.Warning,
                5000,
            )
        self._refresh_tg_link()
        self._refresh_tg_toggle_button()
        self._refresh_tg_status_widget()
        self._update_tray_tooltip()

    def on_open_tg_proxy(self):
        if not self.tg_runner:
            return
        link = self.tg_runner.proxy_link
        if not link:
            self.log_line("[TG-PROXY] Ссылка ещё не получена")
            return
        try:
            os.startfile(link)
            self.log_line(f"[TG-PROXY] Открываю в Telegram: {link}")
        except Exception as e:
            self.log_line(f"[TG-PROXY] Не удалось открыть ссылку: {e}")

    def _refresh_tg_menu(self):
        if self.tg_runner and self.tg_runner.proxy_link:
            self.act_tg_open.setEnabled(True)
            self.act_tg_open.setText("Открыть прокси в Telegram")
        else:
            self.act_tg_open.setEnabled(False)
            self.act_tg_open.setText("Открыть прокси в Telegram (ожидание...)")

    # ---------- Прочее ----------

    def pick_dir(self):
        start_dir = str(self.zapret_dir) if self.zapret_dir else ""
        d = QFileDialog.getExistingDirectory(self, "Папка zapret", start_dir)
        if not d:
            return
        new_path = Path(d)
        if not is_valid_zapret_dir(new_path):
            QMessageBox.warning(
                self, "Не папка zapret",
                "В выбранной папке нет bin\\winws.exe и general*.bat.\n"
                "Выберите корень распакованного zapret."
            )
            return
        self.zapret_dir = new_path
        self.settings.zapret_dir = str(new_path)
        save_settings(self.settings)
        self.path_label.setText(str(new_path))
        self.runner = WinwsRunner(self.zapret_dir, log_fn=self.log_line)
        self.btn_scan.setEnabled(True)
        self.log_line(f"Папка zapret: {new_path}")
        self._refresh_winws_menu()
        self._update_tray_tooltip()

    def on_toggle_updates(self, checked: bool):
        self.settings.auto_update_check = checked
        save_settings(self.settings)

    def on_toggle_admin_task(self, checked: bool):
        if checked and not _is_admin():
            self.cb_admin_task.blockSignals(True)
            self.cb_admin_task.setChecked(False)
            self.cb_admin_task.blockSignals(False)
            self.log_line(
                "[ОШИБКА] Для активации автозапуска программа должна "
                "быть запущена от имени Администратора"
            )
            return

        if checked:
            ok, err = create_admin_task()
            if not ok:
                self.cb_admin_task.blockSignals(True)
                self.cb_admin_task.setChecked(False)
                self.cb_admin_task.blockSignals(False)
                self.log_line(f"[ОШИБКА] Не удалось создать задачу: {err}")
                return
            self.settings.admin_autostart = True
            save_settings(self.settings)
            self.log_line(
                "Задача ZapretManagerAdmin создана. "
                "При входе в систему приложение будет запускаться "
                "с правами администратора без UAC."
            )
            self.tray.showMessage(
                "Zapret Manager",
                "Автозапуск при входе в систему включён",
                QSystemTrayIcon.Information,
                4000,
            )
        else:
            ok, err = delete_admin_task()
            if not ok:
                self.log_line(f"[ПРЕДУПРЕЖДЕНИЕ] Удаление задачи: {err}")
            self.settings.admin_autostart = False
            save_settings(self.settings)
            self.log_line(
                "Задача ZapretManagerAdmin удалена. "
                "Автозапуск при входе в систему выключен."
            )

    def on_toggle_apply_on_start(self, checked: bool):
        self.settings.apply_on_start = checked
        save_settings(self.settings)
        self.log_line(
            f"Запуск winws.exe при старте "
            f"{'включён' if checked else 'выключен'}"
        )

    # ---------- Лог: вывод в окно + запись в файл ----------

    def log_line(self, text: str):
        self.log.appendPlainText(text)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._append_log_file(text)

    def _append_log_file(self, text: str):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")
        except Exception:
            pass

    # ---------- Элемент списка ----------

    def _make_item(self, score: float, name: str, argv: list) -> QListWidgetItem:
        item = QListWidgetItem(f"{score:>6.2f}  {_display_name(name)}")
        item.setData(Qt.ItemDataRole.UserRole, score)
        item.setData(STRATEGY_ARGV_ROLE, argv)
        item.setData(STRATEGY_NAME_ROLE, name)
        item.setData(RUNNING_ROLE, False)
        preview = " ".join(argv[:6]) + (" ..." if len(argv) > 6 else "")
        item.setToolTip(f"{name}\n{preview}")
        return item

    def load_cached(self):
        scores = load_scores()
        for name, data in sorted(scores.items(),
                                 key=lambda x: -x[1].get("score", 0)):
            item = self._make_item(
                data.get("score", 0), name, data.get("argv", [])
            )
            self.list_widget.addItem(item)
        self._update_running_mark()

    # ---------- Контекстное меню списка ----------

    def _on_list_context_menu(self, pos: QPoint):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return

        menu = QMenu(self)

        act_apply = QAction("Применить", self)
        act_apply.triggered.connect(lambda: self._apply_from_item(item))
        menu.addAction(act_apply)

        act_delete = QAction("Удалить из результатов", self)
        act_delete.triggered.connect(lambda: self._remove_from_results(item))
        menu.addAction(act_delete)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _apply_from_item(self, item: QListWidgetItem):
        argv = item.data(STRATEGY_ARGV_ROLE)
        name = item.data(STRATEGY_NAME_ROLE)
        if not argv:
            self.log_line(f"[ОШИБКА] Нет argv для {name}")
            return
        self.log_line(f">>> Применяю {_display_name(name)}")
        self._apply_strategy(argv, name)

    def _remove_from_results(self, item: QListWidgetItem):
        name = item.data(STRATEGY_NAME_ROLE) or ""
        if not name:
            return

        try:
            cache = load_scores()
            if name in cache:
                del cache[name]
                save_scores(cache)
        except Exception as e:
            self.log_line(f"[CACHE] не удалось обновить scores.json: {e}")

        row = self.list_widget.row(item)
        self.list_widget.takeItem(row)

        if self.best_result and self.best_result.strategy.name == name:
            self.best_result = None

        self.log_line(f"Удалено из результатов: {_display_name(name)}")
        self._update_running_mark()

    # ---------- Обновления ----------

    def maybe_check_updates(self):
        if not self.settings.auto_update_check:
            return

        self.log_line("Проверка обновлений zapret...")

        local = get_local_zapret_version(self.zapret_dir)
        latest = check_latest_version()

        if latest is None:
            self.log_line("Не удалось проверить обновления (нет сети?)")
            return

        if local is None:
            self.log_line(
                f"Не удалось определить локальную версию zapret. "
                f"Последняя доступная: {latest}"
            )
            return

        if local == latest:
            self.log_line(f"Установлена последняя версия zapret: {local}")
            return

        if _version_tuple(latest) > _version_tuple(local):
            self.log_line(f"Локальная версия zapret: {local}")
            self.log_line(f"Доступна новая версия zapret: {latest}")
            self._offer_update(local, latest)
        else:
            self.log_line(
                f"Локальная версия zapret: {local} "
                f"(новее публичной {latest})"
            )

    def _offer_update(self, local: str, latest: str):
        if not ask_yes_no(
            self,
            "Обновление zapret",
            f"Доступна новая версия zapret: {latest}\n"
            f"У вас: {local}\n\n"
            f"Обновить?",
            default_yes=True,
        ):
            self.log_line("Скачивание отменено пользователем.")
            return

        info = get_latest_release_info()
        if not info:
            self.log_line("[ОШИБКА] Не удалось получить ссылку на релиз.")
            self.tray.showMessage(
                "Zapret Manager",
                "Не удалось получить ссылку на релиз.",
                QSystemTrayIcon.Critical,
                5000,
            )
            return

        url = info["download_url"]
        asset_name = info["asset_name"]
        target = get_temp_dir() / asset_name

        self.log_line(f"Скачиваю: {asset_name}")
        self.log_line(f"URL: {url}")
        self.log_line(f"Куда: {target}")

        self.downloader = DownloadWorker(url, target)
        self.downloader.progress.connect(self._on_download_progress)
        self.downloader.finished_.connect(self._on_download_finished)
        self.downloader.start()

    def _on_download_progress(self, pct: int):
        self.progress.setValue(pct)
        self.status_label.setText(f"{APP_VERSION}  ·  Скачивание: {pct}%")

    def _on_download_finished(self, ok: bool, info: str):
        self.progress.setValue(0)
        self.status_label.setText(APP_VERSION)

        if not ok:
            self.log_line(f"[ОШИБКА] Скачивание не удалось: {info}")
            self.tray.showMessage(
                "Zapret Manager",
                f"Скачивание не удалось: {info}",
                QSystemTrayIcon.Critical,
                5000,
            )
            return

        path = Path(info)
        self.log_line(f"Скачано: {path}")

        if path.suffix.lower() != ".zip":
            self.log_line(
                f"[ОШИБКА] Ожидался .zip, получен {path.suffix}. "
                f"Обновление отменено."
            )
            self.tray.showMessage(
                "Zapret Manager",
                f"Ожидался .zip, получен {path.suffix}. Обновление отменено.",
                QSystemTrayIcon.Critical,
                5000,
            )
            return

        self._auto_update_from_zip(path)

    def _auto_update_from_zip(self, zip_path: Path):
        """Автоматическое обновление из zip-архива."""
        if not self.zapret_dir or not self.zapret_dir.is_dir():
            self.log_line(
                "[ОШИБКА] Папка zapret не задана — авто-обновление невозможно"
            )
            self.tray.showMessage(
                "Zapret Manager",
                "Папка zapret не задана — укажите её в настройках.",
                QSystemTrayIcon.Warning,
                5000,
            )
            return

        self.log_line(f"Авто-обновление из: {zip_path}")

        # 1. Распаковка во временную подпапку %TEMP%\zapret_update_<name>
        extract_to = get_temp_dir() / f"zapret_update_{zip_path.stem}"
        safe_rmtree(extract_to)
        self.log_line(f"Распаковываю в: {extract_to}")
        src_dir = extract_zip(zip_path, extract_to)
        if not src_dir:
            self.log_line("[ОШИБКА] Не удалось распаковать архив")
            self.tray.showMessage(
                "Zapret Manager",
                "Не удалось распаковать архив. Смотрите лог.",
                QSystemTrayIcon.Critical,
                5000,
            )
            safe_rmtree(extract_to)
            return

        self.log_line(f"Источник для копирования: {src_dir}")

        # 2. Проверка, что это zapret
        if not (src_dir / "bin" / "winws.exe").exists():
            self.log_line(
                "[ОШИБКА] В распакованном архиве нет bin\\winws.exe. "
                "Похоже, это не zapret."
            )
            self.tray.showMessage(
                "Zapret Manager",
                "В архиве нет bin\\winws.exe — обновление отменено.",
                QSystemTrayIcon.Critical,
                5000,
            )
            safe_rmtree(extract_to)
            return

        # 3. Запоминаем, был ли запущен winws
        was_running = bool(self.runner and self.runner.is_running())
        self.log_line(
            f"winws.exe был {'запущен' if was_running else 'остановлен'}"
        )

        # 4. Останавливаем winws и службы
        if was_running:
            self.log_line("Останавливаю winws.exe...")
            try:
                self.runner.stop()
            except Exception:
                pass
            try:
                kill_all_winws()
            except Exception:
                pass
            try:
                stop_zapret_service()
            except Exception as e:
                self.log_line(
                    f"[ПРЕДУПРЕЖДЕНИЕ] stop_zapret_service: {e}. "
                    f"Если служба zapret запущена, копирование может упасть."
                )

        # 5. Копируем с заменой
        self.log_line(
            f"Копирую содержимое {src_dir} → {self.zapret_dir}"
        )
        try:
            shutil.copytree(
                str(src_dir),
                str(self.zapret_dir),
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "_last_winws_stderr.log",
                    "*.log",
                ),
            )
        except PermissionError as e:
            self.log_line(f"[ОШИБКА] PermissionError при копировании: {e}")
            self.log_line(
                "Возможно, winws.exe всё ещё держит файлы. "
                "Попробуйте вручную заменить."
            )
            self.tray.showMessage(
                "Zapret Manager",
                "Не удалось заменить файлы: доступ запрещён. "
                "Смотрите лог.",
                QSystemTrayIcon.Critical,
                5000,
            )
            safe_rmtree(extract_to)
            return
        except Exception as e:
            self.log_line(f"[ОШИБКА] Копирование не удалось: {e}")
            self.tray.showMessage(
                "Zapret Manager",
                f"Ошибка копирования: {e}",
                QSystemTrayIcon.Critical,
                5000,
            )
            safe_rmtree(extract_to)
            return

        self.log_line("Файлы заменены.")

        # 6. Чистим временную подпапку
        self.log_line(f"Удаляю временную папку: {extract_to}")
        safe_rmtree(extract_to)

        # 7. Запускаем winws обратно, если был запущен
        if was_running and self.settings.last_strategy_argv:
            self.log_line("Запускаю winws.exe с сохранённой стратегией...")
            self._apply_strategy(
                self.settings.last_strategy_argv,
                self.settings.last_strategy or "(сохранённая)",
                from_startup=True,
            )
        else:
            self.log_line("winws.exe не перезапускаю")

        # 8. Уведомление о завершении обновления — всплывающее окно
        try:
            new_version = get_local_zapret_version(self.zapret_dir) or "?"
        except Exception:
            new_version = "?"

        self.log_line(f"zapret обновлён до версии {new_version}")

        QMessageBox.information(
            self,
            "Zapret обновлён",
            f"zapret обновлён до версии {new_version}",
        )

    # ---------- Автозапуск при старте ----------

    def _maybe_apply_on_start(self):
        if not self.settings.apply_on_start:
            self.log_line("Запуск winws.exe при старте отключён.")
        elif not self.settings.last_strategy_argv:
            self.log_line("Нет сохранённой стратегии для автозапуска.")
        else:
            self.log_line(
                f"Автозапуск стратегии: "
                f"{_display_name(self.settings.last_strategy) or '(без имени)'}"
            )
            self._apply_strategy(
                self.settings.last_strategy_argv,
                self.settings.last_strategy or "(сохранённая)",
                from_startup=True,
            )

        if self.settings.tg_proxy_enabled:
            if self.settings.tg_proxy_running and self.tg_runner:
                self.log_line("=" * 40)
                self.log_line("Возобновляю TG WS Proxy...")
                self._start_tg_proxy()
            else:
                self.log_line(
                    "TG Proxy активирован, но был остановлен — не запускаю"
                )
                self._refresh_tg_status_widget()
        else:
            if self.tg_runner and self.tg_runner.is_running():
                self._stop_tg_proxy(force=True)

        self._update_tray_tooltip()

    # ---------- winws ----------

    def _refresh_winws_menu(self):
        if self.runner and self.runner.is_running():
            self.act_toggle_winws.setText("Остановить winws.exe")
        else:
            self.act_toggle_winws.setText("Запустить winws.exe")
        self._update_running_mark()
        self._update_tray_tooltip()

    def on_toggle_winws_clicked(self):
        if self.runner and self.runner.is_running():
            self._stop_winws()
        else:
            self._start_last_winws()

    def _start_last_winws(self):
        if not self.runner:
            self.log_line("Папка zapret не задана.")
            self.tray.showMessage(
                "Zapret Manager",
                "Папка zapret не задана — укажите её в окне.",
                QSystemTrayIcon.Warning,
                4000,
            )
            return
        if not self.settings.last_strategy_argv:
            self.log_line("Нет сохранённой стратегии. Сначала просканируйте.")
            self.tray.showMessage(
                "Zapret Manager",
                "Нет сохранённой стратегии. Сначала просканируйте.",
                QSystemTrayIcon.Warning,
                4000,
            )
            return
        self._apply_strategy(
            self.settings.last_strategy_argv,
            self.settings.last_strategy or "(сохранённая)",
        )

    def _stop_winws(self):
        if self.runner:
            self.runner.stop()
        self.log_line("winws.exe остановлен.")
        self._refresh_winws_menu()
        self.tray.showMessage(
            "Zapret Manager",
            "winws.exe остановлен",
            QSystemTrayIcon.Information,
            3000,
        )

    # ---------- Сканирование ----------

    def on_scan_button_clicked(self):
        if self.worker and self.worker.isRunning():
            self.stop_scan()
        else:
            self.start_scan()

    def _set_scan_running(self, running: bool):
        text = "Отмена" if running else "Сканировать"
        self.btn_scan.setText(text)

    def start_scan(self):
        if self.worker and self.worker.isRunning():
            return

        if not self.zapret_dir:
            QMessageBox.warning(
                self, "Папка zapret не задана",
                "Укажите папку zapret (кнопка «...») перед сканированием."
            )
            return

        if not (self.zapret_dir / "bin" / "winws.exe").exists():
            QMessageBox.critical(
                self, "Ошибка",
                f"Не найден winws.exe:\n{self.zapret_dir / 'bin' / 'winws.exe'}"
            )
            return

        self.show_from_tray()

        self._set_scan_running(True)
        self.list_widget.clear()
        self.log.clear()
        self.log_line(f"Сканирование: {self.zapret_dir}")

        self.worker = ScanWorker(self.zapret_dir)
        self.worker.progress.connect(self.on_progress)
        self.worker.log_signal.connect(self.log_line)
        self.worker.finished_.connect(self.on_scan_done)
        self.worker.start()

    def stop_scan(self):
        if self.worker and self.worker.isRunning():
            self.log_line(">>> Запрошена остановка...")
            self.worker.request_stop()
            self.btn_scan.setEnabled(False)

    def on_progress(self, i: int, total: int, name: str):
        self.progress.setValue(int(i / total * 100))
        self.status_label.setText(
            f"{APP_VERSION}  ·  [{i}/{total}] {_display_name(name)}"
        )

    def on_scan_done(self, results):
        self._set_scan_running(False)
        self.btn_scan.setEnabled(True)
        self.progress.setValue(100 if results else 0)

        if results:
            cache = load_scores()
            for r in results:
                cache[r.strategy.name] = {
                    "score": r.score,
                    "argv": r.strategy.argv,
                }
            save_scores(cache)

        self.list_widget.clear()
        for r in results:
            item = self._make_item(r.score, r.strategy.name, r.strategy.argv)
            self.list_widget.addItem(item)

        if results and results[0].score > 0:
            self.best_result = results[0]
            best_display = _display_name(results[0].strategy.name)
            self.status_label.setText(
                f"{APP_VERSION}  ·  Лучшая: {best_display} "
                f"(score {results[0].score})"
            )
            self.log_line(f"ЛУЧШАЯ: {best_display} — {results[0].score}")

            self.settings.last_strategy = results[0].strategy.name
            self.settings.last_strategy_argv = list(results[0].strategy.argv)
            self.settings.last_scan_iso = datetime.now().isoformat()
            save_settings(self.settings)

            if self.settings.apply_on_start:
                self.log_line("Запускаю winws.exe после сканирования...")
                self._apply_strategy(
                    results[0].strategy.argv,
                    results[0].strategy.name,
                )

            self.tray.showMessage(
                "Zapret Manager",
                f"Лучшая: {best_display}\nScore {results[0].score}",
                QSystemTrayIcon.Information,
                5000,
            )
        elif results:
            self.best_result = results[0]
            self.status_label.setText(
                f"{APP_VERSION}  ·  Все стратегии дали score=0"
            )
            self.log_line("Все стратегии дали score=0 — проверьте лог.")
        else:
            self.status_label.setText(
                f"{APP_VERSION}  ·  Стратегии не найдены или сканирование отменено"
            )

        self._refresh_winws_menu()

    # ---------- Применение стратегии ----------

    def _apply_strategy(self, argv: list, name: str, from_startup: bool = False):
        if not argv:
            self.log_line(
                f"[ОШИБКА] Для стратегии {_display_name(name)} "
                f"нет сохранённых аргументов."
            )
            return

        if not self.runner:
            self.log_line("[ОШИБКА] Папка zapret не задана — некуда запускать.")
            return

        kill_all_winws()
        stop_zapret_service()

        if not self.runner.start_nowait(argv):
            self.tray.showMessage(
                "Zapret Manager",
                f"winws.exe не запустился ({_display_name(name)}). "
                f"Смотрите лог.",
                QSystemTrayIcon.Critical,
                5000,
            )
            self._refresh_winws_menu()
            return

        self.settings.last_strategy = name
        self.settings.last_strategy_argv = list(argv)
        self.settings.last_scan_iso = datetime.now().isoformat()
        save_settings(self.settings)

        self.log_line(f"Запущено: {_display_name(name)}")
        self._refresh_winws_menu()

        if not from_startup:
            self.tray.showMessage(
                "Zapret Manager",
                f"Применено: {_display_name(name)}",
                QSystemTrayIcon.Information,
                4000,
            )

        if self._winws_check_timer:
            self._winws_check_timer.stop()
        self._winws_check_timer = QTimer(self)
        self._winws_check_timer.setSingleShot(True)
        self._winws_check_timer.setInterval(3000)
        self._winws_check_timer.timeout.connect(self._check_winws_alive)
        self._winws_check_timer.start()

    def _check_winws_alive(self):
        if not self.runner:
            return
        if not self.runner.check_alive():
            self.tray.showMessage(
                "Zapret Manager",
                "winws.exe упал после запуска. Смотрите лог.",
                QSystemTrayIcon.Critical,
                5000,
            )
            self._refresh_winws_menu()

    def on_item_double_clicked(self, item: QListWidgetItem):
        self._apply_from_item(item)

    # ---------- Трей ----------

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._tray_click_timer.start()
        elif reason == QSystemTrayIcon.DoubleClick:
            if self._tray_click_timer.isActive():
                self._tray_click_timer.stop()
            self.quit_app()

    def show_from_tray(self):
        if self.windowState() & Qt.WindowState.WindowMinimized:
            self.setWindowState(
                self.windowState() & ~Qt.WindowState.WindowMinimized
                | Qt.WindowState.WindowActive
            )
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        self.hide()

    def quit_app(self):
        self._really_quit = True
        self.close()

    # ---------- События окна ----------

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMinimized:
                event.ignore()
                self.hide()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        if not self._really_quit and self.tray.isVisible():
            event.ignore()
            self.hide_to_tray()
            return

        if self.worker and self.worker.isRunning():
            self.log_line(">>> Закрытие: останавливаю сканирование...")
            self.worker.request_stop()
            self.worker.wait(5000)
        if self.worker:
            self.worker.quit()
            self.worker.wait(2000)

        if self.downloader and self.downloader.isRunning():
            self.log_line(">>> Закрытие: отменяю скачивание...")
            self.downloader.request_stop()
            self.downloader.wait(3000)

        try:
            if self.runner:
                self.runner.stop()
        except Exception:
            pass
        try:
            kill_all_winws()
        except Exception:
            pass
        try:
            stop_zapret_service()
        except Exception:
            pass
        try:
            if self.tg_runner:
                self.tg_runner.stop()
        except Exception:
            pass

        try:
            save_settings(self.settings)
        except Exception:
            pass

        self.tray.hide()
        event.accept()

        QTimer.singleShot(1500, lambda: os._exit(0))