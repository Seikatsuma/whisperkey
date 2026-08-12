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
import warnings
from collections import deque

# Системный питон macOS собран с LibreSSL, и urllib3 при каждом старте печатает
# об этом предупреждение на три строки. На работу оно не влияет — запросы к Groq
# идут нормально, — но выводится ПЕРЕД приветствием и выглядит как ошибка.
# Глушим точечно: только это предупреждение, остальные видны как раньше.
warnings.filterwarnings("ignore", message=r".*OpenSSL 1\.1\.1\+.*")
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except Exception:
    pass
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

# Каскад распознавания (Deepgram/Groq/локальная модель, гейт плотности, перенос
# знаков/терминов/форм) вынесен в общий пакет speech_engine — тот же движок
# используют thoughts-bot, transcribe-bot, toki-bot, claude-tg-bot (устройство,
# профили, все замеры — speech_engine/agent.md). Здесь остаётся только
# платформенная часть: микрофон, клавиша, вставка, уведомления.
#
# Пакет лежит ПРЯМО В ЭТОЙ ПАПКЕ (speech_engine/, рядом с этим файлом), а не
# отдельным репозиторием — иначе самообновление ловило бы только whisperkey.py,
# а не движок, от которого он теперь зависит. Ровно это и случилось 11.08.26:
# первая версия переноса ссылалась на ~/speech-engine на сервере, `git pull`
# на Mac Егора обновил whisperkey.py, но не принёс пакет — падение при каждом
# запуске (ModuleNotFoundError, дважды — основной путь и обходной тоже мимо).
# С пакетом внутри репозитория обычный `import speech_engine` находит его сам:
# Python всегда ищет модули рядом со своим стартовым файлом.
import speech_engine


def load_env_file(path: str = ".env") -> None:
    """Минимальная загрузка .env без внешних зависимостей.

    Значение из файла НЕ перебивает уже заданную переменную окружения (стандартное
    поведение dotenv-загрузчиков — даёт приоритет явному экспорту в шелле). Побочный
    эффект: если ключ когда-то был экспортирован в `.zshrc`/`.bash_profile` (например,
    во время первой настройки) и с тех пор отличается от актуального в `.env`, правка
    файла молча ничего не меняет — программа продолжает пользоваться старым значением
    без единой строчки в логе. Диагностировано 11.08.26 на живом логе Егора: ключ
    Deepgram стабильно отклонялся (HTTP 401) через несколько дней после того, как
    ключ в `.env` должен был быть верным — эта тихая подмена была первым кандидатом
    в причины, и без предупреждения ниже её пришлось бы вычислять по обрывкам лога.
    """
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
                if not k:
                    continue
                if k not in os.environ:
                    os.environ[k] = v
                elif os.environ[k] != v:
                    print(f"[env warn] {k}: значение из .env проигнорировано — уже задано "
                          f"в окружении шелла (проверь ~/.zshrc, ~/.bash_profile на старый export {k})")
    except Exception as e:
        print(f"[env warn] {e}")

# ─── Настройки ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_PATH  = "small" # CEO Upgrade: 'base' -> 'small' for significantly better Russian accuracy
TAIL_CAPTURE_SECONDS = 1.0  # Захват хвоста после отпускания клавиши.
# Поднято с 0.6 до 1.0 (07.08.26) по жалобе: последнее слово не попадает
# в запись, хотя клавиша отпущена позже. Прежние 0.6 с ставились ради
# скорости, но диктовка идёт в облако 2-4 секунды, и лишние 0.4 с на фоне
# этого не заметны, а потерянное слово заметно всегда.
# Проверить это замером нельзя: корпус эталонов содержит УЖЕ записанный
# звук, и то, что не попало в файл, в нём отсутствует у всех одинаково.
# Проверка — на калибровочном тексте, где известно, что было сказано.
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
# Второй проход несёт две нагрузки сразу: образец пунктуации и словарь терминов.
# Совмещение проверено 07.08.26 на калибровочной записи (эталон — текст, который
# Егор читал вслух, 731 слово): точность 78.9% -> 80.4%, верно записанных терминов
# 6 -> 16. Отдельный третий запрос под словарь давал 80.2% — то есть совмещение
# не только дешевле, но и точнее.
#
# STYLE_PROMPT, TERM_CANON, ASR_TEMPO, PUNCT_TRANSFER_MIN/MAX_SECONDS,
# NARRATOR_LOOP_PATTERN, BOH_TAIL_MARKERS, CLOUD_WHISPER_MODEL, PARALLEL_CLOUD_CHUNKS,
# MAX_CLOUD_WORKERS, MIN_REQUEST_INTERVAL, CHUNK_*_SECONDS, DENSITY_GATE_*,
# HALLUCINATION_TRIGGERS — всё это переехало в speech_engine.DICTATION (Profile),
# см. ~/speech-engine/speech_engine/profiles.py. Числа и замеры не изменились,
# изменилось только место, где они лежат — общее для всех проектов вместо
# копии в одном файле. DICTATION ниже — короткий псевдоним для тех мест этого
# файла, которым нужно прочитать значение (стартовые печати, диагностика).
DICTATION = speech_engine.DICTATION
CLOUD_WHISPER_MODEL = DICTATION.groq_model
DEEPGRAM_MODEL = DICTATION.deepgram_model

# ─── Сбор корпуса реальных диктовок ───────────────────────────────────────────
# Все замеры качества сделаны на чужом материале (получасовой созвон, дальний
# микрофон). Корпуса собственной диктовки нет — без него пороги нарезки остаются
# экстраполяцией. Сбор идёт EVAL_COLLECT_DAYS дней от первого запуска, потом
# выключается сам и при старте показывает баннер.
EVAL_SAMPLES_ENABLED = True
EVAL_COLLECT_DAYS = 3   # 08.08.26: по просьбе Егора — корпус набирается быстрее
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

# ─── Deepgram — первая ступень каскада ────────────────────────────────────────
# Параметры (модель nova-2, DEEPGRAM_PARAMS, тайминги, порог блокировки) —
# в speech_engine.DICTATION (см. правку выше и agent.md пакета, там же цифры
# замера 08.08.26: 86.5% против 78.6% на калибровке, nova-2 против nova-3 и т.д.).
# Здесь остаётся только чтение ключа — он нужен локально, чтобы решить, включать
# ли ступень вообще, и для стартовой печати.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
DEEPGRAM_ENABLED = bool(DEEPGRAM_API_KEY)

# Создаем глобальную сессию для Keep-Alive. Общая и для локальных warm-up вызовов
# (ниже), и для каскада speech_engine — единственный пул соединений на процесс.
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

# Состояние облачных ступеней (было: модульные словари cloud_status/deepgram_status)
# теперь живёт внутри _engine.ctx (speech_engine.Context) — тот же смысл, тот же
# набор полей (is_blocked/last_degenerate/blocked_until/last_seconds/...), просто
# инкапсулировано в объект пакета вместо двух глобальных словарей файла.
# local_model=None здесь: модель грузится позже в main() (дорогая операция,
# должна идти после баннера сбора корпуса) — присваивается в _engine.ctx.local_model
# по готовности, см. main().
_engine = speech_engine.Engine(
    profile=DICTATION, groq_api_key=GROQ_API_KEY, deepgram_api_key=DEEPGRAM_API_KEY,
    sample_rate=SAMPLE_RATE, session=http_session,
)
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
    session_id: int, dur: float, raw_text: str, final_text: str,
    stages: dict | None = None
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
                    # Какая ступень каскада отработала: без этого нельзя отличить
                    # ошибку Deepgram от ошибки whisper, а лечатся они по-разному.
                    "engine": (stages or {}).get("engine", ""),
                    # Текст на каждом шаге — иначе при пропаже предложения нельзя
                    # понять, потеряла его модель, сборка или правки.
                    "api_text": (stages or {}).get("api_text", ""),
                    "assembled": (stages or {}).get("assembled", ""),
                    "raw_whisper": raw_text or "",
                    "final_text": final_text or "",
                    "degenerate": (stages or {}).get("degenerate", []),
                    "dropouts": (stages or {}).get("dropouts", 0),
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
        # Переполнение входного буфера — это выброшенные железом сэмплы, то есть
        # речь, которой в записи уже не будет. Считаем их во время диктовки,
        # чтобы такую потерю нельзя было спутать с ошибкой распознавания:
        # модель тут ни при чём, до неё звук просто не дошёл.
        if is_recording:
            audio_dropouts.append(str(status))
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

# background_cloud_probe (фоновая проверка доступности Groq) переехала в
# speech_engine.groq_engine — вызывается изнутри каскада автоматически, отдельно
# звать её отсюда больше не нужно.

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

# transfer_punctuation/transfer_terms/transfer_endings/fix_known_terms/TERM_FIX/
# TERM_CANON переехали в speech_engine (terms.py/transfer.py) — дословно, см.
# правку в начале файла. Локальных копий больше нет: раньше расхождение здесь
# и в transcribe-bot было ровно тем дублированием, которое убирает этот перенос.

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

CLIPBOARD_WAIT_SEC = 1.5      # сколько ждём, пока система реально обновит буфер
CLIPBOARD_RESTORE_SEC = 2.5   # через сколько возвращаем прежний буфер

def _clipboard_now() -> bytes:
    try:
        return subprocess.run(['pbpaste'], capture_output=True, timeout=2).stdout
    except Exception:
        return b''

def _put_clipboard(data: bytes) -> bool:
    """Кладёт в буфер и ЖДЁТ подтверждения, что там оказалось именно это.

    Прежний код клал и спал 0.1 с наугад. Если система не успевала, следующий
    Cmd+V вставлял ПРЕЖНЕЕ содержимое буфера — текст диктовки пропадал,
    а на экран приезжало то, что человек копировал до этого.
    """
    try:
        subprocess.run(['pbcopy'], input=data, check=True, timeout=5)
    except Exception as e:
        print(f"[insert] буфер не записался: {e}")
        return False
    deadline = time.time() + CLIPBOARD_WAIT_SEC
    while time.time() < deadline:
        if _clipboard_now() == data:
            return True
        time.sleep(0.02)
    print("[insert] буфер не подтвердился за отведённое время")
    return False

def _restore_clipboard_async(old_clipboard: bytes, ours: bytes) -> None:
    """Возвращает прежний буфер — но только когда это безопасно.

    Две защиты, которых не было:
      • пауза 2.5 с вместо 0.5 — медленные приложения (браузер, Slack, Notion)
        успевают забрать вставку. Раньше буфер возвращался раньше, чем
        приложение читало его, и вставлялся старый текст;
      • возврат только если в буфере всё ещё НАШ текст. Если человек за это
        время скопировал своё, его копия важнее — не трогаем.
    """
    def run_restore():
        try:
            time.sleep(CLIPBOARD_RESTORE_SEC)
            if _clipboard_now() != ours:
                return            # буфер уже чей-то — не наше дело
            subprocess.run(['pbcopy'], input=old_clipboard, timeout=5)
        except Exception as e:
            print(f"[insert] clipboard restore error: {e}")

    threading.Thread(target=run_restore, daemon=True).start()

def direct_insert(text: str):
    """Вставка через буфер обмена с подтверждением на каждом шаге.

    Порядок важен: сначала убеждаемся, что в буфере лежит наш текст, и только
    потом жмём Cmd+V. Если подтверждения нет — НЕ вставляем вовсе: вставка
    в этот момент означала бы, что человеку в документ приедет его старый
    буфер вместо продиктованного.
    """
    try:
        old_clipboard = _clipboard_now()
        text_bytes = text.encode('utf-8')

        inserted = False
        for attempt in range(1, 4):
            if not _put_clipboard(text_bytes):
                time.sleep(0.15)
                continue

            # Проверка А: AppleScript — самый надёжный путь на macOS.
            script = 'tell application "System Events" to key code 9 using command down'
            result = subprocess.run(["/usr/bin/osascript", "-e", script],
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                inserted = True
                break

            # Проверка Б: pynput, если System Events недоступен.
            try:
                kb.press(KeyboardKey.cmd); kb.press('v')
                kb.release('v'); kb.release(KeyboardKey.cmd)
                inserted = True
                break
            except Exception:
                pass
            time.sleep(0.2)

        if not inserted:
            # Набор посимвольно: медленно, зато не зависит ни от буфера, ни от
            # Cmd+V. Буфер при этом уже содержит текст — вставить можно и руками.
            try:
                kb.type(text)
                inserted = True
                print("[insert] вставка набором текста")
            except Exception:
                pass

        if inserted:
            print(f"[insert success] '{text[:30]}...'")
            if RESTORE_CLIPBOARD:
                _restore_clipboard_async(old_clipboard, text_bytes)
        else:
            # Буфер СОЗНАТЕЛЬНО не восстанавливаем: текст остаётся в нём и его
            # можно вставить руками. Раньше буфер затирался прежним содержимым,
            # и результат диктовки пропадал совсем.
            print("[insert fail] Текст остался в буфере обмена — нажми Cmd+V")
            notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Cmd+V")

    except Exception as e:
        # Текст к этому моменту уже в буфере — сообщаем, а не молчим: молчание
        # здесь означало бы потерянную диктовку без всякого следа.
        print(f"[insert error] {e}")
        notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Cmd+V")

artifacts_removed: list[str] = []   # что вырезали в последней обработке — для уведомления
audio_dropouts: list[str] = []      # переполнения входного буфера за время записи
                                    # (сэмплы, выброшенные железом до распознавания)

# strip_asr_artifacts/NARRATOR_LOOP_PATTERN/BOH_TAIL_MARKERS/BRAND_NAMES переехали
# в speech_engine.watermarks (тот же алгоритм, дословно) — единственное отличие:
# там функция ВОЗВРАЩАЕТ список того, что вырезала, вместо мутации глобального
# artifacts_removed (нужно было для безопасности при нескольких процессах на
# один пакет, см. speech-engine/agent.md). clean_noise ниже — прежний вызов
# на прежнем месте, просто docstring короче: подробности теперь в пакете.
def clean_noise(text: str) -> str:
    """Снимает водяные знаки ASR и нормализует написание брендов. Слова не трогает."""
    if not text:
        return ""
    text, removed = speech_engine.watermarks.clean_noise(text)
    artifacts_removed.extend(removed)
    return text

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

# ─── Каскад распознавания ──────────────────────────────────────────────────────
# _change_tempo, _restore_timeline, create_audio_wav, _find_degenerate_segments,
# _words_in_range, _text_from_response, _throttle, transcribe_deepgram,
# transcribe_cloud_turbo, _transcribe_local, _transcribe_one_chunk, _needs_retry,
# _split_audio, _norm_word, _drop_overlap, _fmt_mmss, _overlap_word_count,
# _join_chunks, _recognize_with_whisper — весь этот блок (было ~700 строк) переехал
# в общий пакет speech_engine (профиль dictation), без изменения поведения и
# констант. Единственный узел, через который process_audio теперь получает текст —
# _engine.recognize(audio, dur), см. process_audio ниже. Устройство каскада,
# все замеры и красные зоны — ~/speech-engine/agent.md.

def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, session_phase
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        schedule_eval_sample_collect(audio, dur, session_id)

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        # ─── Каскад распознавания (speech_engine, профиль dictation) ───────────
        # Deepgram → Groq (со всей обвязкой: гейт плотности, стилевой проход,
        # перенос знаков/терминов/форм) → локальная модель, по очереди. Устройство
        # каскада — ~/speech-engine/agent.md, не здесь.
        t_asr_start = time.time()
        result = _engine.recognize(audio, dur)
        engine_name = result.engine
        full_raw_text = result.text
        assembled_text = result.assembled_text
        lost_marks = result.lost_marks
        chunk_quality = result.chunk_quality
        t_asr_done = time.time()

        if engine_name == "deepgram":
            print(f"[engine] Deepgram {DEEPGRAM_MODEL}, "
                  f"{result.meta.get('deepgram_seconds', 0.0):.1f}с")
            print(f"[raw deepgram] '{full_raw_text}'")
        elif full_raw_text:
            print(f"[engine] {engine_name} (запасной путь)")

        if not full_raw_text:
            finalize_eval_sample_meta(session_id, dur, "", "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            return

        # Таблица названий работает ВСЕГДА, даже когда второго прохода не было:
        # короткая диктовка, отказ сети, исчерпанная квота. Сети не требует.
        # (Применена внутри _engine.recognize — здесь только печать счётчика.)
        if result.terms_fixed:
            print(f"[terms] по таблице поправлено: {result.terms_fixed}")

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
            finalize_eval_sample_meta(
                session_id, dur, full_raw_text, text,
                stages={
                    "engine": engine_name,
                    "api_text": result.meta.get("api_text", ""),
                    "assembled": assembled_text,
                    "degenerate": result.degenerate,
                    "dropouts": len(audio_dropouts),
                })

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
            if audio_dropouts:
                # Это единственный класс потерь, который распознавание не лечит:
                # звука уже нет. Пользователь должен видеть разницу.
                problems.append(f"микрофон не успевал: пропусков {len(audio_dropouts)}")
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

                audio_dropouts.clear()
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

    if DEEPGRAM_ENABLED:
        print(f"Распознавание: 1) Deepgram {DEEPGRAM_MODEL} → 2) {CLOUD_WHISPER_MODEL} "
              f"→ 3) локальная модель")
    else:
        # Молчаливая работа на второй ступени — худший исход: качество ниже на
        # восемь пунктов, а человек об этом не знает и винит распознавание.
        print(f"Распознавание: {CLOUD_WHISPER_MODEL} (Deepgram не подключён — "
              f"он точнее на 8 пунктов и вдвое быстрее)")
        print("   Чтобы включить: допиши в файл .env строку DEEPGRAM_API_KEY=<ключ>")
    print(f"   промпт: {'выключен' if not DICTATION.groq_context_prompt else 'включён'}")
    try:
        print("Загрузка локальной модели (запасной вариант, если облако недоступно)...")
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=False)
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)
        # Модель грузится тут (дорого, после баннера сбора корпуса) — отдаём её
        # в Engine как третью ступень каскада (используется, только когда и
        # Deepgram, и Groq недоступны или ничего не вернули).
        _engine.ctx.local_model = model

        if USE_CLOUD or DEEPGRAM_ENABLED:
            def warm_network():
                for host in (("https://api.deepgram.com" if DEEPGRAM_ENABLED else None),
                             ("https://api.groq.com" if USE_CLOUD else None)):
                    if not host:
                        continue
                    try: http_session.head(host, timeout=2.0)
                    except Exception: pass
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
