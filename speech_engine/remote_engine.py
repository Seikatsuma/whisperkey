"""Ступень «удалённый каскад»: звук уходит на HTTP-мост `http_bridge/`, который
прогоняет ТОТ ЖЕ dictation-каскад (Deepgram → Groq → local) на стороне сервера.

Зачем (04.10.26): у Егора на Mac'е запасная ступень каскада (Groq) мёртва —
гео-блок 403 с домашнего IP (живой инцидент 11.08.26, см. agent.md). Итог:
любой сбой Deepgram из дома уронит диктовку прямо в локальную модель — и по
качеству (small на CPU, ~14% ошибок против nova-2), и по скорости. Мост на
сервере держит полный каскад с рабочими Deepgram и Groq — удалённая ступень
отдаёт качество «как через сервер» дословно.

Включается адресом: `REMOTE_ASR_URL` (например,
`http://127.0.0.1:8092/audio/transcriptions` через SSH-туннель к серверу) —
пусто = ступени нет вообще, никаких лишних запросов. `REMOTE_ASR_TOKEN` —
заголовок `X-Bridge-Token`, если мост выставлен наружу с проверкой (через
туннель на 127.0.0.1 не нужен).

Возвращает только текст (мост отдаёт `{"text": ...}` — свои chunk-метки и
words[] у него внутри). Темп НЕ применяем: сервер применит свой asr_tempo
внутри своего каскада — двойное ускорение 1.03×1.03 дало бы ~1.06.
"""
from __future__ import annotations

import io
import logging
import os
import queue
import threading
import time

import numpy as np

from .audio import create_upload_audio
from .context import Context

logger = logging.getLogger("speech_engine.remote")

REMOTE_TIMEOUT = (5.0, 120.0)   # connect, read: каскад на длинной записи ~10с


def transcribe_remote(audio: np.ndarray, dur: float, ctx: Context) -> str:
    """Один запрос к мосту на ВСЮ запись (он сам дробит, если надо).
    Любой отказ → "", ступень просто пропускается, дальше и так есть локальная
    модель. Единственный повтор — после исключения соединения, когда клиентский
    `remote_reviver` (сторож туннеля на Mac) уже успел поднять мост обратно."""
    url = ctx.remote_asr_url
    if not url:
        # Сторож уже мог снять адрес после упавшей пробы, а туннель к моменту
        # записи восстановиться. Если клиент дал reviver — спрашиваем мост прямо
        # сейчас, не дожидаясь следующего тика сторожа.
        reviver = getattr(ctx, "remote_reviver", None)
        if reviver is None:
            return ""
        try:
            if not reviver():
                return ""
        except Exception:
            return ""
        url = ctx.remote_asr_url
        if not url:
            return ""
    upload = create_upload_audio(audio, ctx.sample_rate, tempo=1.0)
    if not upload:
        return ""
    payload, name, mime = upload
    headers = {}
    if ctx.remote_asr_token:
        headers["X-Bridge-Token"] = ctx.remote_asr_token

    def _post() -> str:
        files = {"file": (name, io.BytesIO(payload), mime)}
        r = ctx.session.post(ctx.remote_asr_url, files=files, headers=headers,
                             timeout=REMOTE_TIMEOUT)
        if r.status_code != 200:
            ctx.remote_last_error = f"статус {r.status_code}"
            return ""
        text = (r.json().get("text") or "").strip()
        ctx.remote_last_error = "" if text else "пустой ответ"
        if text:
            maybe_sync_terms(ctx)
        return text

    try:
        text = _post()
        if not text:
            logger.warning("remote: %s, отказ", ctx.remote_last_error)
        return text
    except Exception as e:
        ctx.remote_last_error = f"исключение {type(e).__name__}"
        logger.warning("remote: исключение %s", type(e).__name__)

    # Туннель мог умереть посреди запроса: сторож переподнимает его за секунды,
    # но без повтора запись уезжала в локальную модель на ~50с (живой журнал
    # 22.10.26: обрыв домашнего интернета на Mac → 3 записи подряд по 48-61с
    # через local, хотя туннель каждый раз поднимался обратно). Даём
    # поднявшемуся мосту ОДИН повтор — дёшево против цены локальной модели.
    reviver = getattr(ctx, "remote_reviver", None)
    if reviver is None:
        return ""
    try:
        revived = bool(reviver())
    except Exception:
        revived = False
    if not revived or not ctx.remote_asr_url:
        return ""
    try:
        ctx.session.close()   # в пуле лежит мёртвый сокет старого туннеля
    except Exception:
        pass
    try:
        text = _post()
        if text:
            logger.warning("remote: повтор после поднятия туннеля — успех")
        else:
            logger.warning("remote: повтор после поднятия туннеля — %s",
                           ctx.remote_last_error)
        return text
    except Exception as e:
        ctx.remote_last_error = f"исключение {type(e).__name__}"
        logger.warning("remote: повтор после поднятия туннеля — %s", type(e).__name__)
        return ""


_TERMS_SYNC_INTERVAL = 30 * 60   # раз в полчаса — словарь ночной, чаще не надо
_terms_last_sync = 0.0


def maybe_sync_terms(ctx) -> None:
    """Тянет свежий словарь с моста (GET /terms → learned_keyterms +
    term_fixes). Зачем (28.10.26): когда мост жив, прямой путь почти не
    используется, но при его падении Mac идёт в Deepgram напрямую — и там
    работает со словарём на момент последнего git pull. Синк держит прямой
    путь на тех же терминах, что серверный. Пишет файлы только при изменении;
    оба читателя (terms/deepgram_engine) перечитывают по mtime сами.
    Вызывается из точек обращения к мосту; сам — фоновый поток."""
    global _terms_last_sync
    if ctx is None or not getattr(ctx, "remote_asr_url", ""):
        return
    now = time.time()
    if now - _terms_last_sync < _TERMS_SYNC_INTERVAL:
        return
    _terms_last_sync = now

    def _sync():
        try:
            import json as _json
            from pathlib import Path as _P
            base = ctx.remote_asr_url.rsplit("/audio/transcriptions", 1)[0].rstrip("/")
            http = ctx.session or __import__("requests")
            resp = http.get(f"{base}/terms", timeout=5.0)
            if resp.status_code != 200:
                return
            data = resp.json()
            data_dir = _P(__file__).resolve().parent.parent / "data"
            for key, name in (("learned_keyterms", "learned_keyterms.json"),
                              ("term_fixes", "term_fixes.json")):
                if key not in data:
                    continue
                path = data_dir / name
                new = _json.dumps(data[key], ensure_ascii=False, indent=1)
                try:
                    old = path.read_text(encoding="utf-8")
                except OSError:
                    old = ""
                if old.strip() != new.strip():
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(new + "\n", encoding="utf-8")
                    tmp.replace(path)
                    logger.info("remote: словарь обновлён с моста (%s)", name)
        except Exception as e:
            logger.info("remote: синк словаря не прошёл (%s)", type(e).__name__)

    threading.Thread(target=_sync, daemon=True).start()


class RemoteStream:
    """Потоковая отправка звука на мост ВО ВРЕМЯ записи (22.10.26).

    Один POST на `/audio/stream` с телом chunked: PCM s16le моно `sample_rate`
    кусками по мере записи. К отпусканию кнопки звук уже на сервере: мост сразу
    гонит обычный каскад (Deepgram — целым WAV без темпа) и отдаёт `{"text": ...}`.
    Из ожидания после записи уходят кодирование FLAC и отправка файла по
    домашнему каналу; само распознавание остаётся тем же по качеству.

    Любой отказ (старый мост без маршрута, обрыв туннеля, пустой ответ) —
    `result()` вернёт "" и вызывающий идёт прежним путём `Engine.recognize`
    по уже записанному звуку: поток может только ускорить диктовку.
    `feed` зовётся из аудио-коллбэка — только неблокирующая очередь.
    """

    def __init__(self, url: str, *, session, sample_rate: int = 16000, token: str = "",
                 timeout: tuple[float, float] = REMOTE_TIMEOUT, ctx=None):
        self.url = url.rsplit("/audio/transcriptions", 1)[0].rstrip("/") + "/audio/stream"
        self.session = session
        self.sample_rate = sample_rate
        self.token = token
        self.timeout = timeout
        self._q: queue.Queue = queue.Queue()
        self._aborted = False
        self._text = ""
        self.error = ""
        self.engine = ""
        self.fallbacks: list = []
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.ctx = ctx

    def start(self) -> "RemoteStream":
        maybe_sync_terms(self.ctx)
        self._thread.start()
        return self

    def feed(self, block: np.ndarray) -> None:
        if self._aborted:
            return
        pcm = np.clip(np.asarray(block, dtype=np.float32).reshape(-1), -1.0, 1.0)
        buf = np.empty(pcm.shape, dtype=np.int16)
        np.multiply(pcm, 32767, out=buf, casting="unsafe")
        self._q.put_nowait(buf.tobytes())

    def close(self) -> None:
        """Конец записи: тело запроса закрывается, мост финализирует."""
        self._q.put_nowait(None)

    def abort(self) -> None:
        """Отмена диктовки: соединение рвётся, мост ничего не распознаёт."""
        self._aborted = True
        self._q.put_nowait(None)

    def _body(self):
        while True:
            item = self._q.get()
            parts = []
            end = item is None
            if not end:
                parts.append(item)
                # Всё, что накопилось, — одним куском (меньше накладных на туннель).
                while True:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        end = True
                        break
                    parts.append(nxt)
            if self._aborted:
                raise RuntimeError("отменено")
            if parts:
                yield b"".join(parts)
            if end:
                return

    def _run(self) -> None:
        headers = {"Content-Type": "application/octet-stream",
                   "X-Sample-Rate": str(self.sample_rate)}
        if self.token:
            headers["X-Bridge-Token"] = self.token
        try:
            r = self.session.post(self.url, data=self._body(), headers=headers, timeout=self.timeout)
            if r.status_code != 200:
                self.error = f"статус {r.status_code}"
                return
            j = r.json()
            self._text = (j.get("text") or "").strip()
            self.engine = j.get("engine") or ""
            self.fallbacks = list(j.get("fallbacks") or [])
            if not self._text:
                self.error = "пустой ответ"
        except Exception as e:
            self.error = "отменено" if self._aborted else f"исключение {type(e).__name__}"
        finally:
            # Отказ посреди записи: дальнейшие feed просто копятся — освободим.
            self._aborted = True

    def result(self, timeout: float | None = None) -> str:
        self._thread.join(timeout if timeout is not None else self.timeout[1] + 5)
        if self._thread.is_alive():
            self.error = self.error or "нет ответа вовремя"
            self.abort()
            return ""
        return self._text
