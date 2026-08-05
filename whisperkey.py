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
import difflib
from collections import deque
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

# Промпт задаёт модели ТОЛЬКО образец пунктуации — ни темы, ни терминов, ни имён
# собственных. Тематическая подсказка опасна: она протекает в текст там, где речь
# не разобрана (замер: "Субтитры делал DimaTorzok" — 3 слова вместо 74 живых).
#
# Замер 05.08.26 на 10 окнах реального разговора (2 независимые выборки по 5):
#   промпт                    слов    точек   запятых
#   пусто                      996        5        11   (текст сплошным потоком)
#   тематический (прежний)     975       97       139
#   этот                      1033      105       159
# То есть образец пунктуации без содержания даёт И больше текста (+5.9% к прежнему,
# +3.7% к пустому), И больше знаков препинания. Галлюцинаций — 0 на всех 10 окнах.
# ОБНОВЛЕНО 06.08.26 — промпт убран совсем, и вот почему.
# Прежний замер делался на 45-секундных окнах разговора и туда переносить его
# было нельзя. На КОРОТКОЙ диктовке промпт не помогает, а глушит речь:
# 8-секундные клипы, 6 окон — 80 слов с промптом против 112 без него (−29%),
# причём на одном клипе вся фраза схлопнулась в «Да.» — слово из самого промпта.
# Проверка по длительности показала не зависимость, а разброс:
#   8 с  −29%   15 с  +9%   25 с  −8%   40 с  +21%
# То есть промпт — лотерея, и на коротком входе проигрыш означает потерю всей
# фразы. Единственное, что он давал полезного, — заглавная буква в начале;
# она теперь ставится локально (CAPITALIZE_FIRST), без риска для слов.
ASR_CONTEXT_PROMPT = ""

# ─── Знаки препинания вторым проходом ─────────────────────────────────────────
# ЗАМЕР 06.08.26 на 101 реальной диктовке Егора (38 минут речи):
#   было (промпт в основном запросе)  3880 слов, 39.6 знака/100 слов, 8 простыней
#   без промпта                       4076 слов, 31.5 знака/100 слов, 23 простыни
#   слияние двух проходов             4076 слов, 39.6 знака/100 слов, 5 простыней
# «Простыня» — участок от 20 слов подряд без единого знака: полный, но нечитаемый
# текст. Без промпта такими приходила почти четверть записей, причём все длинные.
#
# Почему два запроса, а не подбор одного промпта: любой промпт — лотерея.
# Мета-инструкция («расставь знаки») срезала 160-секундную диктовку с 229 слов
# до 45, слова-заполнители теряли до трети на других записях. Промпт не может
# добавить знаков, не получив права подставлять свои слова.
# Почему не отдельная модель для пунктуации: llama-3.3 переписывала слова в
# половине кусков («вообще» → «generally», удаляла слова, «исправляла» грамматику),
# проверку сохранности проходило 69% при задержке 6.5 с.
# Решение: два запроса ОДНОВРЕМЕННО. Слова берутся только из прохода без промпта,
# знаки переносятся из прохода с промптом на совпавших участках. Слово из промпта
# в результат попасть не может — оно не совпадёт и будет отброшено.
STYLE_PROMPT = "Так, ну, в общем, смотри. Значит, вот. И, соответственно, дальше."

# Короткие диктовки whisper оформляет сам: на 47 записях до 15 секунд простыня
# была ровно одна. Второй запрос там — лишний расход квоты без выигрыша.
PUNCT_TRANSFER_MIN_SECONDS = 12.0

# Верхняя граница: второй проход идёт одним запросом на всю запись, а Groq
# принимает не больше 25 МБ. 16 кГц/16 бит — это 32 КБ на секунду, то есть
# около 780 секунд на предел. Берём с запасом.
PUNCT_TRANSFER_MAX_SECONDS = 600.0

NARRATOR_LOOP_PATTERN = r'(?:спикер|смикер|speaker)\s+говорит'

# Только водяные знаки Whisper из субтитровых корпусов. Обычные слова русского языка
# сюда попадать НЕ должны: "корректор" и "продолжение следует" съедали до 75% фразы
# ("Он корректор и дизайнер" -> "Он.").
BOH_TAIL_MARKERS = [
    "редактор субтитров",
    "субтитры сделал",
    "субтитры сделала",
    "субтитры подогнал",
    "субтитры делал",
    "субтитры создавал",
    "subtitles by",
    "thanks for watching",
    "dimatorzok",
    "ссылка на сайт в описании",
]
CLOUD_WHISPER_MODEL = "whisper-large-v3"
PARALLEL_CLOUD_CHUNKS = True
# Три потока, а не четыре, и пауза между запросами: на 30-минутной записи
# 32 куска в 4 потока без пауз словили 429 и потеряли 4 куска текста насовсем.
MAX_CLOUD_WORKERS = 3
MIN_REQUEST_INTERVAL = 0.7

# Дробление длинной записи — крайняя мера, а не норма.
#
# Замер 05.08.26 на реальной записи 29:45, сравнение с эталонной расшифровкой:
#   одним куском целиком        3810 слов, полнота 79.8%, точность 92.3%
#   нарезка 60 с + перекрытие   3559 слов, полнота 72.3%, точность 89.4%
# Groq режет длинный файл у себя внутри и делает это лучше, чем мы снаружи:
# наша нарезка добавляет швы, дубли на стыках и лишние запросы, упирающиеся
# в лимит. Поэтому порог поднят выше любой реальной диктовки — до 15 минут.
# Всё, что короче, уходит в облако одним запросом: максимум качества,
# ни одного шва. Нарезка остаётся только для записей, которые иначе
# не влезут в ограничение API по размеру файла.
CHUNK_THRESHOLD_SECONDS = 900.0
CHUNK_SIZE_SECONDS = 60.0
CHUNK_OVERLAP_SECONDS = 3.0

# Детектор провала распознавания. no_speech_prob его НЕ ловит (у пустых сегментов
# замерено 0.028 и 0.581), плотность слов ловит 3 из 3 и 4 из 4 в двух проверках.
DENSITY_GATE_MIN_DURATION = 8.0
DENSITY_GATE_MIN_WORDS_PER_SEC = 0.5

HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "голос за кадром",
]

# ─── Сбор корпуса реальных диктовок ───────────────────────────────────────────
# Все замеры качества сделаны на чужом материале (получасовой созвон, дальний
# микрофон). Корпуса собственной диктовки нет — без него пороги нарезки остаются
# экстраполяцией. Сбор идёт EVAL_COLLECT_DAYS дней от первого запуска, потом
# выключается сам и при старте показывает баннер.
EVAL_SAMPLES_ENABLED = True
EVAL_COLLECT_DAYS = 7
EVAL_SAMPLES_ROOT = os.path.expanduser("~/Desktop/WhisperKey-Eval-Samples")
EVAL_STATE_FILE = os.path.join(EVAL_SAMPLES_ROOT, "state.json")
EVAL_MIN_DURATION = 1.0          # короче — не показательно
EVAL_HARD_LIMIT = 400            # предохранитель от разрастания папки
EVAL_BUCKETS = {
    "eval_samples": {"limit": EVAL_HARD_LIMIT, "label": "Диктовки"},
}
eval_collection_finished = False  # выставляется при старте, читается баннером

# API Настройки (Groq Cloud)
# Путь абсолютный от самого файла: с относительным ключ не находился при запуске
# из другого каталога, и вся диктовка молча уходила на слабую локальную модель.
load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
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

# Предзапись. Поток микрофона держится открытым, и последние PREROLL_SECONDS
# всегда лежат в кольце — при нажатии клавиши они уходят в запись вместе с новым
# звуком. Это единственное настоящее лекарство от срезанного первого слова:
# PortAudio не отдаёт ни одного сэмпла, пока поднимается, а уведомление «говори»
# выдавалось ещё раньше. Буфер под это был объявлен в коде год назад и не использовался.
#
# Поток НЕ висит вечно: после PREROLL_IDLE_TIMEOUT секунд без диктовки он закрывается
# сам. Пока диктуешь сериями — предзапись работает; ушёл заниматься другим — микрофон
# отпущен и индикатор погас. Вечно открытый поток вреден не нагрузкой (она ничтожна:
# 32 раза в секунду по 2 КБ), а тем, что умирает при смене аудиоустройства —
# подключил наушники, и запись уходит в мёртвый поток.
# PREROLL_ENABLED = False вернёт прежнее поведение: поток только на время записи.
#
# ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА. На реальном железе постоянно открытый поток словил
# PaMacCore (AUHAL) err=-10863 kAudioUnitErr_CannotDoInCurrentContext: CoreAudio
# отказался работать, поток умер, предзапись обнулилась ("предзапись 0 мс"),
# и следующая диктовка ушла в никуда с сообщением «слишком короткая запись».
# Выигрыш предзаписи при этом НЕ ИЗМЕРЕН — величину срезания первого слова
# проверить не удалось. Недоказанная оптимизация, ломающая основную функцию,
# в рабочей версии не держится. Включай True, только если готов проверять.
PREROLL_ENABLED = False
PREROLL_SECONDS = 0.5
PREROLL_IDLE_TIMEOUT = 90.0
_PREROLL_BLOCKS = max(1, int(PREROLL_SECONDS * SAMPLE_RATE / 512))
preroll_buffer: deque = deque(maxlen=_PREROLL_BLOCKS)
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
    "consecutive_success": 0,  # CEO Fix: Счетчик стабильных запросов
    "last_degenerate": [],     # окна, где модель сорвалась (гейт плотности)
}
kb = KeyboardController()
_instance_lock_handle = None
_eval_lock = threading.Lock()
_eval_pending_paths: dict[int, str] = {}
_eval_full_notified: set[str] = set()


def _eval_bucket_for_duration(dur: float) -> str:
    # Собираем ВСЕ диктовки, а не только длинные: короткие фразы — основной режим,
    # и именно на них ломались текстовые фильтры.
    if dur >= EVAL_MIN_DURATION:
        return "eval_samples"
    return "ignored"

def _eval_load_state() -> dict:
    try:
        with open(EVAL_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _eval_save_state(state: dict) -> None:
    try:
        os.makedirs(EVAL_SAMPLES_ROOT, exist_ok=True)
        with open(EVAL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[eval] state error: {e}")

def _eval_check_deadline() -> tuple[bool, int, int]:
    """Решает, идёт ли сбор. Возвращает (сбор_активен, прошло_дней, собрано_записей).

    Срок отсчитывается от ПЕРВОГО запуска со сбором, а не от установки: иначе
    неделя истекала бы, пока программа лежит без дела.
    """
    global EVAL_SAMPLES_ENABLED, eval_collection_finished
    if not EVAL_SAMPLES_ENABLED:
        return False, 0, 0

    state = _eval_load_state()
    now = time.time()
    started = state.get("started_at")
    if not started:
        started = now
        state["started_at"] = started
        state["started_human"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _eval_save_state(state)

    days_passed = int((now - started) // 86400)
    collected = _eval_count_in_bucket("eval_samples")
    deadline_hit = (now - started) >= EVAL_COLLECT_DAYS * 86400
    limit_hit = collected >= EVAL_HARD_LIMIT

    if deadline_hit or limit_hit:
        EVAL_SAMPLES_ENABLED = False
        eval_collection_finished = True
        if not state.get("finished"):
            state["finished"] = True
            state["finished_human"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["collected"] = collected
            state["reason"] = "срок" if deadline_hit else "лимит записей"
            _eval_save_state(state)
            notify("WhisperKey — сбор завершён",
                   f"Собрано {collected} записей. Материал готов к разбору.")
        return False, days_passed, collected

    return True, days_passed, collected

def _eval_print_banner(active: bool, days_passed: int, collected: int) -> None:
    """Крупная плашка в терминале — её видно при каждом запуске."""
    if eval_collection_finished or not active:
        if collected <= 0:
            return
        print()
        print("=" * 70)
        print("   ███  С Б О Р   З А В Е Р Ш Ё Н  ███")
        print()
        print(f"   Собрано записей: {collected}")
        print(f"   Папка: {EVAL_SAMPLES_ROOT}")
        print()
        print("   МОЖНО ИДТИ ДАЛЬШЕ — материал готов к разбору.")
        print("   Скажи Клоду: «разбери корпус диктовок».")
        print("=" * 70)
        print()
        return

    left = max(0, EVAL_COLLECT_DAYS - days_passed)
    print(f"[eval] Сбор корпуса идёт: {collected} записей, осталось дней: {left}")

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
    
    # Останов по сроку и по жёсткому лимиту живёт в _eval_check_deadline(),
    # здесь только снимок состояния.
    manifest_path = os.path.join(EVAL_SAMPLES_ROOT, "manifest.json")
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[eval] manifest error: {e}")
    return manifest

def init_eval_samples_library() -> None:
    """Готовит папку сбора и печатает статус. Решение «идёт или закончен» — в _eval_check_deadline."""
    try:
        os.makedirs(EVAL_SAMPLES_ROOT, exist_ok=True)
        for bucket in EVAL_BUCKETS:
            os.makedirs(os.path.join(EVAL_SAMPLES_ROOT, bucket), exist_ok=True)
        readme = os.path.join(EVAL_SAMPLES_ROOT, "README.txt")
        if not os.path.exists(readme):
            lines = [
                "WhisperKey — корпус реальных диктовок",
                "",
                f"Сбор идёт {EVAL_COLLECT_DAYS} дней от первого запуска, затем выключается сам.",
                "Каждая диктовка сохраняется как .wav + .meta.json рядом с ним.",
                "В .meta.json лежит сырой ответ Whisper и финальный вставленный текст —",
                "по ним видно, что именно испортила обработка.",
                "",
                "Зачем: пороги нарезки и фильтров сейчас подобраны на чужом материале.",
                "На своём корпусе их можно проверить и подвинуть по факту.",
                "",
                "Статус сбора: state.json и manifest.json в этой папке.",
                "Когда сбор закончится, программа скажет об этом при запуске.",
            ]
            with open(readme, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

        active, days_passed, collected = _eval_check_deadline()
        _eval_refresh_manifest()
        _eval_print_banner(active, days_passed, collected)
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
    """Пишет в запись, а между записями — в кольцо предзаписи.

    Флаг status раньше игнорировался: input_overflow от PortAudio означает
    сэмплы, выброшенные аппаратным буфером, и это было полностью невидимо.
    """
    if status:
        print(f"[audio] PortAudio: {status}")
    block = indata.copy()
    if is_recording:
        recording_data.append(block)
    elif PREROLL_ENABLED:
        preroll_buffer.append(block)

def start_audio_stream(raise_on_error: bool = False):
    """Открывает поток захвата.

    Раньше исключение глоталось здесь же, поэтому is_recording вставал в True
    при мёртвом потоке, в лог шло заведомо ложное «микрофон включен», а отказ
    микрофона доходил до пользователя как «Слишком короткая запись».
    """
    global audio_stream
    if audio_stream:
        try:
            audio_stream.stop()
            audio_stream.close()
        except Exception:
            pass
        audio_stream = None

    # CoreAudio иногда отвечает -10863 (cannot do in current context), когда
    # система в этот момент перестраивает аудио — например, только что
    # подключились наушники. Через долю секунды та же операция проходит,
    # поэтому пара повторов дешевле, чем сорванная диктовка.
    last_error = None
    for attempt in range(3):
        try:
            audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                latency='low', blocksize=512, callback=audio_callback
            )
            audio_stream.start()
            return True
        except Exception as e:
            last_error = e
            audio_stream = None
            if attempt < 2:
                print(f"[audio] Микрофон не открылся ({type(e).__name__}), повтор {attempt + 2} из 3")
                time.sleep(0.25 * (attempt + 1))

    print(f"[audio start error] {last_error}")
    notify("WhisperKey — микрофон недоступен", str(last_error)[:120])
    if raise_on_error:
        raise last_error
    return False

_idle_timer: threading.Timer | None = None
_idle_timer_lock = threading.Lock()

def ensure_audio_stream() -> bool:
    """Гарантирует живой поток захвата.

    Поток умирает при смене аудиоустройства (подключил наушники — и всё), причём
    молча: объект остаётся, а сэмплы не идут. Поэтому проверяем не только наличие
    объекта, но и его активность.
    """
    global audio_stream
    stream = audio_stream
    if stream is not None:
        try:
            if stream.active:
                return True
        except Exception:
            pass
        print("[audio] Поток микрофона умер (возможно, сменилось устройство) — переоткрываю")
        stop_audio_stream()
        preroll_buffer.clear()
    return start_audio_stream()

def _close_idle_stream():
    """Отпускает микрофон после простоя — чтобы индикатор не горел без дела."""
    global audio_stream
    if is_recording:
        return
    with state_lock:
        if session_phase != "idle":
            return
    if audio_stream is not None:
        print(f"[audio] Микрофон освобождён после {PREROLL_IDLE_TIMEOUT:.0f}с простоя")
        stop_audio_stream()
        preroll_buffer.clear()

def schedule_idle_close():
    """Заводит таймер освобождения микрофона, сбрасывая предыдущий."""
    global _idle_timer
    if not PREROLL_ENABLED or PREROLL_IDLE_TIMEOUT <= 0:
        return
    with _idle_timer_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(PREROLL_IDLE_TIMEOUT, _close_idle_stream)
        _idle_timer.daemon = True
        _idle_timer.start()

def cancel_idle_close():
    global _idle_timer
    with _idle_timer_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None

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
    """Косметика пробелов и ничего больше.

    Убрано отсюда сознательно:
      - вставка пробела после .!? — ломала числа, версии и имена файлов
        ("версия 3.5" -> "версия 3. 5", "main.py" -> "main. py");
      - re.sub(r"(\\w+)ться", r"\\1ться") — шаблон тождественен замене, мёртвый код;
      - схлопывание Claude[а-яА-Я]+ — правило из словаря замен, переписывало речь.
    Whisper сам расставляет пробелы после знаков корректно; чинить нечего.
    """
    if not text:
        return text
    text = re.sub(r'\s+([,.!?;:])', r'\1', text)   # пробел ПЕРЕД знаком — всегда лишний
    text = re.sub(r'[ \t]{2,}', ' ', text)          # двойные пробелы
    return text.strip()

# Союзы/предлоги в конце — фраза не завершена, точку не ставим (#4).
_INCOMPLETE_ENDING_RE = re.compile(
    r'\b(?:и|а|но|или|либо|чтобы|что|как|если|когда|где|куда|откуда|'
    r'который|которая|которое|которые|которых|которому|которой|'
    r'при|для|на|в|во|с|со|у|о|об|от|до|без|через|про|над|под|'
    r'перед|после|между|среди|по|к|ко|из)\s*$',
    re.IGNORECASE,
)

# Два РАЗНЫХ решения, которые раньше жили под одним флагом.
#
# Заглавная первая буква — включена. Без промпта модель отдаёт текст со строчной
# (замер: 1 из 6 клипов с заглавной против 6 из 6 с промптом), и это единственное,
# что промпт давал полезного. Поднять первую букву локально безопасно: слова
# не меняются, порядок не трогается, риска потерять фразу нет.
CAPITALIZE_FIRST = True

# Точка в конце — выключена. Диктовка часто идёт в середину предложения
# ("и добавь туда" -> "И добавь туда." ломает мысль), а сам whisper-large-v3
# ставит точку там, где она уместна.
FORCE_TRAILING_DOT = False

_TRANSFER_PUNCT = '.,!?;:—…'

def _transfer_norm(w: str) -> str:
    return re.sub(r'[^\w]', '', w, flags=re.UNICODE).lower()

def transfer_punctuation(base_text: str, styled_text: str) -> str:
    """Слова из base_text, знаки препинания — из styled_text.

    base_text  — проход без промпта: полный список слов, но часто без знаков.
    styled_text — проход с STYLE_PROMPT: знаки есть, но часть слов подменена
                  словами из промпта.
    Совпадающие участки находятся difflib по нормализованным словам; с каждого
    совпавшего слова переносится только «хвост» из знаков и, если оно начинало
    предложение, заглавная буква. Несовпавшие участки остаются как в base_text.
    Инвариант проверяется утверждением: список слов на выходе равен входному.
    """
    if not base_text or not styled_text:
        return base_text

    a, b = base_text.split(), styled_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out = list(a)

    raised = set()
    for i, j, n in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_matching_blocks():
        for k in range(n):
            src, dst = b[j + k], out[i + k]
            if not src or not dst:
                continue
            tail = ''.join(c for c in src[len(src.rstrip(_TRANSFER_PUNCT)):]
                           if c in _TRANSFER_PUNCT)
            if tail and dst[-1] not in _TRANSFER_PUNCT:
                out[i + k] = dst + tail
            # Заглавную берём, только если слово реально начинало предложение
            # в оформленной версии, иначе она приезжает из середины чужой фразы.
            prev_styled = b[j + k - 1] if (j + k) else ''
            if (src[0].isupper() and dst[0].islower()
                    and ((j + k) == 0 or (prev_styled and prev_styled[-1] in '.!?…'))):
                out[i + k] = out[i + k][0].upper() + out[i + k][1:]
                raised.add(i + k)

    # Согласуем регистр с итоговой расстановкой знаков: заглавная стоит после
    # точки и в начале. Перенесённая заглавная, под которой точки не оказалось
    # (в оформленной версии предложение начиналось, а здесь — нет), снимается.
    for i, t in enumerate(out):
        if not t:
            continue
        prev = out[i - 1] if i else ''
        starts = (i == 0) or (prev and prev[-1] in '.!?…')
        if starts and t[0].islower():
            out[i] = t[0].upper() + t[1:]
        elif not starts and i in raised:
            out[i] = t[0].lower() + t[1:]

    result = ' '.join(out)
    if [_transfer_norm(x) for x in result.split()] != na:
        print("[punct] перенос знаков нарушил слова — оставляю текст без знаков")
        return base_text
    return result

def apply_smart_sentence_ending(text: str) -> str:
    """Минимальное оформление: заглавная в начале, точка — по флагу."""
    if not text:
        return text
    text = text.rstrip()
    if len(text) <= 1:
        return text

    if CAPITALIZE_FIRST and text[0].islower():
        text = text[0].upper() + text[1:]

    if not FORCE_TRAILING_DOT:
        return text

    if text[-1] in '.!?…':
        return text
    if _INCOMPLETE_ENDING_RE.search(text.rstrip('.,;:')):
        return text
    return text + '.'

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
            # Буфер СОЗНАТЕЛЬНО не восстанавливаем: текст остаётся в нём, и его
            # можно вставить руками. Раньше через 0.5 с буфер затирался прежним
            # содержимым, и результат диктовки пропадал совсем.
            print("[insert fail] Текст остался в буфере обмена — нажми Cmd+V")
            notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Cmd+V")

    except Exception as e:
        print(f"[insert error] {e}")

artifacts_removed: list[str] = []   # что вырезали в последней обработке — для уведомления

def strip_asr_artifacts(text: str) -> str:
    """Удаляет водяные знаки Whisper, НЕ трогая окружающий текст.

    Два принципиальных отличия от прежней версии:
      1. Вырезается только сам маркер, а не "маркер и всё после него". Раньше
         cleaned[:idx] выбрасывал продолжение фразы — "Он корректор и дизайнер"
         превращалось в "Он".
      2. Ищем по всему тексту, а не в последних 20 символах: в реальных выдачах
         маркеры стоят и в середине (замер по transcribe-bot — все 4 подстановки
         были в середине текста).
    Маркер удаляется, только когда он занимает отдельную фразу — то есть слева
    начало текста или конец предложения, справа конец текста или знак препинания.
    """
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    # Зацикленный рассказчик: режем, только если паттерн повторился 2+ раза.
    loop_matches = list(re.finditer(NARRATOR_LOOP_PATTERN, cleaned, flags=re.IGNORECASE))
    if len(loop_matches) >= 2:
        cut_pos = loop_matches[0].start()
        artifacts_removed.append(f"зацикливание ({len(loop_matches)} повторов)")
        print(f"[boh] deloop cut at {cut_pos} ({len(loop_matches)} matches)")
        cleaned = cleaned[:cut_pos].strip()

    if not cleaned:
        return cleaned

    # Водяной знак занимает предложение целиком и обычно тащит за собой хвост
    # ("Субтитры сделал DimaTorzok", "Редактор субтитров А.Семкин"). Удаляем от
    # начала маркера до конца его предложения — но только если хвост короткий:
    # это защита от сноса живого абзаца, если маркер вдруг совпал со словами речи.
    MAX_TAIL_WORDS = 6
    for marker in BOH_TAIL_MARKERS:
        pattern = re.compile(
            r'(?:(?<=^)|(?<=[.!?…]))(\s*' + re.escape(marker) + r'([^.!?…]*))',
            flags=re.IGNORECASE,
        )
        while True:
            match = pattern.search(cleaned)
            if not match:
                break
            tail_words = match.group(2).split()
            if len(tail_words) > MAX_TAIL_WORDS:
                break  # слишком длинный хвост — это, скорее всего, живая речь
            cleaned = (cleaned[:match.start(1)] + ' ' + cleaned[match.end(1):]).strip()
            cleaned = re.sub(r'^\s*[.!?…]+\s*', '', cleaned)
            artifacts_removed.append(marker)
            print(f"[boh] водяной знак удалён: '{marker}'")

    return re.sub(r'\s{2,}', ' ', cleaned).strip()

# Нормализация написания названий продуктов — единственная разрешённая замена слов.
# Слово не меняется на другое слово: приводится лишь регистр/латиница уже
# распознанного названия. Ставь False, если не нужно даже это.
NORMALIZE_BRAND_NAMES = True
BRAND_NAMES = {
    r'\bclaude\b': 'Claude',
    r'\bcursor\b': 'Cursor',
    r'\bgroq\b': 'Groq',
    r'\btelegram\b': 'Telegram',
    r'\bwhisper\b': 'Whisper',
    r'\bgithub\b': 'GitHub',
    r'\bapi\b': 'API',
}

def clean_noise(text: str) -> str:
    """Снимает водяные знаки ASR. Слова пользователя не трогает.

    Что убрано отсюда и почему:
      - hallucination_words: одиночные "Python"/"Cursor"/"Конец" обнулялись целиком,
        то есть односложный ответ терялся на 100% и приходило "Речь не распознана";
      - bad_endings: срезали настоящее последнее слово фразы;
      - business_vocabulary: переписывал живую речь ("сео" -> "CEO", "депло" -> "деплой")
        без ведома говорящего;
      - [фФfFaA]{4,}: смесь кириллицы и латиницы, ломала имена файлов
        ("Открой файл AAAA.txt" -> "Открой файл. txt").
    """
    if not text:
        return ""
    text = strip_asr_artifacts(text)
    if not text:
        return ""

    text = re.sub(r'[.]{4,}', '...', text).strip()   # только явно избыточные точки

    if NORMALIZE_BRAND_NAMES:
        for pattern, replacement in BRAND_NAMES.items():
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
    """Упаковка звука в WAV без какой-либо обработки.

    Всё, что здесь было раньше, снято по замерам:
      - compress_silence: на диктовке результат побайтово тот же (совпадение 1.0000),
        на длинной записи цепочка стоила 15.6% слов;
      - паддинг 0.5 с тишины: приписывание чистой цифровой тишины к идентичному
        аудио сдвигает границы 30-секундных окон модели и отнимает 8-10% слов;
      - нормировка по пику: пользы не показала, а вместе с паддингом двигала таймлайн.
    Остался только клип в ±1.0, чтобы не переполнить int16.
    """
    try:
        audio_data = np.clip(np.asarray(audio_data, dtype=np.float32), -1.0, 1.0)

        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
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

def _find_degenerate_segments(segments: list) -> list:
    """Сегменты, где модель сорвалась: длинные и почти без слов.

    no_speech_prob этот отказ не видит — у замеренных пустых сегментов он был
    0.028 и 0.581, то есть «речь точно есть». Плотность слов ловит его надёжно:
    3 из 3 и 4 из 4 в двух независимых проверках.
    """
    bad = []
    for seg in segments:
        try:
            dur = float(seg.get('end', 0.0)) - float(seg.get('start', 0.0))
        except (TypeError, ValueError):
            continue
        if dur < DENSITY_GATE_MIN_DURATION:
            continue
        n_words = len(seg.get('text', '').split())
        if dur > 0 and (n_words / dur) < DENSITY_GATE_MIN_WORDS_PER_SEC:
            bad.append({'start': float(seg.get('start', 0.0)),
                        'end': float(seg.get('end', 0.0)),
                        'words': n_words, 'dur': dur})
    return bad

def _words_in_range(words: list, start: float, end: float) -> str:
    """Слова из words[], попадающие в интервал времени."""
    picked = []
    for w in words:
        try:
            w_start = float(w.get('start', -1))
        except (TypeError, ValueError):
            continue
        if start <= w_start < end:
            token = (w.get('word') or '').strip()
            if token:
                picked.append(token)
    return ' '.join(picked)

def _text_from_response(result: dict) -> tuple[str, list]:
    """Собирает текст: пунктуация из segments[], потерянное — из words[].

    Почему не одно из двух:
      - segments[] дают читаемый текст со знаками препинания, но на длинном входе
        модель отдаёт пустые и вырожденные окна ("95 95 95") — до 19.5% выхода
        теряется прямо внутри успешного ответа API;
      - words[] содержат ВСЁ распознанное (слов, которые есть в сегментах и нет
        в словах, во всех замерах было ровно 0), но приходят без пунктуации.
    Поэтому основа — сегменты, а words[] подставляются только туда, где сегмент
    сорвался. Так сохраняется и читаемость, и полнота.
    """
    words = result.get('words') or []
    segments = result.get('segments') or []
    degenerate = _find_degenerate_segments(segments)
    degenerate_spans = {(d['start'], d['end']) for d in degenerate}

    if not segments:
        if words:
            return ' '.join((w.get('word') or '').strip()
                            for w in words if (w.get('word') or '').strip()), degenerate
        return (result.get('text') or '').strip(), degenerate

    pieces = []
    prev_end = 0.0
    for seg in segments:
        try:
            s_start = float(seg.get('start', 0.0))
            s_end = float(seg.get('end', 0.0))
        except (TypeError, ValueError):
            s_start = s_end = 0.0
        seg_text = (seg.get('text') or '').strip()

        # Слова, оставшиеся между сегментами — модель их распознала, но в текст
        # они не попали.
        if words and s_start > prev_end + 0.5:
            gap = _words_in_range(words, prev_end, s_start)
            if gap:
                pieces.append(gap)

        if (s_start, s_end) in degenerate_spans or not seg_text:
            recovered = _words_in_range(words, s_start, s_end) if words else ''
            if recovered:
                print(f"[recover] окно {s_start:.1f}-{s_end:.1f}с: "
                      f"восстановлено {len(recovered.split())} слов из words[]")
                pieces.append(recovered)
            elif seg_text:
                pieces.append(seg_text)
        elif seg_text:
            pieces.append(seg_text)

        prev_end = max(prev_end, s_end)

    # Хвост после последнего сегмента
    if words:
        tail = _words_in_range(words, prev_end, float('inf'))
        if tail:
            pieces.append(tail)

    text = ' '.join(p for p in pieces if p).strip()
    return (text or (result.get('text') or '').strip()), degenerate

_rate_lock = threading.Lock()
_last_request_ts = [0.0]

def _throttle():
    """Разносит запросы во времени.

    Замер на 30-минутной записи: 32 куска в 4 потока без пауз словили 429 и
    потеряли 4 куска текста насовсем. Лимит ключа — 7200 секунд аудио в час,
    но упирается всё в частоту запросов, а не в объём.
    """
    with _rate_lock:
        delta = time.time() - _last_request_ts[0]
        if delta < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - delta)
        _last_request_ts[0] = time.time()

def transcribe_cloud_turbo(audio_data, allow_retry: bool = True, use_prompt: bool = True,
                           return_words: bool = False, prompt_override: str = ""):
    """Расшифровка через Groq whisper-large-v3.

    Возвращает строку текста либо None. При return_words=True — кортеж
    (текст, список слов с таймкодами): слова нужны для склейки перекрытий
    по времени, текстовое сравнение на стыке ненадёжно, потому что модель
    распознаёт один и тот же участок в разных кусках немного по-разному.
    Сорванные окна отдаёт через cloud_status['last_degenerate'].
    """
    global cloud_status

    empty = (None, []) if return_words else None

    if cloud_status["is_blocked"]:
        if time.time() - cloud_status["last_check_time"] > 60:
            background_cloud_probe()
        return empty

    if not GROQ_API_KEY:
        return empty

    wav_data = create_audio_wav(audio_data)
    if not wav_data:
        return empty

    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }

    files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
    data = [
        ('model', CLOUD_WHISPER_MODEL),
        ('language', 'ru'),
        ('temperature', '0.0'),
        ('response_format', 'verbose_json'),
        # Обе гранулярности обязательны: при запросе только 'word' Groq отдаёт
        # segments: null, и детектор плотности остаётся слепым.
        ('timestamp_granularities[]', 'segment'),
        ('timestamp_granularities[]', 'word'),
    ]
    # prompt_override — для прохода за знаками препинания (STYLE_PROMPT).
    # Его результат идёт только в transfer_punctuation и словами не распоряжается.
    prompt = prompt_override or (ASR_CONTEXT_PROMPT if use_prompt else "")
    if prompt:
        data.append(('prompt', prompt))

    for attempt in range(4):
        try:
            _throttle()
            response = http_session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers, files=files, data=data, timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                text, degenerate = _text_from_response(result)
                cloud_status["last_degenerate"] = degenerate
                if degenerate:
                    total = sum(d['dur'] for d in degenerate)
                    print(f"[gate] сорванных окон: {len(degenerate)}, суммарно {total:.1f}с")
                if return_words:
                    return text, (result.get('words') or [])
                return text

            if response.status_code == 403:
                print("[!] Groq 403 (гео-блок). Ухожу на локальную модель.")
                cloud_status["is_blocked"] = True
                cloud_status["last_check_time"] = time.time()
                background_cloud_probe()
                return empty

            # 429 и 5xx лечатся ожиданием. Раньше любой такой ответ давал
            # молчаливую дыру; на 30-минутной записи так терялось 4 куска из 32.
            if response.status_code in (429, 500, 502, 503, 504) and allow_retry and attempt < 3:
                wait = float(response.headers.get('retry-after', 0) or 0) or (2 ** attempt) * 2
                wait = min(wait, 20.0)
                print(f"[cloud] {response.status_code}, повтор через {wait:.1f}с "
                      f"(попытка {attempt + 2} из 4)")
                time.sleep(wait)
                files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
                continue

            print(f"[cloud error] Статус: {response.status_code}")
            return empty

        except Exception as e:
            print(f"[cloud exception] {type(e).__name__}")
            if allow_retry and attempt < 3:
                time.sleep(2 ** attempt)
                files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
                continue
            return empty

    return empty

def _transcribe_local(chunk: np.ndarray) -> str:
    """Локальный fallback — только когда cloud не дал пригодный текст."""
    max_val = np.max(np.abs(chunk))
    if max_val > 0.0001:
        chunk = chunk / max_val * 0.99
    segments, _ = model.transcribe(
        chunk, language="ru",
        beam_size=5, patience=1.0, repetition_penalty=1.2,
        vad_filter=False, suppress_blank=True, without_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=ASR_CONTEXT_PROMPT or None,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()

def _transcribe_one_chunk(chunk: np.ndarray, chunk_idx: int, n_chunks: int) -> tuple[int, str | None, str, list]:
    """Распознаёт один кусок. Возвращает (индекс, текст|None, метка_качества, слова).

    Метка качества: 'cloud' | 'cloud_retry' | 'local' | 'lost'. Она нужна выше,
    чтобы пользователь увидел, что часть записи ушла на слабую локальную модель
    или потерялась — раньше и то и другое проходило молча.
    Слова с таймкодами нужны для склейки перекрытий по времени.
    """
    if n_chunks > 1:
        print(f"[chunk] Сегмент {chunk_idx + 1}/{n_chunks} ({len(chunk) / SAMPLE_RATE:.1f}s)")

    chunk_text = None
    chunk_words: list = []
    quality = 'lost'
    chunk_dur = len(chunk) / SAMPLE_RATE
    use_cloud = CLOUD_ENABLED and not cloud_status.get("is_blocked")

    if use_cloud:
        raw_text, chunk_words = transcribe_cloud_turbo(chunk, return_words=True)
        degenerate = list(cloud_status.get("last_degenerate") or [])
        if raw_text:
            lower = raw_text.lower()
            if any(t in lower for t in HALLUCINATION_TRIGGERS):
                cleaned = strip_asr_artifacts(raw_text)
                if cleaned and (len(cleaned.split()) >= 3 or chunk_dur <= 5.0):
                    chunk_text, quality = cleaned, 'cloud'
                else:
                    print(f"[guard] Сегмент {chunk_idx + 1}: пусто после чистки")
            else:
                chunk_text, quality = raw_text, 'cloud'

        # Переспрос без промпта — только если срыв НЕ удалось залечить словами.
        # Если words[] уже вернули речь для сорванного окна, второй запрос лишний:
        # он стоит квоты, а на 30-минутной записи именно перерасход запросов
        # приводил к 429 и потере целых кусков.
        if degenerate and ASR_CONTEXT_PROMPT and _needs_retry(degenerate, chunk_words):
            print(f"[gate] Сегмент {chunk_idx + 1}: переспрашиваю без промпта")
            retry_text, retry_words = transcribe_cloud_turbo(chunk, use_prompt=False, return_words=True)
            if retry_text and len(retry_text.split()) > len((chunk_text or '').split()):
                chunk_text, quality, chunk_words = retry_text, 'cloud_retry', retry_words

    if not chunk_text:
        print(f"[local] Сегмент {chunk_idx + 1}: локальная модель (качество ниже)")
        try:
            chunk_text = _transcribe_local(chunk) or None
            quality = 'local' if chunk_text else 'lost'
            chunk_words = []
        except Exception as e:
            print(f"[local error] Сегмент {chunk_idx + 1}: {type(e).__name__} {e}")
            chunk_text, quality = None, 'lost'

    return chunk_idx, chunk_text, quality, chunk_words

def _needs_retry(degenerate: list, words: list) -> bool:
    """Стоит ли переспрашивать окно, которое модель провалила.

    Не стоит, если words[] уже дали для этого интервала осмысленную плотность
    речи — текст спасён, повторный запрос ничего не добавит, а квоту потратит.
    """
    for d in degenerate:
        span = d['end'] - d['start']
        if span <= 0:
            continue
        recovered = sum(1 for w in words
                        if d['start'] <= float(w.get('start', -1)) < d['end'])
        if (recovered / span) < DENSITY_GATE_MIN_WORDS_PER_SEC:
            return True
    return False

# ─── Нарезка и склейка ────────────────────────────────────────────────────────

def _split_audio(audio: np.ndarray, dur: float) -> tuple[list, list]:
    """Режет запись на куски с перекрытием. Возвращает (куски, смещения_в_секундах).

    Короткие записи не режутся вовсе — это главная защита от галлюцинаций:
    каждый разрез даёт модели ещё один «конец файла», где она склонна дописывать
    субтитровые водяные знаки.
    """
    if dur <= CHUNK_THRESHOLD_SECONDS:
        return [audio], [0.0]

    size = int(SAMPLE_RATE * CHUNK_SIZE_SECONDS)
    overlap = int(SAMPLE_RATE * CHUNK_OVERLAP_SECONDS)
    step = max(size - overlap, size // 2)

    chunks, offsets = [], []
    pos = 0
    while pos < len(audio):
        end = min(pos + size, len(audio))
        chunks.append(audio[pos:end])
        offsets.append(pos / SAMPLE_RATE)
        if end >= len(audio):
            break
        pos += step

    print(f"[chunk] Длинная запись {dur:.1f}s → {len(chunks)} кусков "
          f"по {CHUNK_SIZE_SECONDS:.0f}s с перекрытием {CHUNK_OVERLAP_SECONDS:.0f}s")
    return chunks, offsets

def _norm_word(w: str) -> str:
    return re.sub(r'[^\w]', '', w.lower().replace('ё', 'е'))

def _drop_overlap(prev_text: str, next_text: str, max_probe: int = 25) -> str:
    """Убирает из next_text повтор, попавший в него из зоны перекрытия.

    Ищется ТОЧНОЕ совпадение хвоста предыдущего куска с головой следующего,
    длиной от трёх слов. Нечёткое сравнение сознательно не используется: на
    замере оно один раз выбросило 12 слов настоящей речи, оставив бессмыслицу.
    Не нашли точного совпадения — ничего не трогаем: лишний дубль пережить можно,
    потерянные слова — нет.
    """
    prev_words = prev_text.split()
    next_words = next_text.split()
    if len(prev_words) < 3 or len(next_words) < 3:
        return next_text

    limit = min(max_probe, len(prev_words), len(next_words))
    for n in range(limit, 2, -1):
        tail = [_norm_word(w) for w in prev_words[-n:]]
        head = [_norm_word(w) for w in next_words[:n]]
        if tail == head and any(tail):
            return ' '.join(next_words[n:])
    return next_text

def _fmt_mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"

def _overlap_word_count(words: list, chunk_start: float, cut_before: float) -> int:
    """Сколько первых слов куска попало в зону перекрытия с предыдущим.

    Считаем по таймкодам, а режем потом от ТЕКСТА — иначе, пересобирая фразу
    из words[], мы теряем пунктуацию, которая есть только в segments[].
    Сравнение по времени надёжнее текстового: один и тот же участок в соседних
    кусках модель распознаёт немного по-разному, и точного совпадения на стыке
    может не быть вовсе.
    """
    n = 0
    for w in words:
        try:
            abs_start = chunk_start + float(w.get('start', 0.0))
        except (TypeError, ValueError):
            break
        if abs_start >= cut_before:
            break
        if (w.get('word') or '').strip():
            n += 1
    return n

def _join_chunks(parts: list, quality: list, offsets: list,
                 words_per_chunk: list | None = None) -> tuple[str, list]:
    """Склеивает куски, помечая потерянные. Возвращает (текст, список_меток_потерь).

    Кусок, который не распознался, раньше просто выпадал: дыра до 23 секунд
    склеивалась встык, и приходило бодрое «Текст готов». Теперь на его месте
    остаётся видимая метка — текст честнее, чем гладкий обман.

    Перекрытие снимается по таймкодам слов, если они есть, и точным текстовым
    совпадением в запасном варианте. Нечёткого сравнения здесь нет намеренно:
    на замере оно уничтожило 12 слов реальной речи, оставив бессмыслицу.
    """
    pieces: list[str] = []
    lost_marks: list[str] = []
    prev_end: float | None = None

    for idx, text in enumerate(parts):
        if not text:
            if len(parts) > 1:
                mark = f"[не распознано {_fmt_mmss(offsets[idx])}]"
                lost_marks.append(mark)
                pieces.append(mark)
            continue

        chunk_words = (words_per_chunk[idx] if words_per_chunk and idx < len(words_per_chunk)
                       else None)
        if pieces and prev_end is not None and chunk_words:
            n_dup = _overlap_word_count(chunk_words, offsets[idx], prev_end)
            words_of_text = text.split()
            # Режем от текста, а не от words[] — так остаётся пунктуация.
            # Если перекрытие вдруг накрыло весь кусок, оставляем текст как есть:
            # лишний дубль пережить можно, потерянные слова нет.
            if 0 < n_dup < len(words_of_text):
                text = ' '.join(words_of_text[n_dup:])
        elif pieces:
            text = _drop_overlap(pieces[-1], text)

        if chunk_words:
            try:
                prev_end = offsets[idx] + max(float(w.get('end', 0.0)) for w in chunk_words)
            except (TypeError, ValueError):
                prev_end = None

        if text:
            pieces.append(text)

    return ' '.join(p for p in pieces if p).strip(), lost_marks

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

        # Дробление длинной записи. Порог поднят с 30 до 90 секунд: на 25 и 60
        # секундах модель не теряет ничего (69=69 и 140=140 слов), а каждый лишний
        # разрез создаёт искусственный «конец записи» — место рождения субтитрового
        # спама. Куски по 60 с с перекрытием 3 с — лучшая из семи замеренных
        # конфигураций (полнота 84.0% и 85.3% в двух независимых проверках против
        # 73.0% у прежних 20 секунд без перекрытия).
        audio_chunks, chunk_offsets = _split_audio(audio, dur)

        n_chunks = len(audio_chunks)

        # Проход за знаками препинания стартует ОДНОВРЕМЕННО с основным, поэтому
        # ожидания не добавляет: оба запроса летят к Groq параллельно.
        style_pool = style_future = None
        if (PUNCT_TRANSFER_MIN_SECONDS <= dur <= PUNCT_TRANSFER_MAX_SECONDS
                and CLOUD_ENABLED and not cloud_status.get("is_blocked")):
            style_pool = ThreadPoolExecutor(max_workers=1)
            style_future = style_pool.submit(
                transcribe_cloud_turbo, audio, allow_retry=False,
                prompt_override=STYLE_PROMPT)

        t_asr_start = time.time()
        ordered_parts: list[str | None] = [None] * n_chunks
        chunk_quality: list[str] = ['lost'] * n_chunks
        chunk_words: list[list] = [[] for _ in range(n_chunks)]

        parallel_ok = (
            PARALLEL_CLOUD_CHUNKS
            and n_chunks > 1
            and CLOUD_ENABLED
            and not cloud_status.get("is_blocked")
        )

        if parallel_ok:
            print(f"[fast] Параллельно: {n_chunks} сегментов, потоков {MAX_CLOUD_WORKERS}")
            with ThreadPoolExecutor(max_workers=MAX_CLOUD_WORKERS) as pool:
                futures = {
                    pool.submit(_transcribe_one_chunk, chunk, idx, n_chunks): idx
                    for idx, chunk in enumerate(audio_chunks)
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    # Исключение в ОДНОМ куске раньше убивало всю запись целиком:
                    # fut.result() пробрасывал его наружу, общий except печатал строку
                    # в терминал, и пользователь не получал ни текста, ни уведомления.
                    try:
                        idx, chunk_text, quality, words = fut.result()
                    except Exception as e:
                        print(f"[error] Сегмент {idx + 1} упал: {type(e).__name__} {e}")
                        chunk_text, quality, words = None, 'lost', []
                    ordered_parts[idx] = chunk_text
                    chunk_quality[idx] = quality
                    chunk_words[idx] = words
        else:
            for idx, chunk in enumerate(audio_chunks):
                try:
                    _, chunk_text, quality, words = _transcribe_one_chunk(chunk, idx, n_chunks)
                except Exception as e:
                    print(f"[error] Сегмент {idx + 1} упал: {type(e).__name__} {e}")
                    chunk_text, quality, words = None, 'lost', []
                ordered_parts[idx] = chunk_text
                chunk_quality[idx] = quality
                chunk_words[idx] = words

        t_asr_done = time.time()

        # Склейка: перекрытие снимается по таймкодам слов, текстовое сравнение —
        # только запасной путь. Нечёткого сравнения нет намеренно.
        full_raw_text, lost_marks = _join_chunks(
            ordered_parts, chunk_quality, chunk_offsets, chunk_words)

        if not full_raw_text:
            if style_pool is not None:
                style_pool.shutdown(wait=False)
            finalize_eval_sample_meta(session_id, dur, "", "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            return

        print(f"[raw whisper] '{full_raw_text}'")

        # Знаки препинания из второго прохода. Он уже почти наверняка завершён —
        # шёл параллельно. Ждём не дольше 8 секунд: текст важнее оформления,
        # и молчаливо задерживать вставку из-за знаков нельзя.
        if style_future is not None:
            try:
                styled = style_future.result(timeout=8.0)
                if styled:
                    before = full_raw_text
                    full_raw_text = transfer_punctuation(full_raw_text, styled)
                    if full_raw_text != before:
                        print(f"[punct] знаки перенесены со второго прохода")
            except Exception as e:
                print(f"[punct] второй проход не дошёл ({type(e).__name__}) — "
                      f"текст остаётся без добавленных знаков")
            finally:
                style_pool.shutdown(wait=False)

        # LLM-полировка удалена. Модель llama-3.1-70b-versatile выведена Groq из
        # обслуживания (HTTP 400 model_decommissioned), и год вызов молча возвращал
        # сырой текст — это и было то качество, которое всех устраивало. Рабочая
        # llama-3.3 на замере переписывает 4.2-5.6% слов, а цель — дословность.
        text = full_raw_text

        elapsed = time.time() - t_start
        asr_sec = t_asr_done - t_asr_start
        print(
            f"[time] total={elapsed:.1f}s | asr={asr_sec:.1f}s "
            f"({elapsed / dur * 100:.0f}% от длины записи)"
        )
        
        artifacts_removed.clear()
        text = clean_noise(text)
        text = smart_grammar_fix(text)

        if text and len(text) > 1:
            text = apply_smart_sentence_ending(text)

            print(f"\n--- ФИНАЛЬНЫЙ ТЕКСТ ---\n{text}\n-----------------------\n")

            last_text_context = text[-40:]
            direct_insert(text + " ")
            finalize_eval_sample_meta(session_id, dur, full_raw_text, text)

            # Любая деградация обязана быть видимой. Раньше уход на локальную
            # модель и выпавший кусок одинаково рапортовались как «Текст готов».
            n_local = chunk_quality.count('local')
            n_retry = chunk_quality.count('cloud_retry')
            problems = []
            if lost_marks:
                problems.append(f"потеряно кусков: {len(lost_marks)}")
            if n_local:
                problems.append(f"локальная модель: {n_local}")
            if artifacts_removed:
                problems.append("вырезан водяной знак")
            if problems:
                notify("WhisperKey — распознано частично", "; ".join(problems))
                print(f"[warn] {'; '.join(problems)}")
            else:
                suffix = f" (переспрошено окон: {n_retry})" if n_retry else ""
                notify("WhisperKey ✓", "Текст готов" + suffix)
        else:
            finalize_eval_sample_meta(session_id, dur, full_raw_text, "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
    except Exception as e:
        # Этот except стоит после всех notify, поэтому раньше пользователь при
        # падении не получал НИЧЕГО — ни текста, ни уведомления, только строку
        # в терминале, куда он не смотрит.
        import traceback
        print(f"[error] {type(e).__name__}: {e}")
        traceback.print_exc()
        notify("WhisperKey — сбой", f"{type(e).__name__}. Текст не вставлен.")
    finally:
        processing = False
        with state_lock:
            if active_session_id == session_id:
                session_phase = "idle"
        # Диктовка закончена — заводим отсчёт до освобождения микрофона.
        # Новое нажатие таймер сбросит, так что при серии диктовок поток живёт.
        schedule_idle_close()

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
            try:
                if PREROLL_ENABLED:
                    cancel_idle_close()
                    # Поток мог закрыться по простою или умереть от смены устройства.
                    if not ensure_audio_stream():
                        session_phase = "idle"
                        return
                    # Порядок важен: буфер заполняется ДО поднятия is_recording,
                    # иначе колбэк успевает дописать в старый список.
                    recording_data = list(preroll_buffer)
                    preroll_buffer.clear()
                else:
                    if not start_audio_stream():
                        session_phase = "idle"
                        return
                    recording_data = []

                is_recording = True
                # Уведомление ПОСЛЕ фактического начала захвата: раньше «говори»
                # выдавалось до того, как микрофон отдавал первый сэмпл.
                notify("WhisperKey", "🎙 Запись...")
                if PREROLL_ENABLED:
                    pre = len(recording_data) * 512 / SAMPLE_RATE
                    # Нулевая предзапись при включённом режиме — признак того, что
                    # поток был мёртв. Молчать об этом нельзя: дальше запись уйдёт
                    # в никуда, а пользователь увидит «слишком короткая запись».
                    if pre <= 0:
                        print("[audio] ВНИМАНИЕ: предзапись пуста, поток был неисправен")
                    else:
                        print(f"[rec] Начата (предзапись {pre * 1000:.0f} мс)")
                else:
                    print("[rec] Начата")

                if USE_CLOUD:
                    def warm_groq():
                        try: http_session.options("https://api.groq.com/openai/v1/audio/transcriptions", timeout=1.0)
                        except: pass
                    threading.Thread(target=warm_groq, daemon=True).start()

            except Exception as e:
                print(f"[audio error] {e}")
                notify("WhisperKey — микрофон недоступен", str(e)[:120])
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
            # global обязателен для ВСЕХ трёх имён. У session_phase его не было:
            # объявление в on_release на вложенную функцию не распространяется,
            # поэтому "idle" ниже писалось в ЛОКАЛЬНУЮ переменную, а модульная
            # фаза навсегда оставалась "processing" — и on_press с этого момента
            # выходил по первой же проверке. Итог: одно случайное короткое
            # касание правого Option намертво выключало диктовку до перезапуска.
            global is_recording, processing, session_phase
            time.sleep(TAIL_CAPTURE_SECONDS)
            is_recording = False
            # При включённой предзаписи поток остаётся открытым — иначе следующее
            # нажатие снова начнётся с холодного старта и срежет первое слово.
            if not PREROLL_ENABLED:
                stop_audio_stream()

            audio_snapshot = list(recording_data)
            if len(audio_snapshot) < 10:
                print("[skip] Слишком коротко")
                notify("WhisperKey", "⚠️ Слишком короткая запись")
                processing = False
                with state_lock:
                    if active_session_id == current_session_id:
                        session_phase = "idle"
                schedule_idle_close()
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

def create_desktop_launcher():
    """CEO UX: Тихо создает ярлык на рабочем столе, если его еще нет."""
    try:
        desktop = os.path.expanduser("~/Desktop")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        if sys.platform == "darwin":
            launcher_name = "WhisperKey.command"
            target_path = os.path.join(desktop, launcher_name)
            source_path = os.path.join(current_dir, "Запустить WhisperKey.command")
            
            if not os.path.exists(target_path) and os.path.exists(source_path):
                import shutil
                shutil.copy2(source_path, target_path)
                os.chmod(target_path, 0o755)
                
        elif sys.platform == "win32":
            launcher_name = "WhisperKey.bat"
            target_path = os.path.join(desktop, launcher_name)
            source_path = os.path.join(current_dir, "run_whisperkey.bat")
            
            if not os.path.exists(target_path) and os.path.exists(source_path):
                import shutil
                shutil.copy2(source_path, target_path)
    except:
        pass

def main():
    global model
    if not acquire_single_instance_lock():
        print("[FATAL] WhisperKey уже запущен. Закрой предыдущий процесс перед новым стартом.")
        return

    print("\n" + "="*60)
    print(" 🎙️  WhisperKey v24 | Дословная диктовка")
    print(" Created by Егор Нищук (Telegram: @Seikatsuma)")
    print("="*60)

    check_macos_accessibility()
    create_desktop_launcher()

    # Сбор корпуса и его баннер — до тяжёлой загрузки модели, чтобы сообщение
    # «сбор завершён» не пряталось за минутой ожидания.
    init_eval_samples_library()

    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        # cpu_affinity сознательно не трогаем: прибивание к двум ядрам на
        # загруженной машине конкурирует с аудио-колбэком и провоцирует потерю
        # сэмплов. На macOS вызов всё равно не поддерживается.
    except Exception:
        pass

    print(f"Модель: {CLOUD_WHISPER_MODEL} | промпт: "
          f"{'выключен' if not ASR_CONTEXT_PROMPT else 'включён'}")
    try:
        print("Загрузка локальной модели (запасной вариант, если облако недоступно)...")
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=False)
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)

        if USE_CLOUD:
            def warm_network():
                try: http_session.head("https://api.groq.com", timeout=2.0)
                except: pass
            threading.Thread(target=warm_network, daemon=True).start()

        if PREROLL_ENABLED:
            # Поток открывается сразу, чтобы первая же диктовка шла с предзаписью,
            # но тут же заводится отсчёт: не воспользовались — микрофон отпущен.
            if start_audio_stream():
                print(f"Предзапись включена: {PREROLL_SECONDS * 1000:.0f} мс, "
                      f"микрофон освобождается после {PREROLL_IDLE_TIMEOUT:.0f}с простоя")
                schedule_idle_close()
            else:
                print("Предзапись недоступна — микрофон не открылся, работаю по старой схеме")
    except Exception as e:
        print(f"[FATAL] {e}")
        return

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    notify("WhisperKey", "Готов к работе!")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
