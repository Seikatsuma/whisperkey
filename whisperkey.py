#!/usr/bin/env python3
"""
WhisperKey v17.5 - CEO PRECISION RESTORED
- Архитектура: Dual-Stage Pipeline (Cloud Stealth + Stable Offline)
- Качество: Context-Aware Grammar (возврат идеальных окончаний)
- Целостность: Fast Tail Capture (300ms) + VAD Shield (1000ms)
- Стабильность: Hysteresis Cloud Switching + 15s Timeout
"""
from __future__ import annotations

import threading
import subprocess
import os
import sys
import time
import fcntl
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import re
import psutil
import requests
import io
import wave

# Настройки для Intel Mac
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

try:
    import sounddevice as sd
except OSError as e:
    print("\n" + "!"*60)
    print(" ОШИБКА: Библиотека PortAudio не найдена.")
    if sys.platform == "darwin":
        print(" Пожалуйста, установите её командой: brew install portaudio")
    else:
        print(" Пожалуйста, установите PortAudio для вашей системы.")
    print("!"*60 + "\n")
    sys.exit(1)

from faster_whisper import WhisperModel
from pynput import keyboard
from pynput.keyboard import Controller as KeyboardController, Key as KeyboardKey


def load_env_file(path: str = ".env") -> None:
    """Минимальная загрузка .env без внешних зависимостей."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[env warn] {e}")

# ─── Настройки ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_PATH  = "small" # CEO Upgrade: 'base' -> 'small' for significantly better Russian accuracy
TAIL_CAPTURE_SECONDS = 0.6  # CEO Speed: 0.8 -> 0.6 (быстрее старт, риск минимален)
RESTORE_CLIPBOARD = True
SAVE_DEBUG_AUDIO = False  # Speed: без записи WAV на диск (качество 5/5)
ASR_CONTEXT_PROMPT = "Русская деловая речь. IT, программирование, технические задачи."
NARRATOR_LOOP_PATTERN = r'(?:спикер|смикер|speaker)\s+говорит'
BOH_TAIL_MARKERS = [
    "редактор субтитров",
    "корректор",
    "продолжение следует",
    "субтитры сделал",
    "субтитры подогнал",
    "subtitles by",
    "thanks for watching"
]
CLOUD_WHISPER_MODEL = "whisper-large-v3"
PARALLEL_CLOUD_CHUNKS = True
MAX_CLOUD_WORKERS = 4
LLAMA_SKIP_MAX_WORDS = 0
HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "продолжение следует",
    "голос за кадром",
]

# Библиотека эталонных записей на Рабочем столе (для оценки качества)
EVAL_SAMPLES_ENABLED = False
EVAL_SAMPLES_ROOT = os.path.expanduser("~/Desktop/WhisperKey-Eval-Samples")
EVAL_BUCKETS = {
    "eval_samples": {"limit": 7, "label": "Целевые записи (от 20с)"},
}

# API Настройки (Groq Cloud)
load_env_file(".env")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip() or "YOUR_GROQ_API_KEY_HERE"

if GROQ_API_KEY == "YOUR_GROQ_API_KEY_HERE" or not GROQ_API_KEY:
    print("\n" + "!"*60)
    print(" ОШИБКА: API КЛЮЧ НЕ НАЙДЕН")
    print(" Пожалуйста, создайте файл .env и добавьте туда GROQ_API_KEY")
    print(" Инструкция в README.md")
    print("!"*60 + "\n")
    USE_CLOUD = False
else:
    USE_CLOUD = True

CLOUD_ENABLED = USE_CLOUD

# Создаем глобальную сессию для Keep-Alive
http_session = requests.Session()

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False
last_text_context = ""  # Буфер для хранения контекста предыдущей фразы
global_audio_buffer = [] # Постоянный буфер для фонового прослушивания
trigger_held = False
last_trigger_ts = 0.0
TRIGGER_DEBOUNCE_SEC = 0.35
session_counter = 0
state_lock = threading.Lock()
session_phase = "idle"   # idle -> recording -> processing
active_session_id = 0
audio_stream = None # CEO Fix: Инициализируем при нажатии

# CEO Cloud Management: Динамическое управление состоянием облака
cloud_status = {
    "is_blocked": False,
    "last_check_time": 0,
    "check_in_progress": False,
    "consecutive_success": 0  # CEO Fix: Счетчик стабильных запросов
}
kb = KeyboardController()
_instance_lock_handle = None
_eval_lock = threading.Lock()
_eval_pending_paths: dict[int, str] = {}
_eval_full_notified: set[str] = set()


def _eval_bucket_for_duration(dur: float) -> str:
    if dur >= 20.0:
        return "eval_samples"
    return "ignored"

def _write_raw_recording_wav(audio_data: np.ndarray, path: str) -> None:
    """Сохраняет сырую запись (без compress/padding) как слышал микрофон."""
    audio_data = np.asarray(audio_data, dtype=np.float32).flatten()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        max_v = float(np.max(np.abs(audio_data))) if len(audio_data) else 0.0
        if max_v > 0:
            audio_data = audio_data / max_v * 0.98
        wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())

def _eval_count_in_bucket(bucket: str) -> int:
    if bucket == "ignored": return 999
    folder = os.path.join(EVAL_SAMPLES_ROOT, bucket)
    if not os.path.isdir(folder):
        return 0
    return sum(1 for name in os.listdir(folder) if name.lower().endswith(".wav"))

def _eval_refresh_manifest() -> dict:
    global EVAL_SAMPLES_ENABLED
    manifest = {"root": EVAL_SAMPLES_ROOT, "buckets": {}}
    total_collected = 0
    for bucket, cfg in EVAL_BUCKETS.items():
        count = _eval_count_in_bucket(bucket)
        if bucket != "ignored":
            total_collected += count
        manifest["buckets"][bucket] = {
            "label": cfg["label"],
            "count": count,
            "limit": cfg["limit"],
            "full": count >= cfg["limit"],
        }
    
    # CEO Fix: Автоматическое отключение при достижении лимита
    if total_collected >= 7:
        if EVAL_SAMPLES_ENABLED:
            print("[eval] Лимит в 7 записей достигнут. Авто-отключение сбора.")
            EVAL_SAMPLES_ENABLED = False
            
    manifest_path = os.path.join(EVAL_SAMPLES_ROOT, "manifest.json")
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[eval] manifest error: {e}")
    return manifest

def init_eval_samples_library() -> None:
    """Создаёт папки на Рабочем столе и README с правилами сбора."""
    if not EVAL_SAMPLES_ENABLED:
        return
    try:
        os.makedirs(EVAL_SAMPLES_ROOT, exist_ok=True)
        for bucket in EVAL_BUCKETS:
            os.makedirs(os.path.join(EVAL_SAMPLES_ROOT, bucket), exist_ok=True)
        readme = os.path.join(EVAL_SAMPLES_ROOT, "README.txt")
        if not os.path.exists(readme):
            lines = [
                "WhisperKey — эталонные записи для оценки качества",
                "",
                "Папки:",
                "  short_up_to_15s/   — до 5 файлов, длительность ≤ 15 сек",
                "  medium_15s_to_60s/ — до 10 файлов, 15 < длительность ≤ 60 сек",
                "  long_over_60s/     — до 5 файлов, длительность > 60 сек",
                "",
                "Когда лимит категории заполнен, новые записи этой длины не сохраняются.",
                "К каждому .wav добавляется .meta.json (raw whisper + финальный текст).",
                "Статус: manifest.json",
            ]
            with open(readme, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        manifest = _eval_refresh_manifest()
        print(f"[eval] Папка эталонов: {EVAL_SAMPLES_ROOT}")
        for bucket, info in manifest["buckets"].items():
            print(f"[eval]   {info['label']}: {info['count']}/{info['limit']}")
    except Exception as e:
        print(f"[eval] init error: {e}")

def _eval_collect_worker(audio: np.ndarray, dur: float, session_id: int) -> None:
    if not EVAL_SAMPLES_ENABLED:
        return
    bucket = _eval_bucket_for_duration(dur)
    limit = EVAL_BUCKETS[bucket]["limit"]
    folder = os.path.join(EVAL_SAMPLES_ROOT, bucket)
    os.makedirs(folder, exist_ok=True)

    with _eval_lock:
        count = _eval_count_in_bucket(bucket)
        if count >= limit:
            if bucket not in _eval_full_notified:
                _eval_full_notified.add(bucket)
                print(f"[eval] Категория «{EVAL_BUCKETS[bucket]['label']}» полна ({limit}/{limit}), пропуск")
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fname = f"{stamp}_{dur:.1f}s_id{session_id}.wav"
        wav_path = os.path.join(folder, fname)
        try:
            _write_raw_recording_wav(audio, wav_path)
            _eval_pending_paths[session_id] = wav_path
            _eval_refresh_manifest()
            new_count = _eval_count_in_bucket(bucket)
            print(f"[eval] Сохранено [{EVAL_BUCKETS[bucket]['label']}] {new_count}/{limit}: {fname}")
        except Exception as e:
            print(f"[eval] save error: {e}")

def schedule_eval_sample_collect(audio: np.ndarray, dur: float, session_id: int) -> None:
    """Фоновое сохранение записи — не блокирует распознавание."""
    if not EVAL_SAMPLES_ENABLED or dur < 0.5:
        return
    audio_copy = np.array(audio, dtype=np.float32, copy=True)
    threading.Thread(
        target=_eval_collect_worker,
        args=(audio_copy, dur, session_id),
        daemon=True,
    ).start()

def finalize_eval_sample_meta(
    session_id: int, dur: float, raw_text: str, final_text: str
) -> None:
    with _eval_lock:
        wav_path = _eval_pending_paths.pop(session_id, None)
    if not wav_path:
        return
    meta_path = wav_path.rsplit(".", 1)[0] + ".meta.json"
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "duration_sec": round(dur, 2),
                    "bucket": _eval_bucket_for_duration(dur),
                    "raw_whisper": raw_text or "",
                    "final_text": final_text or "",
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(f"[eval] meta error: {e}")

def acquire_single_instance_lock() -> bool:
    """Гарантирует один активный процесс WhisperKey на машине."""
    global _instance_lock_handle
    try:
        lock_path = "/tmp/whisperkey.lock"
        _instance_lock_handle = open(lock_path, "w")
        fcntl.flock(_instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _instance_lock_handle.write(str(os.getpid()))
        _instance_lock_handle.flush()
        return True
    except OSError:
        return False

def audio_callback(indata, frames, time_info, status):
    """Постоянный колбэк: пишет в буфер, если включена запись."""
    if is_recording:
        recording_data.append(indata.copy())

def start_audio_stream():
    """CEO Method: Включение микрофона только на время записи."""
    global audio_stream
    try:
        if audio_stream:
            audio_stream.stop()
            audio_stream.close()
        audio_stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            latency='low', blocksize=512, callback=audio_callback
        )
        audio_stream.start()
    except Exception as e:
        print(f"[audio start error] {e}")

def stop_audio_stream():
    """CEO Method: Безопасное отключение микрофона без блокировки основного потока."""
    global audio_stream
    try:
        if audio_stream:
            stream_to_close = audio_stream
            audio_stream = None
            def _close():
                try:
                    stream_to_close.stop()
                    stream_to_close.close()
                except: pass
            threading.Thread(target=_close, daemon=True).start()
    except Exception as e:
        print(f"[audio stop error] {e}")

def background_cloud_probe():
    """CEO Method: Фоновая проверка доступности облака с подтверждением стабильности."""
    global cloud_status
    if cloud_status["check_in_progress"]: return
    
    def probe():
        cloud_status["check_in_progress"] = True
        try:
            headers = {
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            }
            response = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=5)
            if response.status_code == 200:
                # CEO Fix: Требуем 2 успешных проверки подряд для выхода из блока, если были "прыжки"
                cloud_status["consecutive_success"] += 1
                if cloud_status["consecutive_success"] >= 1: # Можно поднять до 2 если будет дергаться
                    if cloud_status["is_blocked"]:
                        print("[radar] Связь стабильна. Возвращаю Cloud Turbo.")
                    cloud_status["is_blocked"] = False
            else:
                cloud_status["is_blocked"] = True
                cloud_status["consecutive_success"] = 0
        except:
            cloud_status["is_blocked"] = True
            cloud_status["consecutive_success"] = 0
        finally:
            cloud_status["last_check_time"] = time.time()
            cloud_status["check_in_progress"] = False

    threading.Thread(target=probe, daemon=True).start()

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """CEO Method: Асинхронное уведомление через отдельный поток для стабильности."""
    def run_notify():
        try:
            safe_title = title.replace('"', '\\"')
            safe_message = message.replace('"', '\\"')
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True)
            print(f"[notify] {title}: {message}") 
        except Exception as e:
            print(f"[notify error] {e}")
            
    threading.Thread(target=run_notify, daemon=True).start()

def smart_grammar_fix(text: str) -> str:
    """CEO Quality: Исправление типичных ошибок и окончаний."""
    if not text: return text
    text = re.sub(r"(\w+)ться", r"\1ться", text)
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    text = re.sub(r'([,.!?])\1+', r'\1', text)
    text = re.sub(r'([,.!?])(?=[^\s])', r'\1 ', text)
    text = re.sub(r'Claude[а-яА-Я]+', 'Claude', text)
    return text.strip()

# Союзы/предлоги в конце — фраза не завершена, точку не ставим (#4).
_INCOMPLETE_ENDING_RE = re.compile(
    r'\b(?:и|а|но|или|либо|чтобы|что|как|если|когда|где|куда|откуда|'
    r'который|которая|которое|которые|которых|которому|которой|'
    r'при|для|на|в|во|с|со|у|о|об|от|до|без|через|про|над|под|'
    r'перед|после|между|среди|по|к|ко|из)\s*$',
    re.IGNORECASE,
)

def apply_smart_sentence_ending(text: str) -> str:
    """Умное оформление конца: заглавная буква, точка только если мысль завершена."""
    if not text or len(text) <= 1:
        return text
    if text[0].islower():
        text = text[0].upper() + text[1:]
    stripped = text.rstrip()
    if not stripped:
        return text
    if stripped[-1] in '.!?…':
        return stripped
    tail_check = stripped.rstrip('.,;:')
    if _INCOMPLETE_ENDING_RE.search(tail_check):
        return stripped
    return stripped + '.'

def _restore_clipboard_async(old_clipboard: bytes) -> None:
    """Возврат буфера обмена в фоне — не блокирует завершение вставки."""
    def run_restore():
        try:
            time.sleep(0.5)
            process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
            process.communicate(input=old_clipboard)
        except Exception as e:
            print(f"[insert] clipboard restore error: {e}")

    threading.Thread(target=run_restore, daemon=True).start()

def direct_insert(text: str):
    """CEO Method: Вставка через буфер с максимальной совместимостью."""
    try:
        # 1. Сохраняем старый буфер
        old_clipboard = subprocess.run(['pbpaste'], capture_output=True).stdout

        # Подготавливаем текст заранее
        text_bytes = text.encode('utf-8')

        inserted = False
        for attempt in range(1, 4):
            # Копируем текст в буфер
            subprocess.run(['pbcopy'], input=text_bytes, check=True)
            time.sleep(0.1) # Даем macOS время обновить буфер
            
            # Попытка А: AppleScript через key code 9 (v) - самый надежный метод на Mac
            script = 'tell application "System Events" to key code 9 using command down'
            result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True)
            
            if result.returncode == 0:
                inserted = True
                break
                
            # Попытка Б: pynput native
            try:
                kb.press(KeyboardKey.cmd)
                kb.press('v')
                kb.release('v')
                kb.release(KeyboardKey.cmd)
                inserted = True
                break
            except:
                pass
            
            time.sleep(0.2)

        if not inserted:
            # Попытка В: Прямой ввод текста (медленно, но работает без Cmd+V)
            try:
                kb.type(text)
                inserted = True
                print("[insert] Fallback to typing")
            except:
                pass

        if inserted:
            print(f"[insert success] '{text[:30]}...'")
            if RESTORE_CLIPBOARD:
                _restore_clipboard_async(old_clipboard)
        else:
            print("[insert fail] Check Accessibility permissions for Terminal/Cursor")
            
    except Exception as e:
        print(f"[insert error] {e}")

def strip_asr_artifacts(text: str) -> str:
    """MED: удаляет типичные ASR-хвосты без агрессивной очистки основного текста."""
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    # Deloop: режем только если narrator-паттерн повторяется 2+ раза.
    loop_matches = list(re.finditer(NARRATOR_LOOP_PATTERN, cleaned, flags=re.IGNORECASE))
    if len(loop_matches) >= 2:
        cut_pos = loop_matches[0].start()
        print(f"[boh] deloop cut at {cut_pos} ({len(loop_matches)} matches)")
        cleaned = cleaned[:cut_pos].strip()

    if not cleaned:
        return cleaned

    lower = cleaned.lower()
    tail_start = int(len(lower) * 0.7)  # Проверяем только хвост, чтобы не трогать середину фразы.
    for marker in BOH_TAIL_MARKERS:
        idx = lower.find(marker, tail_start)
        if idx != -1:
            print(f"[boh] tail marker trimmed: '{marker}'")
            cleaned = cleaned[:idx].strip()
            lower = cleaned.lower()
            tail_start = int(len(lower) * 0.7)

    return cleaned.strip()

def should_skip_llm(raw_text: str) -> bool:
    """Пропуск Llama на коротком чистом raw — быстрее без потери на типичных фразах."""
    words = raw_text.split()
    if len(words) > LLAMA_SKIP_MAX_WORDS:
        return False
    lower = raw_text.lower()
    if any(t in lower for t in HALLUCINATION_TRIGGERS):
        return False
    if any(m in lower for m in BOH_TAIL_MARKERS):
        return False
    if len(re.findall(NARRATOR_LOOP_PATTERN, raw_text, flags=re.IGNORECASE)) >= 2:
        return False
    return True

def clean_noise(text: str) -> str:
    """Удаляет галлюцинации и применяет бизнес-словарь."""
    if not text: return ""
    text = strip_asr_artifacts(text)
    if not text:
        return ""
    
    # CEO Fix: Удаляем только ОДИНОЧНЫЕ слова-заглушки на полной тишине.
    # Если эти слова часть предложения - они НЕ удаляются.
    hallucination_words = ["КОНЕЦ", "Конец", "Конец связи", "Cursor", "Python", "CEO to CEO"]
    if text.strip() in hallucination_words:
        return ""

    text = re.sub(r'[фФfFaA]{4,}', '', text).strip()
    text = re.sub(r'[.]{3,}', '...', text).strip()
    
    # CEO Fix: Удаляем технические "хвосты" только если они явно лишние в конце после точки
    bad_endings = [r"Cursor[.!?]*$", r"Python[.!?]*$", r"CEO to CEO[.!?]*$", r"Claude[.!?]*$"]
    for pattern in bad_endings:
        if re.search(r'[.!?]\s+' + pattern, text):
            text = re.sub(r'\s+' + pattern, '', text).strip()

    business_vocabulary = {
        r'\b[Cc]laude\b': 'Claude',
        r'\b[Cc]eo to [Cc]eo\b': 'CEO to CEO',
        r'\b[Cc][Ee][Oo]\b': 'CEO',
        r'\b[Сс]ело то [Сс]ело\b': 'CEO to CEO',
        r'\b[Сс]ео то [Сс]ео\b': 'CEO to CEO',
        r'\b[Сс]ео\b': 'CEO',
        r'\b[Дд]ипло\b': 'деплой',
        r'\b[Дд]епло\b': 'деплой',
        r'\b[Dd]eplo\b': 'deploy',
    }
    for pattern, replacement in business_vocabulary.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            
    return text.strip()

def compress_silence(audio_data, threshold=0.01, min_pause=1.5, keep_pause=0.5):
    """CEO Method: Сжатие длинных пауз до фиксированной длины (векторизовано)."""
    try:
        if len(audio_data) == 0: return audio_data
        
        # Анализируем энергию в окнах по 100мс
        window_size = int(SAMPLE_RATE * 0.1)
        n_windows = len(audio_data) // window_size
        if n_windows == 0: return audio_data
        
        # Векторизованный поиск тишины
        windows = audio_data[:n_windows*window_size].reshape(-1, window_size)
        is_silent = np.max(np.abs(windows), axis=1) < threshold
        
        # Находим границы пауз
        silent_diff = np.diff(is_silent.astype(int))
        starts = np.where(silent_diff == 1)[0] + 1
        ends = np.where(silent_diff == -1)[0] + 1
        
        if is_silent[0]: starts = np.insert(starts, 0, 0)
        if is_silent[-1]: ends = np.append(ends, n_windows)
        
        # Считаем длительность пауз в окнах
        min_pause_windows = int(min_pause / 0.1)
        keep_pause_samples = int(keep_pause * SAMPLE_RATE)
        
        output_chunks = []
        last_idx = 0
        
        for s, e in zip(starts, ends):
            if (e - s) > min_pause_windows:
                # Добавляем звук до паузы
                output_chunks.append(audio_data[last_idx * window_size : s * window_size])
                # Добавляем сжатую тишину
                output_chunks.append(np.zeros(keep_pause_samples, dtype=np.float32))
                last_idx = e
        
        # Добавляем остаток
        output_chunks.append(audio_data[last_idx * window_size:])
        
        return np.concatenate(output_chunks) if output_chunks else audio_data
    except Exception as e:
        print(f"[compress error] {e}")
        return audio_data

def create_audio_wav(audio_data):
    """Создание WAV в памяти с защитным интервалом тишины и сжатием пауз."""
    try:
        # CEO Fix: Сжимаем длинные паузы перед отправкой, чтобы избежать галлюцинаций
        audio_data = compress_silence(audio_data)
        
        # CEO Fix: Добавляем 0.5 секунды тишины в конец, чтобы Whisper не обрезал последние слова
        silence_padding = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        audio_data = np.append(audio_data, silence_padding)
        
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            max_v = np.max(np.abs(audio_data))
            if max_v > 0:
                audio_data = audio_data / max_v
            wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())
        
        if SAVE_DEBUG_AUDIO:
            try:
                with open("debug_audio.wav", "wb") as f:
                    f.write(wav_io.getvalue())
            except Exception:
                pass

        return wav_io.getvalue()
    except Exception as e:
        print(f"[wav error] {e}")
        return None

def transcribe_cloud_turbo(audio_data):
    """Stage 1: Расшифровка через Groq (Whisper Large v3 Turbo) с мгновенным переключением."""
    global cloud_status
    
    if cloud_status["is_blocked"]:
        if time.time() - cloud_status["last_check_time"] > 60:
            background_cloud_probe()
        return None

    if not GROQ_API_KEY: return None

    # CEO Quality: Нормализация для идеального распознавания
    max_val = np.max(np.abs(audio_data))
    if max_val > 0.0001: 
        audio_data = audio_data / max_val * 0.98

    wav_data = create_audio_wav(audio_data)
    if not wav_data: return None

    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }

    files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
    data = {
        'model': CLOUD_WHISPER_MODEL,
        'language': 'ru',
        'prompt': ASR_CONTEXT_PROMPT,
        'temperature': 0.0,
        'response_format': 'verbose_json' # CEO Fix: Запрашиваем подробные данные для фильтрации
    }

    try:
        # CEO Speed Fix: Используем глобальную сессию и WAV (быстрее MP3)
        response = http_session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=15 
        )
        
        if response.status_code == 200:
            result = response.json()
            # CEO Fix: Фильтруем сегменты с низкой уверенностью (галлюцинации)
            segments = result.get('segments', [])
            valid_text = []
            has_sentence_ending = False
            for seg in segments:
                seg_text = seg.get('text', '').strip()
                if not seg_text:
                    continue
                no_speech_prob = seg.get('no_speech_prob', 0.0)

                # A) Короткий мусор на тишине.
                if no_speech_prob > 0.6 and len(seg_text) < 15:
                    print(f"[filter] skip short silent segment (p={no_speech_prob:.2f})")
                    continue

                # B) Подозрительный хвост после завершенной мысли.
                if has_sentence_ending and no_speech_prob > 0.65:
                    print(f"[filter] skip tail segment (p={no_speech_prob:.2f})")
                    continue

                valid_text.append(seg_text)
                if re.search(r'[.!?…]\s*$', seg_text):
                    has_sentence_ending = True
            
            return " ".join(valid_text) if valid_text else result.get('text', '')
        
        if response.status_code == 403:
            print("[!] Groq 403 (Geo-block). Switching to Instant Offline.")
            cloud_status["is_blocked"] = True
            cloud_status["last_check_time"] = time.time()
            background_cloud_probe() # Запускаем радар
            return None

        print(f"[cloud error] Status: {response.status_code}")
    except Exception as e:
        print(f"[cloud exception] {type(e).__name__}")
    
    return None

def refine_text_llm(raw_text):
    """Stage 2: Лингвистическая полировка через Llama-3.1-70B (Роль: Стенографист)."""
    if not raw_text or len(raw_text) < 5: return raw_text
    
    # CEO Fix: Берем короткий шлейф контекста для идеальных падежей
    context_tail = last_text_context[-40:] if last_text_context else ""
    
    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {
                "role": "system", 
                "content": (
                    "Ты - эксперт-лингвист. Твоя цель: превратить сырой ASR-текст в безупречный.\n"
                    "ПРАВИЛА:\n"
                    "1. Исправляй фонетику (провайдеры, может, Экхарт Толле).\n"
                    "2. СТРОГИЙ ЗАПРЕТ на выдумку фамилий и ассоциаций (если в ASR только 'Игорь', не пиши фамилию).\n"
                    "3. НЕ добавляй контекст (не меняй 'вендинг' на 'бизнес').\n"
                    "4. Восстанавливай падежи, окончания и пунктуацию, сохраняя авторский порядок слов.\n"
                    "5. Если фраза обрывается - не дописывай.\n"
                    "Выдай ТОЛЬКО чистый исправленный текст."
                )
            },
            {"role": "user", "content": f"Контекст: ...{context_tail}\nСырой текст ASR: {raw_text}"}
        ],
        "temperature": 0.0
    }
    
    try:
        response = http_session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=10
        )
        if response.status_code == 200:
            refined = response.json()['choices'][0]['message']['content'].strip()
            
            # CEO Integrity Guard: Считаем количество слов.
            # Если LLM удалила больше 1 слова или 10% слов - это брак.
            words_raw = len(raw_text.split())
            words_refined = len(refined.split())
            
            if words_refined >= words_raw - 1 and words_refined >= words_raw * 0.9:
                return refined.strip('"')
            else:
                print(f"[warn] LLM word count guard failed ({words_refined} vs {words_raw}). Using raw text.")
    except: pass
    return raw_text

def _transcribe_local(chunk: np.ndarray) -> str:
    """Локальный fallback — только когда cloud не дал пригодный текст."""
    max_val = np.max(np.abs(chunk))
    if max_val > 0.0001:
        chunk = chunk / max_val * 0.99
    segments, _ = model.transcribe(
        chunk, language="ru",
        beam_size=5, patience=1.0, repetition_penalty=1.2,
        vad_filter=False, suppress_blank=True, without_timestamps=True,
        condition_on_previous_text=False, initial_prompt=ASR_CONTEXT_PROMPT,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()

def _transcribe_one_chunk(chunk: np.ndarray, chunk_idx: int, n_chunks: int) -> tuple[int, str | None]:
    """Cloud (+ BoH при галлюцинации) → local только при пустом/ошибке cloud."""
    if n_chunks > 1:
        print(f"[diamond] Сегмент {chunk_idx + 1}/{n_chunks} ({len(chunk) / SAMPLE_RATE:.1f}s)")

    chunk_text = None
    chunk_dur = len(chunk) / SAMPLE_RATE
    use_cloud = CLOUD_ENABLED and not cloud_status.get("is_blocked")

    if use_cloud:
        raw_text = transcribe_cloud_turbo(chunk)
        if raw_text:
            lower = raw_text.lower()
            if any(t in lower for t in HALLUCINATION_TRIGGERS):
                cleaned = strip_asr_artifacts(raw_text)
                if cleaned and (len(cleaned.split()) >= 3 or chunk_dur <= 5.0):
                    print(f"[fast] Сегмент {chunk_idx + 1}: BoH вместо local fallback")
                    chunk_text = cleaned
                else:
                    print(f"[guard] Сегмент {chunk_idx + 1}: cloud пуст после BoH → local")
            elif len(raw_text.split()) < 3 and chunk_dur > 5.0:
                print(f"[guard] Сегмент {chunk_idx + 1}: cloud слишком короткий → local")
            else:
                chunk_text = raw_text

    if not chunk_text:
        print(f"[local] Сегмент {chunk_idx + 1}: offline decode")
        chunk_text = _transcribe_local(chunk) or None

    return chunk_idx, chunk_text

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, cloud_status, session_phase
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        schedule_eval_sample_collect(audio, dur, session_id)

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        # CEO Diamond: Умное дробление длинных записей (Chunking)
        # Если запись длиннее 30 секунд, режем её на куски по 20 секунд по паузам
        audio_chunks = []
        if dur > 30.0:
            print(f"[diamond] Длинная запись ({dur:.1f}s). Включаю умное дробление...")
            current_pos = 0
            chunk_size_samples = int(SAMPLE_RATE * 20) # CEO Fix: Снижаем до 20 сек для стабильности
            
            while current_pos < len(audio):
                end_pos = min(current_pos + chunk_size_samples, len(audio))
                
                # Если это не последний кусок, ищем паузу для красивого разреза
                if end_pos < len(audio):
                    # Ищем тишину в окне +/- 3 секунды от точки разреза
                    search_window = int(SAMPLE_RATE * 3)
                    search_start = max(end_pos - search_window, current_pos + int(SAMPLE_RATE * 5))
                    search_end = min(end_pos + search_window, len(audio) - int(SAMPLE_RATE * 1))
                    
                    sub_audio = audio[search_start:search_end]
                    if len(sub_audio) > SAMPLE_RATE:
                        # Анализируем энергию в окнах по 100мс
                        win = int(SAMPLE_RATE * 0.1)
                        energies = [np.max(np.abs(sub_audio[j:j+win])) for j in range(0, len(sub_audio)-win, win)]
                        if energies:
                            min_energy_idx = np.argmin(energies)
                            end_pos = search_start + (min_energy_idx * win) + (win // 2)
                
                audio_chunks.append(audio[current_pos:end_pos])
                current_pos = end_pos
        else:
            audio_chunks = [audio]

        n_chunks = len(audio_chunks)
        t_asr_start = time.time()
        ordered_parts: list[str | None] = [None] * n_chunks

        parallel_ok = (
            PARALLEL_CLOUD_CHUNKS
            and n_chunks > 1
            and CLOUD_ENABLED
            and not cloud_status.get("is_blocked")
        )

        if parallel_ok:
            print(f"[fast] Параллельный cloud: {n_chunks} сегментов, workers={MAX_CLOUD_WORKERS}")
            with ThreadPoolExecutor(max_workers=MAX_CLOUD_WORKERS) as pool:
                futures = [
                    pool.submit(_transcribe_one_chunk, chunk, idx, n_chunks)
                    for idx, chunk in enumerate(audio_chunks)
                ]
                for fut in as_completed(futures):
                    idx, chunk_text = fut.result()
                    if chunk_text:
                        ordered_parts[idx] = chunk_text
        else:
            for idx, chunk in enumerate(audio_chunks):
                _, chunk_text = _transcribe_one_chunk(chunk, idx, n_chunks)
                if chunk_text:
                    ordered_parts[idx] = chunk_text

        final_segments = [p for p in ordered_parts if p]
        t_asr_done = time.time()

        full_raw_text = " ".join(final_segments).strip()
        if not full_raw_text:
            finalize_eval_sample_meta(session_id, dur, "", "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            return

        print(f"[raw whisper] '{full_raw_text}'")

        t_llm_start = time.time()
        if should_skip_llm(full_raw_text):
            print(f"[fast] Llama skip ({len(full_raw_text.split())} слов, чистый raw)")
            text = full_raw_text
        else:
            print("[mode] Neural Refinement (Llama-3.1-70B)")
            text = refine_text_llm(full_raw_text)
        t_llm_done = time.time()

        elapsed = time.time() - t_start
        asr_sec = t_asr_done - t_asr_start
        llm_sec = t_llm_done - t_llm_start
        print(
            f"[time] total={elapsed:.1f}s | asr={asr_sec:.1f}s | llama={llm_sec:.1f}s "
            f"({elapsed / dur * 100:.0f}% от длины записи)"
        )
        
        text = clean_noise(text)
        text = smart_grammar_fix(text)

        if text and len(text) > 1:
            text = apply_smart_sentence_ending(text)

            # CEO Fix: Всегда выводим финальный результат в консоль для ручного копирования
            print(f"\n--- ФИНАЛЬНЫЙ ТЕКСТ ---\n{text}\n-----------------------\n")
            
            # CEO Fix: Возвращаем контекст (40 символов) для идеальных окончаний
            last_text_context = text[-40:]
            direct_insert(text + " ")
            finalize_eval_sample_meta(session_id, dur, full_raw_text, text)
            notify("WhisperKey ✓", "Текст готов")
        else:
            finalize_eval_sample_meta(session_id, dur, full_raw_text, "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
    except Exception as e:
        print(f"[error] {e}")
    finally:
        processing = False
        with state_lock:
            if active_session_id == session_id:
                session_phase = "idle"

# ─── Обработка клавиш ─────────────────────────────────────────────────────────

def is_trigger(key):
    if key == keyboard.Key.alt_r: return True
    try:
        if hasattr(key, 'vk') and key.vk == 61: return True
    except: pass
    return False

def on_press(key):
    global is_recording, recording_data, processing, trigger_held, last_trigger_ts, session_counter, active_session_id, session_phase
    now = time.time()
    if is_trigger(key) and not trigger_held:
        if now - last_trigger_ts < TRIGGER_DEBOUNCE_SEC:
            return
        with state_lock:
            if session_phase != "idle":
                return
            last_trigger_ts = now
            trigger_held = True
            session_counter += 1
            active_session_id = session_counter
            session_phase = "recording"
            notify("WhisperKey", "🎙 Запись...")
            try:
                start_audio_stream() # CEO Fix: Включаем микрофон
                is_recording = True
                recording_data = []
                print("[rec] Начата (микрофон включен)")
                
                # CEO Speed: Пре-ворминг соединения с Groq во время записи
                if USE_CLOUD:
                    def warm_groq():
                        try: http_session.options("https://api.groq.com/openai/v1/audio/transcriptions", timeout=1.0)
                        except: pass
                    threading.Thread(target=warm_groq, daemon=True).start()
                    
            except Exception as e:
                print(f"[audio error] {e}")
                is_recording = False
                session_phase = "idle"

def on_release(key):
    global is_recording, processing, trigger_held, session_counter, session_phase
    if is_trigger(key):
        trigger_held = False
    if is_trigger(key) and is_recording:
        with state_lock:
            if session_phase != "recording":
                return
            current_session_id = active_session_id
            session_phase = "processing"
        # CEO Fix: Задержка для захвата хвоста
        def delayed_stop():
            time.sleep(TAIL_CAPTURE_SECONDS)
            global is_recording
            is_recording = False 
            stop_audio_stream() # CEO Fix: Выключаем микрофон
            
            # После полной остановки и захвата хвоста - запускаем обработку
            audio_snapshot = list(recording_data)
            if len(audio_snapshot) < 10: # CEO Fix: Чуть увеличили порог для стабильности
                print("[skip] Слишком коротко")
                notify("WhisperKey", "⚠️ Слишком короткая запись")
                global processing
                processing = False
                with state_lock:
                    if active_session_id == current_session_id:
                        session_phase = "idle"
                return
            
            notify("WhisperKey", "⏹ Распознаю...")
            print(f"[rec] Остановлена (хвост захвачен)")
            threading.Thread(target=process_audio, args=(audio_snapshot, current_session_id), daemon=True).start()

        processing = True
        threading.Thread(target=delayed_stop, daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def check_macos_accessibility():
    """Проверка прав универсального доступа на macOS."""
    if sys.platform != "darwin":
        return True
    
    script = 'tell application "System Events" to set isProcessTrusted to UI elements enabled'
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
        if "false" in result.stdout.lower():
            print("\n" + "!"*60)
            print(" ВНИМАНИЕ: Права Универсального доступа (Accessibility) не выданы!")
            print(" Без них автоматическая вставка текста работать НЕ БУДЕТ.")
            print(" Выдайте права вашему Терминалу/IDE в Системных настройках.")
            print("!"*60 + "\n")
            return False
    except:
        pass
    return True

def main():
    global model
    if not acquire_single_instance_lock():
        print("[FATAL] WhisperKey уже запущен. Закрой предыдущий процесс перед новым стартом.")
        return

    check_macos_accessibility()

    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        if hasattr(p, 'cpu_affinity'): p.cpu_affinity([0, 1])
    except: pass

    print(f"WhisperKey v23.5 Auto-Eval | {CLOUD_WHISPER_MODEL} | Ready.")
    try:
        print("Загрузка локальной модели (может занять время при первом запуске)...")
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=False)
        print("Разогрев локальной модели...")
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)
        
        if USE_CLOUD:
            print("Разогрев облачного соединения...")
            def warm_network():
                try: http_session.head("https://api.groq.com", timeout=2.0)
                except: pass
            threading.Thread(target=warm_network, daemon=True).start()
            
        print("Система готова.")
        init_eval_samples_library()
    except Exception as e:
        print(f"[FATAL] {e}")
        return

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    notify("WhisperKey", "Готов к работе!")
    
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
