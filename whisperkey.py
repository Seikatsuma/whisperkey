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
from collections import Counter, deque

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
import base64

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
from speech_engine.netcheck import internet_down

# Плашка внизу экрана, как у Wispr Flow (pill.py): полоска — ждёт, капсула с
# волной — пишет, капсула со спиннером — распознаёт. Пока она работает, баннеры
# «Запись / Распознаю / Текст готов» не нужны — их видно по плашке; баннеры о
# проблемах остаются, в них текст. Не Mac или WHISPERKEY_PILL=0 — всё как раньше.
import pill as _pill_mod
pill = _pill_mod.create()


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

# Гейт тишины: пик RMS записи ниже этого уровня — речи не было (случайное
# касание, шум вентилятора). Стоит между полом комнаты (~-50 дБ) и самой
# тихой речью в корпусе (-38 дБ), поэтому настоящие тихие слова не режет.
SILENCE_GATE_DB = -45.0
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

# ─── Постоянное локальное хранилище записей ──────────────────────────────────
# Каждая диктовка дублируется сюда рядом с корпусом оценки качества. Это место
# НЕ синхронизируется с iCloud (~/Library/Application Support), чтобы записи
# оставались только на этом Mac. Срок жизни — 24 часа; очистка включается
# отдельным скриптом и LaunchAgent (см. tools/whisperkey-recording-cleanup.sh).
# Папку можно переопределить переменной окружения WHISPERKEY_RECORDINGS_DIR.
RECORDINGS_ENABLED = True
RECORDINGS_ROOT = os.path.expanduser(
    os.environ.get("WHISPERKEY_RECORDINGS_DIR", "~/Library/Application Support/WhisperKey/Recordings")
)
RECORDINGS_MIN_DURATION = 0.5          # короче — случайный шум, не сохраняем

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
# «Залипание» двойным касанием — как у Wispr Flow: тап-тап Option включает
# запись без удержания (диктовка идёт, руки свободны), следующее нажатие её
# завершает. Обычный «нажал-держи-говори» не меняется.
# Окно — от ОТПУСКАНИЯ первого касания (не от начала записи): в документации
# Wispr «within half a second», а счёт от старта на деле режет спокойный темп —
# тап 300 мс + пауза 300 мс уже не лезла. Живой замер на маке Егора (журнал
# ~/.whisperkey_taps.log): его тап ~720 мс, а второе нажатие приходит в
# обработчик через ~1 мс после события отпускания — macOS отдаёт модификаторы
# обработчику пачкой, и физический зазор между касаниями в миллисекунды
# доставки НЕ переводится. Поэтому фильтра «минимальный зазор» здесь нет
# вообще: он съедал настоящие вторые касания, а дребезга у pynput-модификатора
# практически не бывает — лишнее залипание от гипотетического дребезга
# безобидно и снимается следующим нажатием.
LATCH_TAP_MAX_SEC = 1.3    # первое нажатие короче — «тап»; длиннее — «держать»
LATCH_WINDOW_SEC = 0.7     # окно второго касания после отпускания тапа
LATCH_CANCEL_SEC = 0.5     # нажатие в первые 0.5 с залипания — отмена без текста
LATCH_MAX_SEC = 900        # потолок залипшей записи — страховка от «забыл включённым»
latch_active = False       # запись «залипла»: следующее нажатие завершит её
_latch_deadline = 0.0      # конец окна второго касания (= отпускание тапа + 0.7 с)
_latch_started_ts = 0.0    # когда залипание вступило — для окна отмены
_last_release_ts = 0.0     # время отпускания — для диагностики окна залипания
_release_seq = 0           # поколение отпускания; отменяет просроченный финализ
session_counter = 0
state_lock = threading.Lock()
session_phase = "idle"   # idle -> recording -> processing
active_session_id = 0
audio_stream = None # CEO Fix: Инициализируем при нажатии

# Поток звука на мост ВО ВРЕМЯ записи (22.10.26). Пока Option зажат, блоки
# микрофона уходят по туннелю в /audio/stream моста — к отпусканию звук уже на
# сервере, и мост сразу распознаёт: нет FLAC-кодирования и отправки файла после
# записи (замер на сервере: медиана 0.87 с против 1.82 с, точность не ниже).
# Любой отказ потока → прежний путь _engine.recognize по той же записи.
# Отключить: WHISPERKEY_NO_STREAM=1.
STREAM_ENABLED = not os.environ.get("WHISPERKEY_NO_STREAM")
_live_stream = None          # поток текущей записи — его кормит audio_callback
_live_streams: dict = {}     # session_id -> RemoteStream, забирает process_audio

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
    engine_mode=("remote" if sys.platform == "darwin" and os.environ.get("ASR_ENGINE", "auto").strip().lower() == "auto"
                 else os.environ.get("ASR_ENGINE", "auto")),
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
    """Крупная плашка в терминале — её видно при каждом запуске, пока сбор идёт.

    Плашка "СБОР ЗАВЕРШЁН... разбери корпус диктовок" убрана 16.08.26: корпус
    (256 записей) уже выгружен и разобран, повторять призыв к действию, которое
    уже сделано, при каждом запуске незачем.
    """
    if eval_collection_finished or not active:
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

def _recording_collect_worker(audio: np.ndarray, dur: float, session_id: int) -> None:
    """Сохраняет запись в постоянное локальное хранилище (24 часа)."""
    if not RECORDINGS_ENABLED or dur < RECORDINGS_MIN_DURATION:
        return
    try:
        os.makedirs(RECORDINGS_ROOT, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fname = f"{stamp}_{dur:.1f}s_id{session_id}.wav"
        wav_path = os.path.join(RECORDINGS_ROOT, fname)
        _write_raw_recording_wav(audio, wav_path)
        print(f"[recordings] Сохранено: {fname}")
    except Exception as e:
        print(f"[recordings] save error: {e}")


def schedule_recording_collect(audio: np.ndarray, dur: float, session_id: int) -> None:
    """Фоновое сохранение записи в 24-часовое хранилище."""
    if not RECORDINGS_ENABLED or dur < RECORDINGS_MIN_DURATION:
        return
    audio_copy = np.array(audio, dtype=np.float32, copy=True)
    threading.Thread(
        target=_recording_collect_worker,
        args=(audio_copy, dur, session_id),
        daemon=True,
    ).start()


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
        ls = _live_stream
        if ls is not None:
            ls.feed(block)
        if pill.active:
            pill.set_level_rms(float(np.sqrt(np.mean(block * block))))
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
INSERT_VERIFY_SEC = 1.2       # окно опроса AX-поля после Cmd+V
OPTION_WAIT_SEC = 1.5         # сколько ждём отпускания Option перед вставкой

_IS_MAC = (sys.platform == "darwin")
_opt_down = False             # Option физически зажат — по событиям listener'а

# ─── Быстрый путь вставки внутри процесса (pyobjc) ──────────────────────────
# Каждый вызов osascript — порождение процесса ~0.1–0.3с; на нечитаемом поле
# их шло 3–4 подряд — это почти вся видимая секунда между гашением плашки и
# текстом (Егор 19.10.26: «после загрузки жду ~секунду»). AppKit/Quartz уже
# приезжают с pynput, поэтому AX-запись, буфер, пункт меню «Вставить» и Cmd+V
# делаем прямыми вызовами API за миллисекунды; любой отказ быстрого пути →
# прежний osascript-путь, поведение не меняется.
_AXAPI = None
_NSWorkspace = None
_NSPasteboard = None
_PBSTR = None
try:
    if _IS_MAC:
        import ApplicationServices as _AXAPI_mod
        from AppKit import NSWorkspace as _NSW, NSPasteboard as _NSP
        try:
            from AppKit import NSPasteboardTypeString as _PBSTR_mod
        except ImportError:
            from AppKit import NSStringPboardType as _PBSTR_mod
        _AXAPI = _AXAPI_mod
        _NSWorkspace = _NSW
        _NSPasteboard = _NSP
        _PBSTR = _PBSTR_mod
except Exception:
    _AXAPI = None

def _clipboard_now() -> bytes:
    if _NSPasteboard is not None:
        try:
            s = _NSPasteboard.generalPasteboard().stringForType_(_PBSTR)
            return (s or "").encode("utf-8")
        except Exception:
            pass
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
        if _NSPasteboard is not None:
            try:
                pb = _NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(data.decode("utf-8"), _PBSTR)
                if (pb.stringForType_(_PBSTR) or "").encode("utf-8") == data:
                    return True
            except Exception:
                pass
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

# ─── Проверка ФАКТА вставки — политика 19.10.26 (второй проход) ───────────────
# Первый проход лечил «success по returncode»; второй убирает СЛЕПЫЕ удары:
# старый код при нечитаемом поле (Brave) слал Cmd+V и следом добивку через
# меню — два удара в одном цикле, отсюда живые дубли текста.
#
# Новая лестница — каждая ступень только после ДОКАЗАННОГО провала предыдущей,
# и ни одной пары слепых ударов в цикле:
#   1. _ax_smart_insert — ОДИН osascript: фокус + разблокировка дерева
#      AXEnhancedUserInterface (Chromium/Electron отдают поля только после неё)
#      + сплайс по каретке + запись AXValue + чтение назад. Проверка — в Python
#      (_insert_verified). Буфер и клавиши не трогаются.
#   2. _ax_selected_text_insert — запись в AXSelectedText: принимается рядом
#      полей с readonly AXValue. «OK» — атрибут принят элементом.
#   3. _js_insert — браузеры: AppleScript-JavaScript активной вкладки
#      (document.execCommand('insertText') → чтение document.activeElement
#      назад) — настоящая проверка в поле, недоступном AX. Требует во
#      включённого «Allow JavaScript from Apple Events» (меню браузера) —
#      при выключенном флаге один раз показываем пользователю, что включить,
#      и дальше работаем без JS.
#   4. Буферный путь: читаемое поле — Cmd+V/меню с реальными вердиктами
#      _poll_insert; нечитаемое — РОВНО ОДИН удар (меню «Вставить», оно
#      само сообщает об отказе; отказ меню → один Cmd+V), без опроса и без
#      добивок — честный [insert sent].
#   5. Доказанный отказ всех транспортов → посимвольный ввод kb.type
#      (Unicode-события); всё отказало → [insert FAILED] + звук + notify,
#      текст остаётся в буфере.
# Буфер обмена пишется ТОЛЬКО если дошли до буферного пути — при AX/JS-вставке
# буфер пользователя остаётся его буфером вообще без временной подмены.

def _osa(script: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Один вызов osascript: (returncode==0, stdout)."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, timeout=timeout, text=True)
        return r.returncode == 0, (r.stdout or "").rstrip("\n")
    except Exception:
        return False, ""

def _fail_sound() -> None:
    """Слышимый сигнал провала — диктующий не смотрит в терминал."""
    if not _IS_MAC:
        return
    def _play():
        try:
            subprocess.run(["afplay", "/System/Library/Sounds/Basso.aiff"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()

def _frontmost_app() -> str:
    if not _IS_MAC:
        return ""
    ok, out = _osa('tell application "System Events" to get name of '
                   'first application process whose frontmost is true')
    return out.strip() if ok else ""

# Текст фокусного поля через Accessibility API. Формат ответа:
# «<приложение>\n<роль>\n<значение>»; «<<NOFOCUS>>»/«<<NOVALUE>>» — нечитаемо.
_AX_FOCUS_SCRIPT = '''tell application "System Events"
    set fp to first application process whose frontmost is true
    set appName to name of fp
    try
        set el to value of attribute "AXFocusedUIElement" of fp
    on error
        return appName & linefeed & "<<NOFOCUS>>"
    end try
    set r to ""
    try
        set r to role of el
    end try
    try
        set v to value of attribute "AXValue" of el
        if v is missing value then return appName & linefeed & r & linefeed & "<<NOVALUE>>"
        return appName & linefeed & r & linefeed & (v as text)
    on error
        return appName & linefeed & r & linefeed & "<<NOVALUE>>"
    end try
end tell'''

def _ax_focused_value() -> tuple:
    """(текст поля | None, приложение, роль). None — поле через AX не читается."""
    if not _IS_MAC:
        return None, "", ""
    ok, out = _osa(_AX_FOCUS_SCRIPT, timeout=3.0)
    if not ok or not out:
        return None, "", ""
    parts = out.split("\n", 2)
    app = parts[0].strip()
    role = parts[1].strip() if len(parts) > 1 else ""
    if len(parts) < 3 or parts[2] in ("<<NOFOCUS>>", "<<NOVALUE>>"):
        return None, app, role
    return parts[2], app, role

def _norm_for_cmp(s: str) -> str:
    """Сравнительная норма: только буквы/цифры, строчные — автозамена кавычек
    и умная пунктуация приложения не должны ломать сверку."""
    return re.sub(r'[^\w]+', '', s.lower())

def _insert_verified(inserted: str, before, after) -> bool | None:
    """Факт вставки по AX-снимку поля. True — наш текст виден в поле; False —
    поле читается, а текста в нём нет; None — поле нечитаемо (ни успех, ни провал)."""
    if after is None:
        return None
    tail = _norm_for_cmp(inserted)[-24:]
    if tail and tail in _norm_for_cmp(after):
        return True
    if before is not None and len(after) - len(before) >= len(inserted) * 0.9:
        return True
    return False

# Прямая запись текста в фокусное поле через Accessibility — буфер обмена
# не трогается ВООБЩЕ. Один osascript делает всё: находит фокусный элемент,
# при надобности включает AXEnhancedUserInterface (Chromium/Electron не отдают
# текстовые поля в AX-дерево, пока её не выставить — именно этим лечится класс
# «нечитаемое веб-поле» без всякого действия пользователя), сплайсит по
# AXSelectedTextRange (позиция курсора/замена выделения), пишет AXValue и
# читает назад. Формат ответа: «<приложение>\n<статус>\n<роль>\n<значение>»;
# статус: WROTE (записали, значение = поле ПОСЛЕ), WRITEFAIL (читаемо, запись
# отказана, значение = поле до), UNREADABLE (AXValue нет и после разблокировки),
# NOFOCUS (фокусного элемента нет), ERROR. Проверку WROTE делает Python
# (_insert_verified — та же нормализация, что и опрос: «умные» кавычки
# приложения не должны ломать сверку).
_AX_SMART_SCRIPT = '''on run argv
    set ins to item 1 of argv
    set tryUnlock to (item 2 of argv is "1")
    tell application "System Events"
        set fp to first application process whose frontmost is true
        set appName to name of fp
        set el to missing value
        try
            set el to value of attribute "AXFocusedUIElement" of fp
        end try
        if el is missing value and tryUnlock then
            try
                set value of attribute "AXEnhancedUserInterface" of fp to true
                delay 0.05
                set el to value of attribute "AXFocusedUIElement" of fp
            end try
        end if
        if el is missing value then
            return appName & linefeed & "NOFOCUS" & linefeed & "" & linefeed & ""
        end if
        set r to ""
        try
            set r to role of el
        end try
        set cur to missing value
        try
            set cur to value of attribute "AXValue" of el
        end try
        if cur is missing value and tryUnlock then
            try
                set value of attribute "AXEnhancedUserInterface" of fp to true
                delay 0.05
                set el to value of attribute "AXFocusedUIElement" of fp
                set cur to value of attribute "AXValue" of el
            end try
        end if
        if cur is missing value then
            return appName & linefeed & "UNREADABLE" & linefeed & r & linefeed & ""
        end if
        set curText to cur as text
        set curLen to count of curText
        set loc to curLen
        set selLen to 0
        try
            set sel to value of attribute "AXSelectedTextRange" of el
            set loc to location of sel
            set selLen to length of sel
        end try
        if loc < 0 then set loc to 0
        if loc > curLen then set loc to curLen
        set leftPart to ""
        if loc > 0 then set leftPart to text 1 thru loc of curText
        set rightPart to ""
        if (curLen - loc - selLen) > 0 then
            set rightPart to text (loc + selLen + 1) thru curLen of curText
        end if
        try
            set value of attribute "AXValue" of el to leftPart & ins & rightPart
        on error
            return appName & linefeed & "WRITEFAIL" & linefeed & r & linefeed & curText
        end try
        try
            set value of attribute "AXSelectedTextRange" of el to {loc + (count of ins), 0}
        end try
        try
            set v2 to value of attribute "AXValue" of el
            return appName & linefeed & "WROTE" & linefeed & r & linefeed & (v2 as text)
        on error
            return appName & linefeed & "WROTE" & linefeed & r & linefeed & ""
        end try
    end tell
end run'''

# Приложения, где разблокировка AXEnhancedUserInterface уже не помогла в этой
# сессии — повторные попытки внутри вызова стоят ~0.1с на каждой диктовке
# (жалоба «долго жду вставки», 19.10.26).
_ax_unlock_failed: set = set()

def _ax_get(el, attr):
    """(ok, value) для AX-атрибута в обёртке — pyobjc возвращает (err, val)."""
    try:
        err, val = _AXAPI.AXUIElementCopyAttributeValue(el, attr, None)
        return err == 0, val
    except Exception:
        return False, None

def _ax_smart_insert_fast(text: str, try_unlock: bool = True):
    """Тот же контракт, что у _ax_smart_insert, но прямыми AX-вызовами —
    без osascript (~0.01–0.03с вместо ~0.15–0.3с). None — быстрый путь
    недоступен/сброшен, работает медленный."""
    if _AXAPI is None or _NSWorkspace is None:
        return None
    try:
        fa = _NSWorkspace.sharedWorkspace().frontmostApplication()
        if fa is None:
            return "", "NOFOCUS", "", ""
        app = fa.localizedName() or ""
        pid = fa.processIdentifier()
        sw = _AXAPI.AXUIElementCreateSystemWide()
        ok, el = _ax_get(sw, "AXFocusedUIElement")
        app_el = _AXAPI.AXUIElementCreateApplication(pid)
        if (not ok or el is None) and try_unlock:
            _AXAPI.AXUIElementSetAttributeValue(
                app_el, "AXEnhancedUserInterface", True)
            time.sleep(0.03)
            ok, el = _ax_get(sw, "AXFocusedUIElement")
        if not ok or el is None:
            return app, "NOFOCUS", "", ""
        _, role = _ax_get(el, "AXRole")
        role = role or ""
        ok, cur = _ax_get(el, "AXValue")
        if (not ok or not isinstance(cur, str)) and try_unlock:
            _AXAPI.AXUIElementSetAttributeValue(
                app_el, "AXEnhancedUserInterface", True)
            time.sleep(0.03)
            ok2, el2 = _ax_get(sw, "AXFocusedUIElement")
            if ok2 and el2 is not None:
                el = el2
            ok, cur = _ax_get(el, "AXValue")
        if not ok or not isinstance(cur, str):
            return app, "UNREADABLE", role, ""
        loc, sel_len = len(cur), 0
        okr, rng = _ax_get(el, "AXSelectedTextRange")
        if okr and rng is not None:
            try:
                okv, cf = _AXAPI.AXValueGetValue(
                    rng, _AXAPI.AXValueGetType(rng), None)
                if okv:
                    try:
                        loc, sel_len = int(cf.location), int(cf.length)
                    except AttributeError:
                        loc, sel_len = int(cf[0]), int(cf[1])
            except Exception:
                pass
        loc = max(0, min(loc, len(cur)))
        sel_len = max(0, min(sel_len, len(cur) - loc))
        new = cur[:loc] + text + cur[loc + sel_len:]
        werr = _AXAPI.AXUIElementSetAttributeValue(el, "AXValue", new)
        if werr != 0:
            return app, "WRITEFAIL", role, cur
        try:
            r2 = _AXAPI.AXValueCreate(
                _AXAPI.kAXValueCFRangeType, (loc + len(text), 0))
            _AXAPI.AXUIElementSetAttributeValue(el, "AXSelectedTextRange", r2)
        except Exception:
            pass
        okv2, v2 = _ax_get(el, "AXValue")
        return app, "WROTE", role, (v2 if okv2 and isinstance(v2, str) else "")
    except Exception:
        return None

def _ax_smart_insert(text: str, try_unlock: bool = True) -> tuple:
    """Запись в фокусное поле одним вызовом: (app, status, role, value).
    status: WROTE | WRITEFAIL | UNREADABLE | NOFOCUS | ERROR | NOMAC.
    try_unlock=False — пропустить попытки AXEnhancedUserInterface (когда для
    этого приложения она уже не сработала в этой сессии)."""
    if not _IS_MAC:
        return "", "NOMAC", "", ""
    fast = _ax_smart_insert_fast(text, try_unlock)
    if fast is not None:
        return fast
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", _AX_SMART_SCRIPT, text,
                            "1" if try_unlock else "0"],
                           capture_output=True, timeout=4, text=True)
        out = (r.stdout or "").rstrip("\n")
    except Exception:
        return "", "ERROR", "", ""
    parts = out.split("\n", 3)
    app = parts[0].strip() if parts and parts[0] else ""
    status = parts[1].strip() if len(parts) > 1 else "ERROR"
    role = parts[2].strip() if len(parts) > 2 else ""
    value = parts[3] if len(parts) > 3 else ""
    return app, status, role, value

# Запись в AXSelectedText: часть полей принимает её, даже когда AXValue — на
# чтение-only или отсутствует (веб-контролы, нестандартные редакторы). Это
# тот же «ввод вместо выделенного»: пустое выделение = курсор → чистая вставка.
_AX_SELTEXT_SCRIPT = '''on run argv
    set ins to item 1 of argv
    tell application "System Events"
        set fp to first application process whose frontmost is true
        set appName to name of fp
        try
            set el to value of attribute "AXFocusedUIElement" of fp
        on error
            return appName & linefeed & "ERR:NOFOCUS"
        end try
        try
            set value of attribute "AXSelectedText" of el to ins
            return appName & linefeed & "OK"
        on error errMsg
            return appName & linefeed & "ERR:" & errMsg
        end try
    end tell
end run'''

def _ax_selected_text_insert(text: str) -> tuple:
    """(app, ok) — ok=True: элемент принял запись в AXSelectedText."""
    if not _IS_MAC:
        return "", False
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", _AX_SELTEXT_SCRIPT, text],
                           capture_output=True, timeout=3, text=True)
        out = (r.stdout or "").rstrip("\n")
    except Exception:
        return "", False
    parts = out.split("\n", 1)
    app = parts[0].strip()
    ok = len(parts) > 1 and parts[1].strip() == "OK"
    if not ok and len(parts) > 1:
        print(f"[insert] AXSelectedText отклонён ({parts[1].strip()[:80]})")
    return app, ok

# ─── Один слепой удар одним вызовом ─────────────────────────────────────────
# Для нечитаемого поля проверить вставку нечем, и каждый лишний osascript —
# ~0.1-0.15с ожидания у человека (жалоба «долго жду вставки», 19.10.26).
# Вся лестница слепого пути — в одном процессе: меню «Вставить» целевого
# приложения (не клавиша, а команда приложения; ошибка клика — доказанный
# отказ), при отказе — Cmd+V точечно в процесс, затем во frontmost.
# Между попытками — доказанные отказы, поэтому это ОДИН удар, не добивка.
_BLIND_STRIKE_SCRIPT = '''on run argv
    set proc to item 1 of argv
    tell application "System Events"
        if proc is not "" then
            try
                tell process proc
                    repeat with m in {"Edit", "Правка"}
                        try
                            set mb to menu bar item m of menu bar 1
                            repeat with it in {"Paste", "Вставить"}
                                try
                                    set mi to menu item it of menu 1 of mb
                                    if enabled of mi then
                                        try
                                            click mi
                                        end try
                                        -- Живой пункт нажали — это удар; код ошибки
                                        -- клика может врать ПОСЛЕ вставки, добивка
                                        -- клавишей даст дубль.
                                        return "MENU"
                                    end if
                                end try
                            end repeat
                        end try
                    end repeat
                end tell
            end try
            try
                tell process proc to key code 9 using command down
                return "KEY"
            end try
            try
                tell process proc to keystroke "v" using command down
                return "KEY"
            end try
        end if
        try
            key code 9 using command down
            return "KEYF"
        end try
    end tell
    return "ERR"
end run'''

def _pid_of_app(app: str) -> int:
    """pid приложения по имени (для точечных AX/CGEvent); 0 — не нашли."""
    if _NSWorkspace is None:
        return 0
    try:
        if app:
            low = app.lower()
            for a in _NSWorkspace.sharedWorkspace().runningApplications():
                if (a.localizedName() or "").lower() == low:
                    return int(a.processIdentifier())
        fa = _NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(fa.processIdentifier()) if fa is not None else 0
    except Exception:
        return 0

def _menu_paste_press(pid: int) -> bool:
    """AXPress на пункте «Paste/Вставить» меню «Edit/Правка» — прямой вызов,
    без osascript. Нажимаем только ЖИВОЙ пункт (AXEnabled): клик по мёртвому
    вернул бы успех без вставки — тихая потеря."""
    try:
        app_el = _AXAPI.AXUIElementCreateApplication(pid)
        ok, mb = _ax_get(app_el, "AXMenuBar")
        if not ok or mb is None:
            return False
        ok, bar_items = _ax_get(mb, "AXChildren")
        if not ok or not bar_items:
            return False
        for bi in bar_items:
            _, t = _ax_get(bi, "AXTitle")
            if t not in ("Edit", "Правка"):
                continue
            ok, menus = _ax_get(bi, "AXChildren")
            if not ok or not menus:
                continue
            ok, items = _ax_get(menus[0], "AXChildren")
            if not ok or not items:
                continue
            for it in items:
                _, t2 = _ax_get(it, "AXTitle")
                if t2 not in ("Paste", "Вставить"):
                    continue
                _, en = _ax_get(it, "AXEnabled")
                if en is False:
                    continue
                # Проба на ЖИВОМ пункте — сам удар, независимо от кода возврата:
                # AXPress иногда отчитывается ошибкой ПОСЛЕ реального срабатывания,
                # и добивка Cmd+V поверх давала дубль (скрин Егора 19.10.26).
                _AXAPI.AXUIElementPerformAction(it, "AXPress")
                return True
        return False
    except Exception:
        return False

def _cgevent_paste(pid: int = 0) -> bool:
    """Cmd+V синтезированным событием Quartz: в процесс pid или во frontmost."""
    if _Quartz is None:
        return False
    try:
        src = _Quartz.CGEventSourceCreate(
            _Quartz.kCGEventSourceStateHIDSystemState)
        down = _Quartz.CGEventCreateKeyboardEvent(src, 0x09, True)
        up = _Quartz.CGEventCreateKeyboardEvent(src, 0x09, False)
        _Quartz.CGEventSetFlags(down, _Quartz.kCGEventFlagMaskCommand)
        _Quartz.CGEventSetFlags(up, _Quartz.kCGEventFlagMaskCommand)
        if pid:
            _Quartz.CGEventPostToPid(pid, down)
            _Quartz.CGEventPostToPid(pid, up)
        else:
            _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, down)
            _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, up)
        return True
    except Exception:
        return False

def _blind_strike_fast(app: str):
    """Один удар внутри процесса: живой пункт меню либо один Cmd+V."""
    if _AXAPI is None:
        return None
    pid = _pid_of_app(app)
    if pid and _menu_paste_press(pid):
        return "MENU", ""
    if _cgevent_paste(pid):
        return "KEY" if pid else "KEYF", ""
    return None

def _blind_strike(app: str) -> tuple:
    """Один слепой удар в нечитаемое поле, одним osascript.
    (transport, err) — transport: MENU|KEY|KEYF|"" при полном отказе."""
    if not _IS_MAC:
        return "", "not mac"
    fast = _blind_strike_fast(app)
    if fast is not None:
        return fast
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", _BLIND_STRIKE_SCRIPT,
                            app or ""], capture_output=True, timeout=4, text=True)
        out = (r.stdout or "").strip()
    except Exception as e:
        return "", type(e).__name__
    if not out or out.startswith("ERR"):
        return "", out or "пустой ответ osascript"
    return out, ""

# ─── JS-путь для браузеров ───────────────────────────────────────────────────
# AppleScript-JavaScript активной вкладки: вставка через execCommand('insertText')
# — тот же канал, что и настоящий ввод (события input уходят, React-поля видят
# текст), а чтение document.activeElement назад даёт настоящую проверку в
# полях, недоступных AX. Требует в браузере «Вид → Разработчик →
# Allow JavaScript from Apple Events». Текст едет base64 — ни один символ не
# ломает ни JS, ни AppleScript-строку.
_JS_BROWSERS = ("brave", "chrome", "chromium", "edge", "vivaldi", "opera", "arc")
_js_off: set = set()            # приложения, где Apple Events JS выключен
_js_flag_notified: set = set()  # по которым уже показали пользователю инструкцию

def _is_browser(app: str) -> bool:
    a = (app or "").lower()
    return any(b in a for b in _JS_BROWSERS)

# Терминалы: фокусный элемент — экран вывода, не поле ввода. AX-запись в него
# отчитывается успехом, но текст уходит в никуда; а синтетические клавиши при
# Secure Keyboard Entry (залипает после sudo/ssh-паролей) macOS роняет молча.
# Рабочий транспорт для них один — пункт меню «Вставить» (не клавиша).
# «терминал» кириллицей обязателен: на русской macOS Terminal.app отдаёт
# себя локализованным именем, и латинский список его не ловил — ветка
# «WROTE в экран вывода» не включалась, текст улетал в никуда (27.10.26).
_TERMINALS = ("terminal", "терминал", "iterm", "warp", "alacritty", "kitty",
              "wezterm", "hyper", "tabby", "rio")

def _is_terminal(app: str) -> bool:
    a = (app or "").lower()
    return any(t in a for t in _TERMINALS)

def _secure_input_active() -> bool:
    """Secure Keyboard Entry активен → синтетические клавиши теряются молча."""
    if _Quartz is None or not _IS_MAC:
        return False
    try:
        d = _Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("kCGSSessionSecureInputPID"))
    except Exception:
        return False

_JS_INSERT_TEMPLATE = '''(function(){var t=decodeURIComponent(escape(window.atob("__B64__")));var el=document.activeElement;if(!el||el===document.body)return "NOFOCUS";var ce=el.isContentEditable===true;var fld=(el.tagName==="INPUT"||el.tagName==="TEXTAREA")&&typeof el.value==="string";if(!ce&&!fld)return "NOEL";var ok=false;try{ok=document.execCommand("insertText",false,t);}catch(e){}if(!ok&&typeof el.setRangeText==="function"){try{var s=el.selectionStart==null?0:el.selectionStart;var d=el.selectionEnd==null?s:el.selectionEnd;el.setRangeText(t,s,d,"end");el.dispatchEvent(new Event("input",{bubbles:true}));ok=true;}catch(e){}}var a=ce?(el.textContent||""):(el.value||"");var tail=t.replace(/\\s+$/,"").slice(-24);var seen=tail&&a.indexOf(tail)>=0;if(!ok)return seen?"OKVER":"FAIL";return seen?"OKVER":"OK";})()'''

def _js_insert(app: str, text: str) -> tuple:
    """(status, detail): VERIFIED | OK | FAIL | DISABLED | SKIP."""
    if not _IS_MAC:
        return "SKIP", "not mac"
    key = app.lower()
    if key in _js_off:
        return "SKIP", "js выключен (кэш)"
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    js = _JS_INSERT_TEMPLATE.replace("__B64__", b64)
    safe_app = app.replace("\\", "\\\\").replace('"', '\\"')
    js_q = '"' + js.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = (f'tell application "{safe_app}" to execute '
              f"front window's active tab javascript {js_q}")
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, timeout=4, text=True)
    except Exception as e:
        return "FAIL", type(e).__name__
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        low = err.lower()
        if "javascript" in low or "apple events" in low or "-1743" in low:
            _js_off.add(key)
            return "DISABLED", err
        return "FAIL", err or f"rc={r.returncode}"
    if out == "OKVER":
        return "VERIFIED", ""
    if out == "OK":
        return "OK", ""
    return "FAIL", out or "пустой ответ"

def _js_flag_notify_once(app: str) -> None:
    """Одно понятное действие один раз: как включить JavaScript from Apple Events."""
    key = app.lower()
    if key in _js_flag_notified:
        return
    _js_flag_notified.add(key)
    notify("WhisperKey — точная вставка в браузер",
           f"В {app} включи: меню Вид → Разработчик → "
           f"Allow JavaScript from Apple Events. Тогда диктовка в веб-поля "
           f"вставляется с проверкой, без буфера.")

def _send_paste_keystroke_pynput() -> bool:
    """Cmd+V через pynput — другой транспорт, на случай если System Events не дошёл."""
    try:
        kb.press(KeyboardKey.cmd)
        kb.press('v')
        kb.release('v')
        kb.release(KeyboardKey.cmd)
        return True
    except Exception:
        return False

def _poll_insert(text: str, before, timeout: float = INSERT_VERIFY_SEC):
    """Опрашивает AX-значение фокусного поля после отправки вставки.
    Поле, которое за ~0.35с ни разу не отдало значение, нечитаемо — дальше
    крутить опрос бессмысленно и это лишняя задержка перед ответом."""
    deadline = time.time() + timeout
    unreadable_deadline = time.time() + 0.35
    saw_readable = False
    first = True
    while time.time() < deadline:
        # Вставка обычно видна через ~30-80 мс — первый опрос пораньше.
        time.sleep(0.06 if first else 0.12)
        first = False
        after, _app, _role = _ax_focused_value()
        if after is None:
            if not saw_readable and time.time() > unreadable_deadline:
                return None
            continue
        saw_readable = True
        if _insert_verified(text, before, after):
            return True
    return False if saw_readable else None

def _send_paste_keystroke(target_app: str = "") -> bool:
    """Ровно один Cmd+V в целевой процесс; True — событие отправлено."""
    pid = _pid_of_app(target_app)
    if _Quartz is not None:
        return _cgevent_paste(pid)
    safe = target_app.replace("\\", "\\\\").replace('"', '\\"')
    if safe:
        ok, _ = _osa(f'tell application "System Events" to tell process "{safe}" '
                     'to key code 9 using command down', timeout=5)
    else:
        ok, _ = _osa('tell application "System Events" to key code 9 using command down',
                     timeout=5)
    return ok

# Третий транспорт вставки: сама команда меню «Правка → Вставить» целевого
# приложения. Это другой механизм — не синтетическая клавиша, а вызов команды,
# который приложение обрабатывает само. Живой случай 19.10.26: два Cmd+V
# (System Events + pynput) «ушли», поле Brave их проглотило молча.
_AX_MENU_PASTE_SCRIPT = '''on run argv
    set proc to item 1 of argv
    tell application "System Events"
        tell process proc
            repeat with m in {"Edit", "Правка"}
                try
                    set mb to menu bar item m of menu bar 1
                    repeat with it in {"Paste", "Вставить"}
                        try
                            click menu item it of menu 1 of mb
                            return "OK"
                        end try
                    end repeat
                end try
            end repeat
        end tell
    end tell
    return "ERR"
end run'''

def _send_paste_via_menu(target_app: str) -> tuple[bool, str]:
    if not target_app or not _IS_MAC:
        return False, "нет целевого приложения"
    # Сначала прямой AXPress через Accessibility (не зависит от имени процесса
    # и от права «управлять System Events» — osascript-путь мог молча не найти
    # процесс по имени и уронить всю вставку в терминал).
    pid = _pid_of_app(target_app)
    if pid and _menu_paste_press(pid):
        return True, ""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", _AX_MENU_PASTE_SCRIPT,
                            target_app], capture_output=True, timeout=3, text=True)
        if (r.stdout or "").strip() == "OK":
            return True, ""
        err = (r.stderr or "").strip()
        return False, err or "меню «Вставить» не нашлось/не кликнулось"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def _activate_app(name: str) -> bool:
    if not name:
        return False
    safe = name.replace("\\", "\\\\").replace('"', '\\"')
    ok, _ = _osa(f'tell application "{safe}" to activate', timeout=3)
    return ok

try:
    import Quartz as _Quartz                       # приезжает с pynput (pyobjc)
except Exception:
    _Quartz = None
_kCGFlagAlternate = 0x00080000    # kCGEventFlagMaskAlternate — бит Option
_kCGHIDState = 1                  # kCGEventSourceStateHIDSystemState

def _option_physically_down() -> bool | None:
    """Реальное состояние Option по HID-флагам — в отличие от _opt_down не
    залипает, когда событие отпускания потерялось в дороге. None — прочитать
    не удалось, тогда верим флагу listener'а."""
    if _Quartz is None or not _IS_MAC:
        return None
    try:
        flags = _Quartz.CGEventSourceFlagsState(_kCGHIDState)
        return bool(flags & _kCGFlagAlternate)
    except Exception:
        return None

def _option_is_down() -> bool:
    real = _option_physically_down()
    return _opt_down if real is None else real

def _wait_option_release() -> bool:
    """Ждёт отпускания Option перед вставкой (до OPTION_WAIT_SEC).

    Зажатый Option превращает Cmd+V в Cmd+Opt+V — «вставить и согласовать
    стиль», а в части приложений — в ничего. При залипшей записи или быстром
    следующем тапе клавиша легко ещё зажата к моменту вставки — живой класс
    «курсор стоит, текста нет». Читаем физическое состояние через Quartz:
    _opt_down может застрять True при потерянном событии отпускания, и тогда
    КАЖДАЯ вставка ждала бы 1.5 с и уходила в медленный посимвольный набор."""
    if not _option_is_down():
        return True
    deadline = time.time() + OPTION_WAIT_SEC
    while _option_is_down() and time.time() < deadline:
        time.sleep(0.05)
    return not _option_is_down()

def direct_insert(text: str) -> float:
    """Лестница транспортов с проверкой факта на каждой ступени.
    Возвращает длительность вставки, сек (для строки [timing])."""
    t_ins0 = time.time()
    try:
        text_bytes = text.encode('utf-8')
        old_clipboard = b""
        clipboard_touched = False
        verdict = None    # True — доказано; False — доказанный отказ; None — непроверяемо
        sent = False      # хоть один транспорт сообщил о доставке
        via = ""
        reason = ""

        # ── Ступень 1: прямая AX-запись с verify одним вызовом ───────────────
        # Цель — текущее поле на момент вставки, не на момент начала записи.
        # Разблокировку пропускаем для приложений, где она уже не помогла.
        app, st, role, val = _ax_smart_insert(text, try_unlock=True)
        focus_hint = (app or "").lower()
        readable = st in ("WROTE", "WRITEFAIL")
        before = val if readable else None
        if st == "UNREADABLE":
            _ax_unlock_failed.add(focus_hint)

        if st == "WROTE":
            # WROTE означает, что AXSetValue уже выполнился. Текст может исчезнуть
            # из поля до read-back, если пользователь мгновенно нажал Enter.
            # Поэтому отсутствие текста после записи — не разрешение на второй
            # транспорт: иначе отправленное сообщение тут же появится в поле снова.
            sent, via = True, "ax-write"
            if _insert_verified(text, None, val):
                verdict = True
                print("[insert] прямая запись в поле (AX) — проверено")
            elif _is_terminal(app):
                # Терминал: «запись» прошла в экран вывода, не в поле ввода —
                # текст ушёл в никуда. Это отказ транспорта, не «поле
                # изменилось»: идём дальше по лестнице к меню «Вставить»
                # (жалоба Егора 24.10: «в терминале не вставляется»).
                sent, via = False, ""
                reason = "AX-запись прошла в экран терминала — не в поле ввода"
                print(f"[insert] {reason} — меню «Вставить»")
            else:
                print("[insert] прямая запись в поле (AX) отправлена; "
                      "поле уже изменилось — без повторной вставки")
        elif st == "WRITEFAIL":
            reason = f"поле отклонило прямую запись ({app or '?'})"
            print(f"[insert] {reason} — буферный путь")

        # ── Ступень 2: нечитаемое поле — JS-путь браузера (проверяемая вставка)
        if verdict is not True and st in ("UNREADABLE", "NOFOCUS") \
                and _is_browser(app):
            st2, det = _js_insert(app, text)
            if st2 == "VERIFIED":
                verdict, via = True, "js"
                print("[insert] вставка через JS вкладки — проверено")
            elif st2 == "OK":
                sent, via = True, "js"
                print("[insert] JS вставил, чтение назад не подтвердило")
            elif st2 == "DISABLED":
                _js_flag_notify_once(app)
                print(f"[insert] JS в {app} выключен — {det[:80]}")
            else:
                print(f"[insert] JS-путь не сработал ({st2}: {det[:80]})")

        # ── Ступень 3: буферный путь (только если детерминированные не сработали)
        if verdict is not True and not sent:
            if not _wait_option_release():
                # Option зажат — Cmd+V станет «со стилем». Посимвольный ввод.
                try:
                    kb.type(text)
                    sent, via = True, "type"
                    if readable:
                        v2 = _poll_insert(text, before)
                        if v2 is True:
                            verdict = True
                            print("[insert] вставка набором текста")
                        elif v2 is False:
                            verdict = False
                            reason = "поле не приняло посимвольный ввод"
                except Exception as e:
                    reason = f"посимвольный ввод не прошёл ({type(e).__name__})"
            else:
                old_clipboard = _clipboard_now()
                if readable:
                    # Один отправленный транспорт на диктовку. Пустое поле после
                    # Cmd+V может означать, что пользователь уже отправил текст.
                    if not _put_clipboard(text_bytes):
                        reason = "буфер не записался"
                    else:
                        clipboard_touched = True
                        if (_is_terminal(app) or _secure_input_active()):
                            # Терминал и Secure Keyboard Entry: синтетические
                            # клавиши теряются молча — идём сразу в меню
                            # «Вставить», это не клавиша и не блокируется.
                            ok_m, m_err = _send_paste_via_menu(app)
                            if ok_m:
                                sent, via = True, "menu"
                                verdict = _poll_insert(text, before)
                                if verdict is True:
                                    print("[insert] меню «Вставить» — проверено")
                                else:
                                    verdict = None
                            elif _send_paste_keystroke(app):
                                # Меню не нашлось/не кликнулось — всё равно
                                # бьём клавишей: если Secure Input неактивен,
                                # это рабочий транспорт.
                                sent, via = True, "paste"
                                verdict = _poll_insert(text, before)
                                if verdict is not True:
                                    verdict = None
                            else:
                                reason = f"меню «Вставить» не сработало: {m_err}"
                                print(f"[insert] {reason}")
                        elif _send_paste_keystroke(app):
                            sent, via = True, "paste"
                            verdict = _poll_insert(text, before)
                            if verdict is True:
                                print("[insert] Cmd+V — проверено")
                            else:
                                verdict = None
                                print("[insert] Cmd+V отправлен; поле уже изменилось — "
                                      "без повторной вставки")
                        else:
                            reason = ("клавиша Cmd+V не отправлена — проверь права "
                                      "Accessibility/Input Monitoring у Терминала")
                            ok_m, m_err = _send_paste_via_menu(app)
                            if ok_m:
                                sent, via = True, "menu"
                                verdict = _poll_insert(text, before)
                                if verdict is not True:
                                    verdict = None
                            else:
                                print(f"[insert] меню «Вставить» не сработало: {m_err}")
                                reason = reason or f"меню: {m_err}"
                else:
                    # Нечитаемое поле: РОВНО ОДИН слепой удар одним вызовом —
                    # меню «Вставить», при отказе точечный Cmd+V, во frontmost —
                    # внутри того же osascript. Без опроса и добивок: пара
                    # слепых ударов — механизм дубля, а лишние вызовы — это
                    # жалоба «долго жду вставки» (Егор 19.10.26).
                    if _put_clipboard(text_bytes):
                        clipboard_touched = True
                        via, err = _blind_strike(app)
                        if via:
                            sent = True
                            print(f"[insert] проверка недоступна — один удар ({via})")
                        else:
                            reason = (f"ни меню, ни Cmd+V не ушли ({err}) — "
                                      "проверь права Accessibility у Терминала")
                    else:
                        reason = "буфер не записался"

        # ── Ступень 4: доказанный отказ/неотправка → посимвольный ввод ───────
        # Unicode-событиям не мешают ни буфер, ни зажатый модификатор.
        # При sent=True по слепому удару НЕ набираем — тот уже мог встать,
        # набор был бы вторым текстом (дубль). via=="type" — набор уже был
        # (ветка «Option зажат»), повторный дал бы второй текст.
        if verdict is not True and (verdict is False or not sent) and via != "type":
            try:
                kb.type(text)
                sent, via = True, "type"
                if readable:
                    v2 = _poll_insert(text, before)
                    if v2 is True:
                        verdict = True
                        print("[insert] вставка набором текста")
                    elif v2 is False:
                        verdict = False
                        reason = "поле не приняло ни вставку, ни посимвольный ввод"
            except Exception as e:
                reason = f"посимвольный ввод не прошёл ({type(e).__name__})"

        dt = time.time() - t_ins0
        if verdict is True:
            print(f"[insert success] '{text[:30]}...' ({via}, {dt:.2f}с)")
            if RESTORE_CLIPBOARD and clipboard_touched:
                _restore_clipboard_async(old_clipboard, text_bytes)
            return dt

        if verdict is None and sent:
            # Удар ушёл, проверить нечем — честно, без «success».
            # verdict=False при sent=True — это ДОКАЗАННЫЙ отказ → FAILED ниже.
            print(f"[insert sent] '{text[:30]}...' ({via}, {dt:.2f}с) — "
                  f"проверка недоступна ({app or '?'})")
            if RESTORE_CLIPBOARD and clipboard_touched:
                _restore_clipboard_async(old_clipboard, text_bytes)
            return dt

        # Провал подтверждён или отправить не удалось. Буфер НЕ восстанавливаем:
        # текст остаётся в нём как последний шанс (ручной Cmd+V).
        _put_clipboard(text_bytes)
        print(f"[insert FAILED] {reason or 'поле не подтвердило вставку'} — "
              f"текст в буфере, нажми Cmd+V ({dt:.2f}с)")
        _fail_sound()
        notify("WhisperKey — вставка не удалась",
               f"{reason or 'поле не подтвердило вставку'}. Текст в буфере — нажми Cmd+V")
        return dt

    except Exception as e:
        print(f"[insert error] {e}")
        notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Cmd+V")
        return time.time() - t_ins0

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

# ─── Страховка постобработки от потери слов (жалоба 19.10.26) ─────────────────
# «Шаг исправления» не должен терять смысловые слова: законны только чистка
# пробелов, водяные знаки и оформление. Считаем потерянные токены между сырой
# расшифровкой и финалом; необъяснённая потеря сверх порога — откат на сырую
# версию: грязный текст лучше обрезанного.

CLEANUP_LOSS_ABS = 3        # откат, если потеряно больше N смысловых слов…
CLEANUP_LOSS_RATIO = 0.10   # …и больше этой доли от сырого текста

def _word_tokens(s: str) -> list:
    return re.findall(r'\w+', s.lower())

def _diff_removed_text(before: str, after: str) -> str:
    """Слова, пропавшие при переходе before→after (по пословному diff)."""
    if not before or not after:
        return before or ""
    a, b = before.split(), after.split()
    na = [_norm_for_cmp(x) for x in a]
    nb = [_norm_for_cmp(x) for x in b]
    removed = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(a[i1:i2])
    return " ".join(removed)

def _verify_cleanup(raw_text: str, final_text: str, legal_removed: str = ""):
    """Потеряно ли сверх порога смысловых слов между raw и финалом.
    legal_removed — текст, который чистка обязана была вырезать (водяные
    знаки): он потерей не считается. Возвращает (текст_к_вставке, n_lost)."""
    if not raw_text:
        return final_text, 0
    lost = Counter(_word_tokens(raw_text)) - Counter(_word_tokens(final_text))
    if legal_removed:
        lost -= Counter(_word_tokens(legal_removed))
    n_lost = sum(lost.values())
    n_raw = len(_word_tokens(raw_text))
    if n_lost > CLEANUP_LOSS_ABS and n_lost > n_raw * CLEANUP_LOSS_RATIO:
        return raw_text, n_lost
    return final_text, n_lost

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

def _start_live_stream(session_id: int, initial_blocks: list) -> None:
    """Открывает поток на мост вместе с записью. Без моста/в режиме local —
    ничего не делает: тогда работает прежний путь целиком."""
    global _live_stream
    _live_stream = None
    url = _engine.ctx.remote_asr_url
    if not (STREAM_ENABLED and url) or _engine.ctx.engine_mode == "local":
        return
    try:
        # Своя сессия: сторож/reviver закрывают http_session при обрыве туннеля,
        # поток не должен падать от чужого close().
        ls = speech_engine.remote_engine.RemoteStream(
            url, session=requests.Session(), sample_rate=SAMPLE_RATE,
            token=_engine.ctx.remote_asr_token, ctx=_engine.ctx).start()
    except Exception as e:
        print(f"[stream] не стартовал: {type(e).__name__} — прежний путь")
        return
    for b in initial_blocks:      # предзапись, если включена
        ls.feed(b)
    _live_streams[session_id] = ls
    _live_stream = ls


def _end_live_stream(session_id: int, abort: bool = False) -> None:
    """Конец записи: коллбэк больше не кормит, тело запроса закрывается."""
    global _live_stream
    _live_stream = None
    ls = _live_streams.get(session_id)
    if ls is None:
        return
    if abort:
        _live_streams.pop(session_id, None)
        ls.abort()
    else:
        ls.close()


def _take_live_stream(session_id: int, abort: bool = False):
    ls = _live_streams.pop(session_id, None)
    if ls is not None and abort:
        ls.abort()
    return ls


def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, session_phase
    pill_error = False
    try:
        if not audio_snapshot:
            _take_live_stream(session_id, abort=True)
            return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5:
            _take_live_stream(session_id, abort=True)
            return

        # Тишина/чистый шум до ASR: случайное нажатие не едет на распознавание
        # и не оставляет ни текста, ни уведомления. Пик по фреймам, не среднее:
        # речь всплесками, а щелчок клавиши — один сэмпл — гейт не открывает.
        n_frames = len(audio) // 512
        if n_frames:
            peak_rms = float(np.sqrt(
                (audio[: n_frames * 512].reshape(n_frames, 512) ** 2).mean(axis=1)
            ).max())
            if peak_rms < 10.0 ** (SILENCE_GATE_DB / 20.0):
                peak_db = 20.0 * np.log10(peak_rms + 1e-9)
                print(f"[skip] тишина {dur:.1f}s, пик {peak_db:.0f} дБ — молча")
                _take_live_stream(session_id, abort=True)
                return

        schedule_eval_sample_collect(audio, dur, session_id)
        schedule_recording_collect(audio, dur, session_id)

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        # ─── Каскад распознавания (speech_engine, профиль dictation) ───────────
        # Deepgram → Groq (со всей обвязкой: гейт плотности, стилевой проход,
        # перенос знаков/терминов/форм) → локальная модель, по очереди. Устройство
        # каскада — ~/speech-engine/agent.md, не здесь.
        t_asr_start = time.time()
        result = None
        live = _take_live_stream(session_id)
        if live is not None:
            stext = live.result()   # потолок = read-таймаут моста, как у прежнего пути
            if stext:
                result = speech_engine.RecognitionResult(
                    text=stext, engine="stream", assembled_text=stext,
                    meta={"fallbacks": [f"мост: {fb}" for fb in live.fallbacks],
                          "stream_engine": live.engine})
                print(f"[stream] ответ моста через {time.time() - t_asr_start:.2f}с "
                      f"после конца записи (сервер: {live.engine})")
            else:
                print(f"[engine fallback] поток: {live.error or 'нет ответа'} — прежний путь")
        if result is None:
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
        elif engine_name == "stream":
            print(f"[raw stream] '{full_raw_text}'")
        elif full_raw_text:
            print(f"[engine] {engine_name} (запасной путь)")
        # Каждый переход на запасную ступень — видимой строкой, не молчанием.
        for fb in (result.meta.get("fallbacks") or []):
            print(f"[engine fallback] {fb}")

        if not full_raw_text:
            finalize_eval_sample_meta(session_id, dur, "", "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            pill_error = True
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
        # Что именно вырезала чистка (водяные знаки) — легальные потери,
        # верификатор их не считает.
        legal_removed = _diff_removed_text(full_raw_text, text)
        text = smart_grammar_fix(text)

        # Весь результат — субтитровая заглушка Whisper («Продолжение следует…»)
        # или пуст после чистки: на тишине/шуме модель так рисует «речь».
        # Молча гасим — ни вставки, ни баннера. Внутри живой фразы эти слова
        # не трогаем (boilerplate_only проверяет только текст целиком).
        if speech_engine.watermarks.boilerplate_only(text):
            finalize_eval_sample_meta(session_id, dur, full_raw_text, "")
            print(f"[skip] только заглушка ASR: '{text[:60]}' — молча")
            return

        # Страховка от «чистка съела фразу»: необъяснённая потеря смысловых
        # слов сверх порога → откат на распознанный текст.
        checked_text, n_lost = _verify_cleanup(full_raw_text, text, legal_removed)
        if n_lost:
            print(f"[cleanup] dropped {n_lost} tokens"
                  + (" — откат на исходный текст" if checked_text is not text else ""))
        text = checked_text

        if text and len(text) > 1:
            text = apply_smart_sentence_ending(text)

            print(f"\n--- ФИНАЛЬНЫЙ ТЕКСТ ---\n{text}\n-----------------------\n")

            last_text_context = text[-40:]
            # Распознавание закончено — гасим спиннер сразу, до вставки.
            # Иначе плашка крутит «загрузку» всё время опросов и добивок
            # в direct_insert (~1 с), хотя работа для человека уже сделана
            # (жалоба Егора 19.10.26: «плашка ещё загружается после текста»).
            with state_lock:
                if active_session_id == session_id:
                    pill.set_state("idle")
            ins_sec = direct_insert(text + " ") or 0.0
            # Раздельный замер: «стало дольше» почти всегда жило на стороне
            # вставки, а не ASR — путать их нельзя.
            print(f"[timing] asr={asr_sec:.2f}с insert={ins_sec:.2f}с "
                  f"всего={time.time() - t_start:.2f}с")
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
            elif not pill.active:
                suffix = f" (переспрошено окон: {n_retry})" if n_retry else ""
                notify("WhisperKey ✓", "Текст готов" + suffix)
        else:
            finalize_eval_sample_meta(session_id, dur, full_raw_text, "")
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            pill_error = True
    except Exception as e:
        # Этот except стоит после всех notify, поэтому раньше пользователь при
        # падении не получал НИЧЕГО — ни текста, ни уведомления, только строку
        # в терминале, куда он не смотрит.
        import traceback
        print(f"[error] {type(e).__name__}: {e}")
        traceback.print_exc()
        notify("WhisperKey — сбой", f"{type(e).__name__}. Текст не вставлен.")
        pill_error = True
    finally:
        # Нештатный выход до чтения потока — не оставлять его висеть.
        _take_live_stream(session_id, abort=True)
        processing = False
        with state_lock:
            if active_session_id == session_id:
                session_phase = "idle"
                # Под тем же замком, что и фаза: иначе новое нажатие между
                # двумя строками получило бы «запись», а мы бы тут же её затёрли.
                pill.set_state("error" if pill_error else "idle")
        # Диктовка закончена — заводим отсчёт до освобождения микрофона.
        # Новое нажатие таймер сбросит, так что при серии диктовок поток живёт.
        schedule_idle_close()

# ─── Обработка клавиш ─────────────────────────────────────────────────────────

# Журнал событий Option — разбор «почему тап-тап не залип» на живом маке:
# каждое нажатие/отпускание с миллисекундами и решением программы. Читается
# командой: cat ~/.whisperkey_taps.log | tail -40
LATCH_DEBUG_LOG = os.path.expanduser("~/.whisperkey_taps.log")

def _tlog(msg):
    try:
        with open(LATCH_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int((time.time() % 1) * 1000):03d} {msg}\n")
    except Exception:
        pass

def _fam(key):
    """Option-семейство для журнала: правый/левый/alt_gr и vk 58/61/62 — если
    второе касание приходит другим кодом, в журнале это будет видно."""
    if key is None:
        return False
    try:
        if getattr(key, "vk", None) in (58, 61, 62):
            return True
        return key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
                       getattr(keyboard.Key, "alt_gr", None))
    except Exception:
        return False

def is_trigger(key):
    if key == keyboard.Key.alt_r: return True
    try:
        if hasattr(key, 'vk') and key.vk == 61: return True
    except: pass
    return False

def _cancel_recording():
    """Отмена диктовки без распознавания — у Wispr третье быстрое касание в первые
    полсекунды залипания «cancels instead of pasting». Запись просто глушится."""
    global is_recording, session_phase
    with state_lock:
        if session_phase != "recording":
            return
        session_phase = "idle"
    is_recording = False
    _end_live_stream(active_session_id, abort=True)
    recording_data.clear()
    pill.set_state("idle")
    print("[rec] Отменено быстрым третьим касанием")
    if not pill.active:
        notify("WhisperKey", "✖️ Диктовка отменена")
    schedule_idle_close()


def _begin_stop():
    """Финализ записи — общий для двух путей: истекло окно двойного касания
    (обычный «нажал-отпустил») либо при залипании пришло завершающее нажатие."""
    global is_recording, processing, session_phase
    with state_lock:
        if session_phase != "recording":
            return
        current_session_id = active_session_id
        session_phase = "processing"
        # Хвост ещё секунду пишется, но человек клавишу уже отпустил —
        # плашка сразу переходит в «распознаю», как у Wispr.
        pill.set_state("processing")
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
        _end_live_stream(current_session_id)
        # При включённой предзаписи поток остаётся открытым — иначе следующее
        # нажатие снова начнётся с холодного старта и срежет первое слово.
        if not PREROLL_ENABLED:
            stop_audio_stream()

        audio_snapshot = list(recording_data)
        if len(audio_snapshot) < 10:
            # 0 фреймов — поток микрофона мёртв: поломка, её надо видеть.
            # 1-9 фреймов — случайное касание: глушим молча, как у Wispr —
            # пустая диктовка не оставляет ни текста, ни уведомления.
            print(f"[skip] Слишком коротко ({len(audio_snapshot)} фреймов)")
            _take_live_stream(current_session_id, abort=True)
            if not audio_snapshot:
                notify("WhisperKey", "⚠️ Слишком короткая запись")
            processing = False
            with state_lock:
                if active_session_id == current_session_id:
                    session_phase = "idle"
                    pill.set_state("idle")
            schedule_idle_close()
            return

        if not pill.active:
            notify("WhisperKey", "⏹ Распознаю...")
        print(f"[rec] Остановлена (хвост захвачен)")
        threading.Thread(target=process_audio, args=(audio_snapshot, current_session_id), daemon=True).start()

    processing = True
    threading.Thread(target=delayed_stop, daemon=True).start()


def _latch_window_then_stop(seq: int) -> None:
    """Отпускание ждёт окно двойного касания. Второе касание в этом окне обнуляет
    дедлайн — поток просто выходит, а запись продолжается без удержания."""
    global _latch_deadline
    deadline = _latch_deadline
    time.sleep(max(0.0, deadline - time.time()))
    with state_lock:
        if seq != _release_seq or _latch_deadline == 0.0:
            return
        _latch_deadline = 0.0
    _tlog("  → окно истекло, стоп")
    _begin_stop()


def _latch_watchdog(sid: int) -> None:
    """Страховка от «забыл залипшую запись включённой»: по потолку — обычный стоп."""
    time.sleep(LATCH_MAX_SEC)
    with state_lock:
        armed = latch_active and session_phase == "recording" and active_session_id == sid
    if armed:
        print(f"[rec] Залипание: потолок {LATCH_MAX_SEC // 60} мин — останавливаю")
        _tlog("  → сторож 15 мин, стоп")
        _begin_stop()


def on_press(key):
    global is_recording, recording_data, processing, trigger_held, last_trigger_ts, session_counter, active_session_id, session_phase
    global latch_active, _latch_deadline, _latch_started_ts, _last_release_ts, _release_seq
    global _opt_down
    now = time.time()
    if _fam(key):
        _opt_down = True
        _tlog(f"P {key} trig={is_trigger(key)} held={trigger_held} ph={session_phase} "
              f"latch={int(latch_active)} win={_latch_deadline - now:+.3f} "
              f"postRel={now - _last_release_ts:.3f}")
    if is_trigger(key) and trigger_held and session_phase == "recording":
        # Нажатие при «ещё нажатой» клавише: физически её успели отпустить,
        # иначе press не пришёл бы — значит, событие отпускания потерялось по
        # дороге (macOS может сливать быстрые переключения модификатора).
        # Считаем отпускание случившимся прямо перед нажатием — дальше общий
        # путь второго касания: окно залипания уже открыто.
        _tlog("  → press при held: отпускание потерялось, считаю случившимся")
        trigger_held = False
        with state_lock:
            _last_release_ts = now
            _release_seq += 1
            if not latch_active:
                # Дедлайн только когда залипание ещё не включено — иначе
                # протухшее окно переживёт эту запись и ложно поймает следующий тап.
                _latch_deadline = now + LATCH_WINDOW_SEC
    if is_trigger(key) and not trigger_held:
        # Сначала — режимы, которые не стартуют новую запись и потому идут до
        # дебаунса: второй тап приходит раньше дебаунс-порога и съедается им.
        with state_lock:
            if latch_active:
                # «Залипшая» диктовка заканчивается нажатием — как одиночное
                # касание у Wispr, а не отпусканием. Но если нажатие пришло в
                # первые 0.5 с залипания — это у Wispr отмена без вставки текста.
                latch_active = False
                trigger_held = True
                end_by_press = True
                cancel_by_press = (now - _latch_started_ts) < LATCH_CANCEL_SEC
            else:
                end_by_press = False
                cancel_by_press = False
            if (not end_by_press and session_phase == "recording"
                    and now < _latch_deadline):
                # Второе касание в окне после отпускания — запись «залипает».
                latch_active = True
                _latch_started_ts = now
                _latch_deadline = 0.0
                trigger_held = True
                last_trigger_ts = now
                armed_sid = active_session_id
                armed = True
            else:
                armed = False
                if (not end_by_press and session_phase == "recording"
                        and _last_release_ts > 0):
                    # Касание при живой записи, но мимо окна залипания — пишем,
                    # чтобы по журналу было видно, почему тап-тап не сработал.
                    print(f"[rec] касание мимо окна залипания "
                          f"(после отпускания {int((now - _last_release_ts) * 1000)} мс, "
                          f"до конца окна {int((_latch_deadline - now) * 1000)} мс)")
                    _tlog("  → касание мимо окна")
        if end_by_press:
            _tlog("  → стоп залипания нажатием" + (" (отмена)" if cancel_by_press else ""))
            if cancel_by_press:
                _cancel_recording()
            else:
                _begin_stop()
            return
        if armed:
            _tlog("  → LATCH")
            print("[rec] Залипание: запись без удержания — Option завершит")
            threading.Thread(target=_latch_watchdog, args=(armed_sid,), daemon=True).start()
            return
        if now - last_trigger_ts < TRIGGER_DEBOUNCE_SEC:
            _tlog("  → дебаунс, пропуск")
            return
        with state_lock:
            if session_phase != "idle":
                _tlog(f"  → пропуск, фаза {session_phase}")
                return
            last_trigger_ts = now
            trigger_held = True
            session_counter += 1
            active_session_id = session_counter
            session_phase = "recording"
            _tlog("  → старт записи")
            try:
                if PREROLL_ENABLED:
                    cancel_idle_close()
                    # Поток мог закрыться по простою или умереть от смены устройства.
                    if not ensure_audio_stream():
                        session_phase = "idle"
                        pill.set_state("error")
                        return
                    # Порядок важен: буфер заполняется ДО поднятия is_recording,
                    # иначе колбэк успевает дописать в старый список.
                    recording_data = list(preroll_buffer)
                    preroll_buffer.clear()
                else:
                    if not start_audio_stream():
                        session_phase = "idle"
                        pill.set_state("error")
                        return
                    recording_data = []

                audio_dropouts.clear()
                _start_live_stream(active_session_id, recording_data)
                is_recording = True
                # Уведомление ПОСЛЕ фактического начала захвата: раньше «говори»
                # выдавалось до того, как микрофон отдавал первый сэмпл.
                pill.set_state("recording")
                if not pill.active:
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
                _end_live_stream(active_session_id, abort=True)
                session_phase = "idle"
                pill.set_state("error")

def on_release(key):
    global is_recording, processing, trigger_held, session_counter, session_phase, _latch_deadline, _last_release_ts, _release_seq
    global _opt_down
    if _fam(key):
        _opt_down = False
        _tlog(f"R {key} trig={is_trigger(key)} latch={int(latch_active)} "
              f"rec={int(is_recording)} ph={session_phase}")
    if is_trigger(key):
        trigger_held = False
        if latch_active or not is_recording:
            # Залипшая запись заканчивается нажатием (в on_press), отпускание
            # здесь просто игнорируется — как у Wispr.
            return
        with state_lock:
            if session_phase != "recording":
                return
            # Окно двойного касания — от ОТПУСКАНИЯ короткого тапа (0.7 с):
            # второе касание в нём «залипает». Клавишу держали дольше
            # LATCH_TAP_MAX_SEC — это уже «нажал-держи-говори», стоп мгновенный,
            # без всякой задержки и без окна.
            _last_release_ts = time.time()
            _release_seq += 1
            seq = _release_seq
            hold = _last_release_ts - last_trigger_ts
            if hold >= LATCH_TAP_MAX_SEC:
                _tlog(f"  → стоп сразу (держали {hold:.3f} с)")
                finalize_now = True
            else:
                _tlog(f"  → окно залипания {LATCH_WINDOW_SEC} с (тап {hold:.3f} с)")
                _latch_deadline = _last_release_ts + LATCH_WINDOW_SEC
                finalize_now = False
                print(f"[rec] тап {int(hold * 1000)} мс — жду второе касание "
                      f"{int(LATCH_WINDOW_SEC * 1000)} мс")
        if finalize_now:
            _begin_stop()
        else:
            threading.Thread(target=_latch_window_then_stop, args=(seq,), daemon=True).start()

# ─── Серверный мост: сторож туннеля (19.10.26) ────────────────────────────────
# Туннель поднимал только лаунчер и только один раз: ssh без перезапуска умирал
# от любого разрыва сети, и мост был мёртв до перезапуска WhisperKey — а при
# запуске напрямую python3 его не было вовсе. Жалоба «без VPN не работает»
# именно про это: без VPN мост — единственный живой путь распознавания.
# Теперь сторож раз в BRIDGE_CHECK_SEC проверяет /health и переподнимает
# туннель сам. Работает только на macOS: на сервере мост и так локальный.

def _bridge_host() -> str:
    # Адрес сервера — личный, в репозитории не хранится:
    # env WHISPERKEY_BRIDGE_HOST или файл ~/.whisperkey_bridge_host
    h = os.environ.get("WHISPERKEY_BRIDGE_HOST", "").strip()
    if not h:
        try:
            h = open(os.path.expanduser("~/.whisperkey_bridge_host")).read().strip()
        except OSError:
            h = ""
    return h

BRIDGE_HOST = _bridge_host()
BRIDGE_PID_FILE = "/tmp/whisperkey_tunnel.pid"
BRIDGE_ASR_URL = "http://127.0.0.1:8092/audio/transcriptions"
BRIDGE_CHECK_SEC = 15

def _bridge_health_url() -> str:
    url = _engine.ctx.remote_asr_url or BRIDGE_ASR_URL
    return url.rsplit("/", 2)[0] + "/health"

def _bridge_alive() -> bool:
    try:
        return http_session.get(_bridge_health_url(), timeout=3.0).status_code == 200
    except Exception:
        return False

def _kill_stale_tunnel() -> None:
    """Гасит висючий ssh-туннель строго по сохранённому PID — и только если это
    действительно наш туннель (PID за время мог переиспользоваться)."""
    try:
        pid = int(open(BRIDGE_PID_FILE).read().strip())
    except Exception:
        return
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=3).stdout
        if "8092:127.0.0.1:8092" not in out:
            return
        os.kill(pid, 15)
    except Exception:
        pass

_tunnel_lock = threading.Lock()


def _spawn_tunnel() -> bool:
    """Поднимает ssh-туннель и ждёт /health. True — мост доступен.
    Под lock: одновременный вызов из сторожа и из _revive_bridge (упавший
    запрос посреди диктовки) не должен плодить два ssh-процесса."""
    with _tunnel_lock:
        return _spawn_tunnel_locked()


def _spawn_tunnel_locked() -> bool:
    if not BRIDGE_HOST:
        return False  # адрес сервера не задан — прямой путь через Deepgram
    _kill_stale_tunnel()
    try:
        p = subprocess.Popen(
            ["ssh", "-N", "-L", "8092:127.0.0.1:8092",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=1",
             BRIDGE_HOST],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with open(BRIDGE_PID_FILE, "w") as f:
                f.write(str(p.pid))
        except Exception:
            pass
    except Exception as e:
        print(f"[bridge] туннель не поднялся: {e}")
        return False
    for _ in range(5):
        time.sleep(0.7)
        if _bridge_alive():
            return True
    return False

# При реальном длительном обрыве сети reviver не должен плодить ssh на каждую
# диктовку: после неудачного подъёма отдыхаем BRIDGE_REVIVE_REST_SEC — туннелем
# в это время занимается сторож.
BRIDGE_REVIVE_REST_SEC = 15
_last_revive_fail = [0.0]

def _revive_bridge() -> bool:
    """Переподнимает туннель СРАЗУ, не дожидаясь тика сторожа (15с).
    Вызывается каскадом (ctx.remote_reviver), когда remote-запрос упал посреди
    диктовки или адрес снят: обрыв домашнего интернета без этого отправлял
    запись в локальную модель на ~50с (живой журнал 22.10.26), хотя туннель
    восстанавливался за секунды."""
    # Живой туннель — само по себе доказательство сети: установленный ssh
    # переживает краткий флап лучше, чем свежий TCP-connect общей пробы,
    # и ждать её здесь не нужно.
    if _bridge_alive():
        if not _engine.ctx.remote_asr_url:
            _engine.ctx.remote_asr_url = BRIDGE_ASR_URL
        return True
    # Интернета нет совсем — мост на сервере недостижим по определению;
    # подъём ssh + 5 проверок /health стоили до ~18с перед local (журнал 22.10.26).
    if internet_down():
        # _last_revive_fail НЕ ставим: иначе ещё 15с после возврата сети
        # reviver отказывал бы по таймеру отдыха; частоту держит кэш пробы.
        print("[bridge] интернета нет — туннель не поднимаю, сразу дальше")
        return False
    if time.time() - _last_revive_fail[0] < BRIDGE_REVIVE_REST_SEC:
        return False
    print("[bridge] запрос упал в обрыв — поднимаю туннель сразу")
    if _spawn_tunnel():
        _engine.ctx.remote_asr_url = BRIDGE_ASR_URL
        try:
            http_session.close()   # в пуле лежит мёртвый сокет старого туннеля
        except Exception:
            pass
        print("[bridge] туннель поднят — повторяю запрос")
        return True
    _last_revive_fail[0] = time.time()
    return False

def _bridge_watchdog() -> None:
    while True:
        if _bridge_alive():
            if not _engine.ctx.remote_asr_url:
                _engine.ctx.remote_asr_url = BRIDGE_ASR_URL
                print("[bridge] серверный мост подключён — резерв через сервер")
        else:
            if _engine.ctx.remote_asr_url:
                _engine.ctx.remote_asr_url = ""
                print("[bridge] мост не отвечает — переподнимаю туннель")
            if _spawn_tunnel():
                _engine.ctx.remote_asr_url = BRIDGE_ASR_URL
                print("[bridge] туннель поднят — мост снова доступен")
        time.sleep(BRIDGE_CHECK_SEC)

def _probe_asr_paths() -> None:
    """Разовая проверка доступности облачных ступеней на старте.

    Без VPN Deepgram с домашнего IP недоступен (проверено поведением 19.10.26),
    а Groq в гео-блоке 403 — и без пробы каждая диктовка платила их таймаутами
    (~10-20с на Deepgram + ретраи Groq), прежде чем каскад доходил до моста.
    Мёртвый путь помечается сетевым блоком сразу — дальше каскад идёт мимо него.
    """
    try:
        http_session.head("https://api.deepgram.com/", timeout=3.0)
    except Exception as e:
        from speech_engine.deepgram_engine import NETWORK_BLOCK_SECONDS
        st = _engine.ctx.deepgram_state
        st.blocked_until = time.time() + NETWORK_BLOCK_SECONDS
        st.blocked_by_network = True
        st.last_reason = f"нет связи ({type(e).__name__})"
        print(f"[engine fallback] deepgram: нет связи при старте ({type(e).__name__}) "
              f"— ступень выкл на {int(NETWORK_BLOCK_SECONDS)}с")
    if GROQ_API_KEY and GROQ_API_KEY != "YOUR_GROQ_API_KEY_HERE":
        try:
            from speech_engine.groq_engine import background_cloud_probe
            background_cloud_probe(api_key=GROQ_API_KEY,
                                   state=_engine.ctx.cloud_state, session=http_session)
        except Exception:
            pass

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

def _warm_local_model(local_model):
    try:
        segments, _ = local_model.transcribe(
            np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)
        for _ in segments:
            pass
    except Exception as e:
        print(f"[local] прогрев не выполнен ({type(e).__name__}); запуск продолжается")
        return False
    return True


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
        # cpu_threads 2→4: на Mac ядер больше, потоки на качество не влияют,
        # только на скорость резервного пути. beam_size не трогаем — beam=1
        # измерено хуже: +4 п.п. ошибок на эталоне Salute (22.10.26).
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=False)
        _warm_local_model(model)
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

    try:
        ver = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        ver = "nogit"
    _tlog(f"=== запуск, сборка {ver} ===")

    # Версия — в health моста: в журнале voice-bridge на сервере видно, какая
    # сборка реально стартовала. Работает и со старым лаунчером — главное,
    # чтобы был жив туннель (REMOTE_ASR_URL ставится лаунчером ещё с 04.10).
    if os.environ.get("REMOTE_ASR_URL"):
        def _ping_bridge_version():
            try:
                http_session.get(os.environ["REMOTE_ASR_URL"].rsplit("/", 2)[0] + "/health",
                                 params={"v": ver}, timeout=2)
            except Exception:
                pass
        threading.Thread(target=_ping_bridge_version, daemon=True).start()

    # Проба доступности облачных ступеней + сторож туннеля моста (см. выше).
    threading.Thread(target=_probe_asr_paths, daemon=True).start()
    if _IS_MAC and not os.environ.get("WHISPERKEY_NO_TUNNEL"):
        _engine.ctx.remote_reviver = _revive_bridge
        threading.Thread(target=_bridge_watchdog, daemon=True).start()

    if _engine.ctx.engine_mode != "auto":
        print(f"Режим движков: {_engine.ctx.engine_mode} (с учётом настроек и платформы)")
    if _engine.ctx.yandex_api_key:
        print("Резерв без VPN: Yandex SpeechKit подключён")

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    notify("WhisperKey", f"Готов к работе! (v{ver})")

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    # Окна macOS живут только в главном потоке, поэтому он теперь крутит цикл
    # событий плашки, а клавиатура слушается в своём потоке, как и раньше.
    # Плашка не поднялась (или её нет) — главный поток просто ждёт клавиатуру.
    try:
        pill.run_forever(on_fail=listener.join)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()

if __name__ == "__main__":
    main()
