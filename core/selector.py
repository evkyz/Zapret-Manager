import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .strategy_parser import Strategy, find_strategies
from .runner import WinwsRunner, kill_all_winws, stop_zapret_service
from .health_check import run_all_checks, score as calc_score


@dataclass
class ScanResult:
    strategy: Strategy
    score: float
    details: list


class ScanCancelled(Exception):
    pass


def scan_all(
    zapret_dir: Path,
    progress: Callable[[int, int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    debug_argv: bool = False,
    on_result: Callable[[Strategy, float, list], None] | None = None,
) -> list[ScanResult]:
    log = log or (lambda s: None)
    stop_event = stop_event or threading.Event()

    def check_stop():
        if stop_event.is_set():
            raise ScanCancelled()

    log("=" * 50)
    log("СТАРТ СКАНИРОВАНИЯ")
    log("=" * 50)

    log("Останавливаю службы zapret/WinDivert...")
    stop_zapret_service()
    kill_all_winws()
    time.sleep(1)

    runner = WinwsRunner(zapret_dir, log_fn=log)
    strategies = find_strategies(zapret_dir, debug=debug_argv)

    total = len(strategies)
    log(f"Найдено стратегий: {total}")

    results: list[ScanResult] = []

    for i, strat in enumerate(strategies, 1):
        try:
            check_stop()
        except ScanCancelled:
            log(">>> Сканирование отменено пользователем")
            break

        if progress:
            progress(i, total, strat.name)

        log("")
        log(f"--- [{i}/{total}] {strat.name} ---")

        try:
            check_stop()
        except ScanCancelled:
            log(">>> Сканирование отменено")
            break

        ok = runner.start_nowait(strat.argv)
        if ok:
            waited = 0.0
            while waited < 3.0:
                if stop_event.is_set():
                    break
                time.sleep(0.1)
                waited += 0.1
            ok = runner.check_alive()

        if not ok:
            log("  → стратегия не запустилась, score=0")
            results.append(ScanResult(strategy=strat, score=0.0, details=[]))
            if on_result:
                on_result(strat, 0.0, [])
            runner.stop()
            continue

        try:
            check_stop()
        except ScanCancelled:
            runner.stop()
            log(">>> Сканирование отменено")
            break

        log("  → делаю health-check...")
        try:
            checks = run_all_checks()
            for c in checks:
                if c.ok is None:
                    status = "SKIP"
                elif c.ok:
                    status = "OK  "
                else:
                    status = "FAIL"
                msg = f"    {status} {c.name:<22} {c.latency_ms:>7.0f} ms"
                if c.ok is not True and c.error:
                    msg += f"   ({c.error})"
                log(msg)

            s = calc_score(checks)
            log(f"  → SCORE = {s}")
            results.append(ScanResult(strategy=strat, score=s, details=checks))
            if on_result:
                on_result(strat, s, checks)
        except Exception as e:
            log(f"  → ошибка health-check: {e}")
            results.append(ScanResult(strategy=strat, score=0.0, details=[]))
            if on_result:
                on_result(strat, 0.0, [])
        finally:
            runner.stop()

    runner.stop()
    kill_all_winws()

    results.sort(key=lambda r: r.score, reverse=True)

    log("")
    log("=" * 50)
    log("ИТОГИ:")
    for r in results:
        log(f"  {r.score:>6.2f}  {r.strategy.name}")
    log("=" * 50)

    return results