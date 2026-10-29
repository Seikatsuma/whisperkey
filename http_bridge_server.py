#!/usr/bin/env python3
"""HTTP bridge: exposes speech_engine (Deepgram nova-2 -> Groq whisper-large-v3
-> local model, profile DICTATION) as an OpenAI-compatible
`POST /audio/transcriptions` endpoint.

Written for claudecodeui's voice input (`server/modules/voice/voice.service.ts`),
which is a generic OpenAI-compatible STT proxy: it POSTs multipart/form-data
(`file` + `model`) to `{VOICE_API_BASE_URL}/audio/transcriptions` and expects
JSON `{"text": "..."}` back. Pointing `VOICE_API_BASE_URL` at this bridge
(`http://127.0.0.1:<port>`) swaps the previous "talk to Groq directly" wiring
for the full cascade + punctuation/term pass + hallucination filter that
speech_engine already gives every other consumer (thoughts-bot, toki-bot,
WhisperKey) — see speech-engine/agent.md.

Design notes (why stdlib-only, why threaded):
- No new Python dependency. speech_engine itself needs only numpy+requests,
  both already present in the system /usr/bin/python3 on this host (same
  interpreter claude-tg-bot's TRANSCRIBE_BIN already uses without a venv —
  see agent.md "Доставка пакета до потребителей"). FastAPI is available too,
  but its multipart handling needs `python-multipart`, which is NOT installed
  and this host's Python is externally-managed (installing it means either
  sudo + --break-system-packages or a new venv just for one endpoint) — not
  worth it for one endpoint accepting one file field. `http.server` +
  ~30 lines of manual multipart parsing avoids all of that.
- `ThreadingHTTPServer` (since 21.10.26): single-threaded made /health go
  silent during recognition and the Mac watchdog killed the tunnel; the
  /audio/stream request also holds its thread for the whole recording. The
  shared `Context` is not formally thread-safe; overlapping requests can race
  only on throttle/blocked-until/diagnostic fields (worst case: an extra
  request or a mixed-up `last_reason`), measured 21.10: two parallel
  recognize calls neither serialize nor corrupt text. Deliberately no global
  lock: it would make one person's dictation wait for another's.
- Every ffmpeg invocation is prefixed with `nice -n 19 ionice -c 3` (CLAUDE.md
  red line for CPU-heavy work on this shared VPS) — done here explicitly
  rather than by calling `speech_engine.decode.decode_to_pcm16k` (which does
  not have that prefix and is package code this task does not touch).

Environment variables (set via systemd Environment=, see
../deploy/voice-bridge.service — never hardcoded, never read from another
project's .env by this process):
  GROQ_API_KEY        - required for the Groq stage of the cascade
  DEEPGRAM_API_KEY     - required for the Deepgram stage (first, if present)
  VOICE_BRIDGE_HOST    - default 127.0.0.1
  VOICE_BRIDGE_PORT    - default 8092
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

# This file sits at the repository root next to speech_engine/ — put the
# repo root on sys.path so `import speech_engine` works without install.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from speech_engine.engine import Engine  # noqa: E402
from speech_engine.profiles import DICTATION  # noqa: E402
from speech_engine.groq_engine import transcribe_groq  # noqa: E402

HOST = os.environ.get("VOICE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_BRIDGE_PORT", "8092"))
SAMPLE_RATE = 16000
# Декодирование идёт заметно быстрее реального времени, но потолок всё равно
# должен масштабироваться с длиной записи: 120 с хватало на голосовую заметку и
# обрывало часовую диктовку на середине, а вызывающая сторона видела это как
# «расшифровка не сработала». Настраивается через окружение, чтобы предел можно
# было поднять без правки кода.
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("VOICE_BRIDGE_FFMPEG_TIMEOUT_SECONDS", "900"))
# Потолок длины диктовки. 300 с (13.10) было взято как страховка от мусорных
# файлов, декодирующихся бесконечно, но 14.10 отрезало живую 366-с диктовку
# Егора — а телефон, получив 502, крутил её в очереди каждые полторы минуты.
# Верхняя граница упирается в память, а не в движок: при MemoryMax=200M пик
# запроса — PCM float32 весь + упаковка для облака — это ~190k байт на секунду
# записи, значит ~600 с — максимум, который ещё держится под лимитом службы.
# Больше — только после потоковой упаковки звука (см. agent.md).
MAX_AUDIO_SECONDS = int(os.environ.get("VOICE_BRIDGE_MAX_AUDIO_SECONDS", "600"))


class AudioTooLongError(RuntimeError):
    """Запись длиннее MAX_AUDIO_SECONDS: ответ 413, а не 502 — чтобы клиент не
    крутил такую запись в очереди бесконечно (5xx телефон считает обрывом
    связи и повторяет отправку; история 12–14.10 — одна запись днями)."""

class EmptyAudioError(RuntimeError):
    """Декодирование дало 0 сэмплов — ответ 400."""


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-bridge")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")

if not GROQ_API_KEY and not DEEPGRAM_API_KEY:
    log.warning("Ни GROQ_API_KEY, ни DEEPGRAM_API_KEY не заданы в окружении — "
                "распознавание не сработает (обе облачные ступени каскада пропущены, "
                "локальная модель не подключена по умолчанию).")

# Один Engine на процесс — так и задуман пакет (agent.md, "Состояние — не
# модульные глобальные"): Context/CloudState/Throttle живут здесь и делятся
# между последовательными запросами этого бриджа, как у любого другого
# бота-потребителя пакета.
engine = Engine(
    profile=DICTATION,
    groq_api_key=GROQ_API_KEY,
    deepgram_api_key=DEEPGRAM_API_KEY,
    sample_rate=SAMPLE_RATE,
)


def decode_to_pcm16k_niced(path: str) -> np.ndarray:
    """То же самое, что speech_engine.decode.decode_to_pcm16k (ffmpeg -> mono
    16кГц s16le -> float32), но с обязательным nice/ionice префиксом. Не
    зовём decode_to_pcm16k напрямую и не правим её саму — она не в мандате
    этой задачи, а требование CLAUDE.md про nice/ionice — про код ЭТОГО
    сервиса."""
    fd, tmp_path = tempfile.mkstemp(suffix=".raw", prefix="voice-bridge-")
    os.close(fd)
    try:
        r = subprocess.run(
            ["nice", "-n", "19", "ionice", "-c", "3", "ffmpeg", "-y",
             "-hide_banner", "-loglevel", "error", "-i", path,
             "-t", str(MAX_AUDIO_SECONDS), "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-f", "s16le", tmp_path],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg не смог декодировать аудио: {r.stderr.strip()[:300]}")
        # memmap: int16 остаётся на диске, в память уходит только итоговый
        # float32 — на длинной записи это ровно вдвое меньше пикового расхода.
        raw = np.memmap(tmp_path, dtype=np.int16)
        if len(raw) >= MAX_AUDIO_SECONDS * SAMPLE_RATE:
            raise AudioTooLongError(
                f"аудио длиннее {MAX_AUDIO_SECONDS} с или повреждено: декодирование остановлено"
            )
        audio = np.asarray(raw, dtype=np.float32)
        audio /= 32768.0
        return audio
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


class _LazyDecode:
    """Будущий результат декода, который стартует ffmpeg только при первом
    result(). На счастливом пути (Deepgram дал текст) декод не нужен — и CPU
    вообще не тратится; контракт тот же, что у concurrent.futures.Future
    (dictation зовёт только .result())."""

    def __init__(self, fn, arg):
        self._fn, self._arg = fn, arg
        self._pool = None
        self._fut = None

    def result(self):
        if self._fut is None:
            self._pool = ThreadPoolExecutor(max_workers=1)
            self._fut = self._pool.submit(self._fn, self._arg)
        try:
            return self._fut.result()
        finally:
            self._pool.shutdown(wait=False)


def probe_duration(path: str) -> float | None:
    """Длительность контейнера по метаданным ffprobe (~30-50 мс, без декода).

    Нужна проверке «редкого ответа» в dictation на пути deepgram_upload: раньше
    её давал только полный ffmpeg-декод, ради которого ответ ждал лишние секунды
    (замер 22.10.26: webm 15с — 5-9с на декод под нагрузкой, при том что сам
    Deepgram отвечает за ~2.3с). None = прочитать не удалось, вызывающий решает.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        value = float(r.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


# Контейнеры, которые Deepgram разбирает сам (проверено 22.10.26 боем: webm/opus
# браузера, WAV, FLAC — текст тот же, что после ffmpeg). Остальное — только через
# декодирование, как раньше.
_DIRECT_MIME_BY_EXT = {
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".mp4": "audio/mp4",
}


def direct_mime(filename: str, content_type: str) -> str | None:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("audio/") or ct in ("video/webm", "video/mp4"):
        return ct.replace("video/", "audio/")
    return _DIRECT_MIME_BY_EXT.get(os.path.splitext(filename or "")[1].lower())


def parse_multipart(body: bytes, boundary: bytes) -> dict:
    """Минимальный ручной парсер multipart/form-data — только то, что нужно
    этому эндпоинту (текстовые поля + один файл под именем "file"). Осознанный
    выбор вместо `python-multipart`/`cgi` (устарел, будет удалён из stdlib) —
    см. докстринг модуля."""
    parts = body.split(b"--" + boundary)
    fields: dict[str, str] = {}
    files: dict[str, dict] = {}
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        content = content[:-2] if content.endswith(b"\r\n") else content
        headers = {}
        for line in header_blob.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
        disp = headers.get("content-disposition", "")
        name = None
        filename = None
        for chunk in disp.split(";"):
            chunk = chunk.strip()
            if chunk.startswith("name="):
                name = chunk[len("name="):].strip('"')
            elif chunk.startswith("filename="):
                filename = chunk[len("filename="):].strip('"')
        if name is None:
            continue
        if filename is not None:
            files[name] = {
                "filename": filename,
                "content": content,
                "content_type": headers.get("content-type", "application/octet-stream"),
            }
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    return {"fields": fields, "files": files}


def _read_term_files() -> dict:
    """Содержимое data/learned_keyterms.json и data/term_fixes.json.
    Файлы читаются как текст: клиент пишет их к себе как есть, формат —
    источник истины в speech_engine (terms/deepgram_engine)."""
    out = {}
    data_dir = Path(__file__).resolve().parent.parent / "data"
    for name in ("learned_keyterms.json", "term_fixes.json"):
        try:
            out[name.rsplit(".json", 1)[0]] = json.loads(
                (data_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "voice-bridge/1.0"

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send_json(200, {
                "ok": True,
                "profile": "dictation",
                "groq_key_set": bool(GROQ_API_KEY),
                "deepgram_key_set": bool(DEEPGRAM_API_KEY),
            })
            return
        if self.path.rstrip("/") == "/terms":
            # Свежий словарь для клиентов: Mac WhisperKey тянет раз в ~30 мин,
            # чтобы прямой путь (когда мост недоступен) работал с теми же
            # keyterm и term_fixes, что серверные — словарь единый.
            self._send_json(200, _read_term_files())
            return
        self._send_json(404, {"error": "not found"})

    @staticmethod
    def _decode(path: str):
        audio = decode_to_pcm16k_niced(path)
        dur = len(audio) / SAMPLE_RATE
        if dur <= 0:
            raise EmptyAudioError()
        return audio, dur

    def _iter_body(self):
        """Тело запроса кусками по мере прихода: chunked (поток с Mac) или
        Content-Length. Обрыв клиента — ConnectionError наружу."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    raise ConnectionError("клиент оборвал поток")
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return
                data = self.rfile.read(size)
                if len(data) < size:
                    raise ConnectionError("клиент оборвал поток")
                self.rfile.readline(1024)
                yield data
        else:
            left = int(self.headers.get("Content-Length", "0") or 0)
            while left > 0:
                data = self.rfile.read(min(left, 65536))
                if not data:
                    raise ConnectionError("клиент оборвал поток")
                left -= len(data)
                yield data

    def _handle_stream(self):
        """POST /audio/stream — звук идёт во время записи (WhisperKey, 22.10.26).

        Тело: PCM s16le моно 16 кГц, chunked, всю запись. К концу записи звук уже
        на сервере — после отпускания остаётся только распознавание: тот же
        каскад, а Deepgram получает WAV без темпа (эталон SaluteSpeech: 11.27%
        против 11.97% с темпом). Потоковый сокет Deepgram пробовали и отвергли:
        на том же эталоне 17.4% ошибок против 12.1% (agent.md)."""
        if self.headers.get("X-Sample-Rate", str(SAMPLE_RATE)) != str(SAMPLE_RATE):
            self._send_json(400, {"error": f"нужен PCM s16le {SAMPLE_RATE} Гц"})
            return
        self.connection.settimeout(60)
        max_bytes = MAX_AUDIO_SECONDS * SAMPLE_RATE * 2
        pcm = bytearray()
        too_long = False
        # Потолок по времени, не только по объёму: тело, тянущееся дольше
        # двойного предела длины записи, — не диктовка, а зависший клиент.
        deadline = time.monotonic() + MAX_AUDIO_SECONDS * 2
        # Потоковый второй проход (28.10.26): законченные 30с куски уходят в
        # Groq ПОКА идёт запись — к концу диктовки слиянию остаётся дождаться
        # только хвост, а не всю запись целиком.
        STEP_BYTES = 28 * SAMPLE_RATE * 2
        CH_BYTES = 30 * SAMPLE_RATE * 2   # окно 30с, шаг 28с: нахлёст 2с,
        # чтобы слово на стыке целиком попало в следующий кусок
        ctx = engine.ctx
        gpool = ThreadPoolExecutor(max_workers=3) \
            if ctx.groq_api_key and not ctx.cloud_state.is_blocked else None
        gfuts, gfed = [], 0

        def _groq_one(part_bytes, off_sec):
            part = np.frombuffer(part_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            t, ws = transcribe_groq(
                part, api_key=ctx.groq_api_key, profile=ctx.profile,
                sample_rate=ctx.sample_rate, state=ctx.cloud_state,
                throttle=ctx.throttle, session=ctx.session,
                allow_retry=False, use_prompt=False, return_words=True)
            for w in ws or []:
                w["start"] = (w.get("start") or 0.0) + off_sec
                w["end"] = (w.get("end") or 0.0) + off_sec
            return t, ws or []

        try:
            for data in self._iter_body():
                if time.monotonic() > deadline:
                    raise TimeoutError("поток дольше предела")
                # Сверх потолка тело дочитываем впустую — клиент получит 413, а не обрыв.
                if too_long or len(pcm) + len(data) > max_bytes:
                    too_long = True
                    continue
                pcm += data
                if gpool is not None:
                    while len(pcm) - gfed >= CH_BYTES:
                        chunk = bytes(pcm[gfed:gfed + CH_BYTES])
                        gfuts.append(gpool.submit(
                            _groq_one, chunk, gfed / 2 / SAMPLE_RATE))
                        gfed += STEP_BYTES
        except Exception as e:
            log.info("stream: запрос оборван (%s) — не распознаю", type(e).__name__)
            return
        if too_long:
            self._send_json(413, {"error": f"аудио длиннее {MAX_AUDIO_SECONDS} с"})
            return
        groq_future = None
        if gpool is not None:
            if len(pcm) > gfed:
                gfuts.append(gpool.submit(
                    _groq_one, bytes(pcm[gfed:]), gfed / 2 / SAMPLE_RATE))

            def _gather_groq():
                texts, words = [], []
                for i, f in enumerate(gfuts):
                    try:
                        t, ws = f.result(timeout=10)
                    except Exception:
                        continue
                    if t:
                        texts.append(t)
                    last = i == len(gfuts) - 1
                    for w in ws:
                        # нахлёст 2с: слова куска берём только в его
                        # неперекрытой зоне — стыковое слово полное в
                        # следующем куске, дублей на шве нет
                        if last or w["start"] < i * 28.0 + 28.0:
                            words.append(w)
                words.sort(key=lambda w: w["start"])
                return " ".join(texts) or None, words
            groq_future = gpool.submit(_gather_groq)
        t_end = time.monotonic()
        if len(pcm) % 2:
            del pcm[-1]
        dur = len(pcm) / 2 / SAMPLE_RATE
        if dur < 0.3:
            self._send_json(400, {"error": "decoded audio is empty"})
            return
        try:
            audio = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32) / 32768.0
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm)
            del pcm
            result = engine.recognize(audio, dur, deepgram_upload=(buf.getvalue(), "audio/wav"),
                                      groq_future=groq_future)
            fallbacks = result.meta.get("fallbacks") or []
            log.info("stream %.2fs -> engine=%s chars=%d after_end=%.2fs%s | %s",
                     dur, result.engine, len(result.text), time.monotonic() - t_end,
                     f" fallbacks={fallbacks}" if fallbacks else "",
                     result.text[:300])
            self._send_json(200, {"text": result.text, "engine": result.engine, "fallbacks": fallbacks})
        except Exception as e:
            log.exception("stream transcription failed")
            self._send_json(502, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        # Замолчавший посреди тела клиент не держит поток вечно.
        self.connection.settimeout(120)
        if self.path.rstrip("/") == "/audio/stream":
            self._handle_stream()
            return
        if self.path.rstrip("/") != "/audio/transcriptions":
            self._send_json(404, {"error": "not found"})
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self._send_json(400, {"error": "expected multipart/form-data with a boundary"})
            return
        boundary = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "empty body (Content-Length missing or zero)"})
            return
        body = self.rfile.read(length)

        try:
            parsed = parse_multipart(body, boundary.encode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": f"bad multipart body: {e}"})
            return

        file_part = parsed["files"].get("file")
        if file_part is None:
            self._send_json(400, {"error": "no 'file' field in multipart body"})
            return

        suffix = os.path.splitext(file_part["filename"] or "")[1] or ".webm"
        tmp_path = os.path.join(tempfile.gettempdir(), f"voice-bridge-{uuid.uuid4().hex}{suffix}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(file_part["content"])

            t0 = time.monotonic()
            mime = direct_mime(file_part["filename"], file_part["content_type"])
            if mime:
                # Файл как есть — в Deepgram; ffmpeg вообще не запускается, пока
                # звук реально не понадобился (пустой ответ → повтор WAV,
                # подозрительный хвост, фолбэки) — ответ уходит по времени
                # Deepgram. Замер 22.10.26: Deepgram отвечает по webm за ~2.3с,
                # а ожидание декода добавляло 3-19с — это и был «фриз» кнопки.
                # Плата: неуспешный путь ждёт декод с нуля — он редкий и и так
                # медленный.
                dur = probe_duration(tmp_path) or 0.0
                result = engine.recognize(None, dur or None,
                                          deepgram_upload=(file_part["content"], mime),
                                          audio_future=_LazyDecode(self._decode, tmp_path))
            else:
                audio, dur = self._decode(tmp_path)
                result = engine.recognize(audio, dur)
            log.info("recognized %.2fs input (%s) -> engine=%s chars=%d in %.2fs | %s",
                      dur, mime or "decode", result.engine, len(result.text), time.monotonic() - t0,
                      result.text[:300])
            self._send_json(200, {"text": result.text})
        except EmptyAudioError:
            self._send_json(400, {"error": "decoded audio is empty"})
        except AudioTooLongError as e:
            log.warning("transcription rejected: %s", e)
            self._send_json(413, {"error": str(e)})
        except Exception as e:
            log.exception("transcription failed")
            self._send_json(502, {"error": f"{type(e).__name__}: {e}"})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def main() -> None:
    # Threading: иначе /health не отвечает, пока идёт распознавание (до ~20с),
    # и сторож на Mac'е убивает туннель посреди живого запроса.
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("voice-bridge слушает http://%s:%d (groq_key_set=%s deepgram_key_set=%s)",
              HOST, PORT, bool(GROQ_API_KEY), bool(DEEPGRAM_API_KEY))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
