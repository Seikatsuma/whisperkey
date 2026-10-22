"""Потоковая ступень Deepgram (WebSocket): звук уходит во время записи, после
отпускания кнопки остаётся только Finalize — ~0.1–0.2 с вместо 1–3 с на целый
файл (замер 21–22.10.26 на корпусе WhisperKey, `/tmp/asr-lab/run_stream.py`).

Используется мостом (`http_bridge`, маршрут `/audio/stream`): клиент шлёт PCM
кусками, мост держит один сокет Deepgram на диктовку. Полный текст — только из
is_final-результатов по порядку start. Текст здесь сырой: проверки качества и
таблица названий — `dictation.recognize_streamed`, она же решает, не нужен ли
обычный каскад по накопленному звуку.

Зависимость: `websocket-client` (импорт ленивый — без пакета ступень просто
не стартует, `start()` вернёт False, каскад работает как раньше).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlencode

logger = logging.getLogger("speech_engine.deepgram_stream")

WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramStreamer:
    """Один сеанс диктовки = один сокет. `feed_pcm16` и `finish` зовёт один поток
    (обработчик запроса), читатель ответов — свой фоновый поток."""

    def __init__(self, api_key: str, params: dict, sample_rate: int = 16000,
                 connect_timeout: float = 5.0):
        p = dict(params)
        p.update(encoding="linear16", sample_rate=str(sample_rate), channels="1",
                 interim_results="true")
        self.url = WS_URL + "?" + urlencode(p)
        self.api_key = api_key
        self.connect_timeout = connect_timeout
        self._finals: list[dict] = []
        self._last_final_t: float | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._ws = None
        self.error = ""

    def start(self) -> bool:
        try:
            import websocket
        except ImportError:
            self.error = "нет websocket-client"
            return False
        try:
            self._ws = websocket.create_connection(
                self.url, header=[f"Authorization: Token {self.api_key}"],
                timeout=self.connect_timeout, skip_utf8_validation=True)
            self._ws.settimeout(None)
        except Exception as e:
            self.error = f"подключение {type(e).__name__}"
            self._ws = None
            return False
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    @property
    def alive(self) -> bool:
        return self._ws is not None and not self._closed

    def feed_pcm16(self, pcm: bytes) -> None:
        if not self.alive or not pcm:
            return
        try:
            self._ws.send_binary(pcm)
        except Exception as e:
            self.error = f"отправка {type(e).__name__}"
            self._closed = True

    def _read_loop(self) -> None:
        try:
            while True:
                msg = self._ws.recv()
                if not msg:
                    if self._closed:
                        break
                    continue
                m = json.loads(msg)
                if m.get("type") == "Results" and m.get("is_final"):
                    alt = (m.get("channel", {}).get("alternatives") or [{}])[0]
                    with self._lock:
                        self._finals.append({
                            "start": m.get("start") or 0.0,
                            "text": (alt.get("transcript") or "").strip(),
                            "words": alt.get("words") or [],
                        })
                        self._last_final_t = time.monotonic()
        except Exception as e:
            if not self._closed:
                self.error = self.error or f"чтение {type(e).__name__}"
        finally:
            self._closed = True

    def finish(self, timeout: float = 8.0) -> tuple[str, float, bool]:
        """Finalize → ждём финалы → CloseStream.

        Возвращает (текст, конец_последнего_слова_с, ok). ok=False — сокет умер
        до конца или финалы не пришли: вызывающий обязан уйти в обычный каскад.
        """
        if self._ws is None:
            return "", 0.0, False
        ok = self.alive
        t_sent = time.monotonic()
        try:
            self._ws.send(json.dumps({"type": "Finalize"}))
        except Exception as e:
            self.error = self.error or f"finalize {type(e).__name__}"
            ok = False
        got_final = False
        deadline = t_sent + timeout
        while ok and time.monotonic() < deadline:
            with self._lock:
                lf = self._last_final_t
            # Финал после Finalize пришёл и 0.3 с тишины — хвост собран.
            if lf and lf >= t_sent and time.monotonic() - lf > 0.3:
                got_final = True
                break
            if self._closed:
                break
            time.sleep(0.02)
        if not got_final:
            with self._lock:
                lf = self._last_final_t
            got_final = bool(lf and lf >= t_sent)
            if not got_final:
                self.error = self.error or "нет финала после Finalize"
        try:
            self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
        with self._lock:
            finals = sorted(self._finals, key=lambda r: r["start"])
        text = " ".join(f["text"] for f in finals if f["text"]).strip()
        last_end = 0.0
        for f in finals:
            if f["words"]:
                last_end = max(last_end, float(f["words"][-1].get("end") or 0.0))
        return text, last_end, ok and got_final

    def abort(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
