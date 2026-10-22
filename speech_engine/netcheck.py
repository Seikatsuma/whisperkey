"""Быстрый ответ на вопрос «интернет вообще есть?» — чтобы каскад не тратил
десятки секунд на повторы облачных ступеней, когда сети нет совсем.

Параллельный TCP-connect к трём независимым адресам без DNS (Cloudflare,
Google, Яндекс): DNS при обрыве сам по себе может висеть секундами. «Сети нет» —
только если НЕ ответил НИ ОДИН за `timeout`. Медленная, но живая сеть отвечает
на connect за десятки миллисекунд → считается живой, облачные ступени идут
как раньше. Результат кэшируется на CACHE_SEC: одна диктовка не платит пробу
дважды. Вызывать только ПОСЛЕ неудачи ступени — на счастливом пути проба не нужна.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request

PROBE_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("77.88.8.8", 443))
CACHE_SEC = 5.0

_cache = {"t": 0.0, "down": False}
_lock = threading.Lock()


def _reachable(host: str, port: int, timeout: float, hit: threading.Event) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            hit.set()
    except OSError:
        pass


def _proxy_configured() -> bool:
    # requests ходит через системный/переменный прокси сам, а голый сокет — нет:
    # в сети «только через прокси» проба дала бы ложный офлайн и отрезала облако.
    # Прокси есть — офлайн никогда не объявляем, каскад работает как раньше.
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return False
    return any(proxies.get(k) for k in ("https", "http", "all"))


def internet_down(timeout: float = 1.0) -> bool:
    """True — ни один из PROBE_HOSTS не принял соединение за timeout
    (и прокси не настроен)."""
    if _proxy_configured():
        return False
    with _lock:
        if time.monotonic() - _cache["t"] < CACHE_SEC:
            return _cache["down"]
    hit = threading.Event()
    threads = [threading.Thread(target=_reachable, args=(h, p, timeout, hit), daemon=True)
               for h, p in PROBE_HOSTS]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    while not hit.is_set() and time.monotonic() < deadline and any(t.is_alive() for t in threads):
        hit.wait(0.02)
    down = not hit.is_set()
    with _lock:
        _cache["t"], _cache["down"] = time.monotonic(), down
    return down


def reset_cache() -> None:
    with _lock:
        _cache["t"] = 0.0
