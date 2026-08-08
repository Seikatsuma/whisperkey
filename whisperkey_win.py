#!/usr/bin/env python3
"""
WhisperKey v24 — Windows Edition. Дословная диктовка под правый Alt.

Логика распознавания синхронизирована с whisperkey.py (macOS) — он эталон,
и причины всех решений с цифрами замеров лежат в agent.md. Расходиться с ним
можно ровно в семи платформенных точках, и они помечены в коде:
  1. однократный запуск — именованный мьютекс вместо fcntl.flock;
  2. уведомления — консоль плюс системный звук вместо osascript-баннера;
  3. вставка текста — буфер обмена плюс SendInput вместо AppleScript;
  4. путь к Рабочему столу — реестр вместо ~/Desktop (OneDrive);
  5. приоритет процесса — класс приоритета вместо nice;
  6. кодировка stdout — иначе русский print убивает программу;
  7. поведение клавиши Alt — одиночный Alt на Windows открывает меню окна.
Всё остальное (текстовые фильтры, разбор ответа модели, нарезка, склейка,
сеть) переносится из эталона дословно — там оно доказано замерами.
"""
from __future__ import annotations

import ctypes
import io
import os
import re
import sys
import threading
import time
import wave
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

# ─── Консоль Windows (платформенная точка 6) ──────────────────────────────────
# Делается ДО первого print и до импортов, которые печатают.
#
# На Windows кодировка вывода — не косметика, а живучесть программы. Три отказа,
# ни одного из которых нет на macOS:
#   1. вывод перенаправлен в файл или пайп (запуск из планировщика, из IDE,
#      из обёртки) — Python берёт системную cp1251, и первый же русский print
#      бросает UnicodeEncodeError. Печать у нас идёт в том числе из колбэка
#      клавиатурного хука; исключение оттуда pynput ловит в _emitter, ОСТАНАВЛИВАЕТ
#      listener и перебрасывает из join() — клавиша умирает до перезапуска;
#   2. запуск под pythonw.exe — sys.stdout вообще None, print падает с
#      AttributeError по тому же маршруту;
#   3. QuickEdit в cmd.exe включён по умолчанию: случайное выделение мышью
#      в окне консоли замораживает запись в stdout навсегда. Уведомления
#      печатаются из отдельного потока (см. notify), но лишний раз рисковать
#      незачем — снимаем режим сразу.
def _setup_console() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        # Кодовая страница консоли: cmd.exe стартует в cp866, и кириллица
        # в ней нечитаема независимо от кодировки самого Python.
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

    try:
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)          # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_EXTENDED_FLAGS (0x0080) обязателен: без него система
            # вернёт снятый бит QUICK_EDIT_MODE (0x0040) обратно.
            k32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


_setup_console()

import numpy as np
import psutil
import requests
import pyperclip

try:
    import winsound            # stdlib, есть только на Windows
except Exception:
    winsound = None

try:
    import sounddevice as sd
except OSError as e:
    print("\n" + "!"*60)
    print(" ОШИБКА: Библиотека PortAudio не найдена.")
    print(" Пожалуйста, убедитесь, что все зависимости установлены корректно.")
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_PATH  = "small"          # запасная локальная модель, если облако недоступно
TAIL_CAPTURE_SECONDS = 1.0  # Захват хвоста после отпускания клавиши.
# Поднято с 0.6 до 1.0 (07.08.26) по жалобе: последнее слово не попадает
# в запись, хотя клавиша отпущена позже. Прежние 0.6 с ставились ради
# скорости, но диктовка идёт в облако 2-4 секунды, и лишние 0.4 с на фоне
# этого не заметны, а потерянное слово заметно всегда.
# Проверить это замером нельзя: корпус эталонов содержит УЖЕ записанный
# звук, и то, что не попало в файл, в нём отсутствует у всех одинаково.
# Проверка — на калибровочном тексте, где известно, что было сказано.
RESTORE_CLIPBOARD = True
SAVE_DEBUG_AUDIO = False       # без записи WAV на диск

# Браузерный User-Agent — строка ДЛЯ WINDOWS. На macOS в эталоне стоит
# "Macintosh", и копировать её сюда нельзя: вся ветка cloud_status["is_blocked"]
# построена вокруг ответа 403 (гео-блок Groq), а несовпадение UA с платформой
# этот шанс только повышает. Без UA отказ приходит чаще, и диктовка молча
# уходит на слабую локальную модель small.
WIN_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# Промпт задаёт модели ТОЛЬКО образец пунктуации — ни темы, ни терминов, ни имён
# собственных. Тематическая подсказка (здесь раньше стояла «Русская деловая речь.
# IT, программирование, технические задачи.») опасна: она протекает в текст там,
# где речь не разобрана (замер: "Субтитры делал DimaTorzok" — 3 слова вместо 74 живых).
#
# Замер 05.08.26 на 10 окнах реального разговора (2 независимые выборки по 5):
#   промпт                    слов    точек   запятых
#   пусто                      996        5        11   (текст сплошным потоком)
#   тематический (прежний)     975       97       139
#   этот                      1033      105       159
# То есть образец пунктуации без содержания даёт И больше текста (+5.9% к прежнему,
# +3.7% к пустому), И больше знаков препинания. Галлюцинаций — 0 на всех 10 окнах.
# ОБНОВЛЕНО 06.08.26 — промпт убран совсем, синхронно с mac-версией.
# Прежний замер делался на 45-секундных окнах разговора, и переносить его
# на короткую диктовку было нельзя. Замер на 8-секундных клипах, 6 окон:
# 80 слов с промптом против 112 без него (-29%), причём одна фраза целиком
# схлопнулась в «Да.» — слово из самого промпта. Проверка по длительности
# дала не зависимость, а разброс (8 с -29%, 15 с +9%, 25 с -8%, 40 с +21%),
# то есть промпт — лотерея, и проигрыш означает потерю всей фразы.
# Заглавную букву, единственную его пользу, ставит CAPITALIZE_FIRST.
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
STYLE_PROMPT = ("Так, ну, в общем, смотри. Значит, вот. И, соответственно, дальше. "
                "Claude Code, Claude Design, WhisperKey, Notebook LM, GitHub, Telegram, "
                "Groq, DeepGram, CEO, API, Яндекс Маркет, Ozon, Wildberries, iPad, "
                "промпт, токены, агенты, скиллы. "
                # Команды в повелительном наклонении — самый частый класс ошибок:
                # модель слышит звук верно, но выбирает форму из субтитровой привычки
                # («сохрани» -> «сохраняет», «реализуй» -> «реализует»). Образцы
                # императивов в промпте смещают выбор формы: 34 верных команды
                # из 51 без них против 40 с ними (замер 08.08.26 на калибровке).
                "Сохрани. Раздели. Выстрой. Реализуй. Проверь. Покажи. Открой. "
                "Запусти. Найди. Убери. Оставь. Пришли. Собери. Напиши. Сделай. "
                "Отметь. Скинь. Запиши. Продолжай. Добавляй.")

# Белый список: ТОЛЬКО эти слова могут приехать из второго прохода в текст.
# Промпт заставляет модель писать названия латиницей, но он же съедает слова
# (замер: 613 слов без словаря против 542 со словарём). Поэтому слова берутся
# из основного прохода, а из словарного переносятся исключительно термины отсюда.
# Слово, которого нет в этом списке, попасть в результат физически не может.
TERM_CANON = {
    "claude": "Claude", "code": "Code", "design": "Design",
    "whisperkey": "WhisperKey", "notebook": "Notebook", "lm": "LM",
    "github": "GitHub", "telegram": "Telegram", "groq": "Groq",
    "deepgram": "DeepGram", "ceo": "CEO", "ipad": "iPad",
    "ozon": "Ozon", "wildberries": "Wildberries",
}

# Темп записи, отправляемой в облако. Обоснование и замеры — в create_audio_wav.
# 1.0 возвращает прежнее поведение.
ASR_TEMPO = 1.03

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
# Прежний порог здесь был 30 секунд, то есть на Windows резалась практически
# каждая длинная диктовка, причём БЕЗ перекрытия — слова на швах терялись
# безвозвратно, склеить их обратно было нечем.
CHUNK_THRESHOLD_SECONDS = 900.0
CHUNK_SIZE_SECONDS = 60.0
CHUNK_OVERLAP_SECONDS = 3.0

# Детектор провала распознавания. no_speech_prob его НЕ ловит (у пустых сегментов
# замерено 0.028 и 0.581), плотность слов ловит 3 из 3 и 4 из 4 в двух проверках.
DENSITY_GATE_MIN_DURATION = 8.0
DENSITY_GATE_MIN_WORDS_PER_SEC = 0.5

# "Продолжение следует" отсюда убрано вместе с BOH_TAIL_MARKERS: это нормальная
# русская фраза, а попадание в этот список включает принудительную чистку куска,
# после которой кусок короче трёх слов выбрасывается целиком.
HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "голос за кадром",
]

# API Настройки (Groq Cloud)
# Путь абсолютный от самого файла: с относительным ключ не находился при запуске
# не через run_whisperkey.bat (из Проводника, из ярлыка с другой рабочей папкой,
# из планировщика), и вся диктовка молча уходила на слабую локальную модель —
# пользователь видел только резко упавшее качество, без единого сообщения.
load_env_file(os.path.join(BASE_DIR, ".env"))
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
# Замер 08.08.26 на калибровке Егора. Эталон настоящий: текст, который он читал
# вслух с листа, — не вывод другой машины, поэтому согласованные ошибки двух
# движков не могут выдать себя за правильные слова. 808 слов эталона:
#     боевой конвейер на whisper        78.6%   173 ошибки
#     Deepgram nova-2 + таблица имён    86.5%   109 ошибок
# Он же и быстрее: 369 секунд аудио — 5.5 с против 8.9 с у Groq, на пятиминутной
# записи 1.6 с против 5.6 с. То есть вторая ступень не только не нужна для
# качества — она и по времени проигрывает.
#
# Модель именно nova-2, хотя nova-3 новее: третья версия обучена прежде всего
# под английский, русский идёт у неё через multilingual и стоит пяти пунктов
# (80.9% против 86.1% на том же материале). Проверено обеими.
#
# smart_format выключен намеренно: он переписывает «двадцать пять» в «25»
# и «пятое августа» в «05.08», а Егор просил дословность — «не форматируй».
# На замере он стоит 2.8 пункта (83.3% против 86.1%). filler_words наоборот
# включён: «ну», «вот», «значит» — часть его речи, а не мусор.
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-2"
DEEPGRAM_PARAMS = {
    "model": DEEPGRAM_MODEL,
    "language": "ru",
    "punctuate": "true",
    "smart_format": "false",
    "filler_words": "true",
    "numerals": "false",
}
DEEPGRAM_ENABLED = bool(DEEPGRAM_API_KEY)
DEEPGRAM_TIMEOUT = 20.0
DEEPGRAM_RETRIES = 2
DEEPGRAM_BLOCK_SECONDS = 900.0

# Создаем глобальную сессию для Keep-Alive
http_session = requests.Session()

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False
last_text_context = ""
global_audio_buffer = []
trigger_held = False
last_trigger_ts = 0.0

# Предзапись. Поток микрофона держится открытым, и последние PREROLL_SECONDS
# всегда лежат в кольце — при нажатии клавиши они уходят в запись вместе с новым
# звуком. Это единственное настоящее лекарство от срезанного первого слова:
# PortAudio не отдаёт ни одного сэмпла, пока поднимается, а уведомление «говори»
# выдавалось ещё раньше. На Windows выигрыш должен быть БОЛЬШЕ, чем на Mac:
# прежняя версия открывала устройство прямо в колбэке клавиатурного хука, а под
# MME/WASAPI открытие занимает сотни миллисекунд.
#
# Поток НЕ висит вечно: после PREROLL_IDLE_TIMEOUT секунд без диктовки он
# закрывается сам. Вечно открытый поток вреден не нагрузкой (32 раза в секунду
# по 2 КБ — доли процента ядра), а тем, что молча умирает при смене
# аудиоустройства. На Windows это случается чаще, чем на Mac: USB-гарнитура,
# Bluetooth, переключение «устройства связи». Поэтому ensure_audio_stream
# проверяет stream.active, а не наличие объекта.
#
# ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА — так же, как в эталоне. Причина отключения на macOS
# (CoreAudio отвечал -10863 kAudioUnitErr_CannotDoInCurrentContext, поток умирал,
# предзапись обнулялась, диктовка уходила в никуда) к Windows не относится:
# такого кода ошибки здесь нет. Но выигрыш предзаписи НЕ ИЗМЕРЕН ни на одной
# платформе, а проверить его на Windows-машине отсюда невозможно. Включать
# True — только вместе с проверкой руками: в логе при старте записи должно быть
# «предзапись 500 мс», а не «предзапись пуста».
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
audio_stream = None

# CEO Cloud Management: Динамическое управление состоянием облака
cloud_status = {
    "is_blocked": False,
    "last_check_time": 0,
    "check_in_progress": False,
    "consecutive_success": 0,
    "last_degenerate": [],     # окна, где модель сорвалась (гейт плотности)
    "last_api_text": "",       # собственный текст модели до нашей сборки
}
deepgram_status = {
    "blocked_until": 0.0,
    "last_reason": "",
    "last_seconds": 0.0,
}
kb = KeyboardController()
_instance_lock_handle = None
_mutex_handle = None


# ─── Однократный запуск (платформенная точка 1) ───────────────────────────────

def acquire_single_instance_lock() -> bool:
    """Гарантирует один активный процесс WhisperKey.

    Именованный мьютекс, а не pid-файл. fcntl.flock из эталона даёт два свойства,
    которых у pid-файла нет ни одного: атомарность (проверка и захват — одна
    операция) и освобождение операционной системой при смерти процесса, включая
    kill. Прежний pid-файл ломался дважды: Windows агрессивно переиспользует
    PID, и чужой процесс с тем же номером после перезагрузки заставлял WhisperKey
    навсегда отказываться стартовать «уже запущен» — лечилось только удалением
    файла руками; плюс путь был относительным, поэтому второй экземпляр,
    запущенный из другого каталога, замка просто не видел.
    Префикс Local\\, а не Global\\: Global требует привилегии, которой
    у обычного пользователя нет.
    """
    global _mutex_handle
    try:
        # use_last_error=True обязателен: без него ctypes не сохраняет код
        # ошибки потока, и отдельный вызов GetLastError() может вернуть чужой
        # результат — то есть замок иногда «срабатывал» бы на пустом месте.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p   # HANDLE, а не int: на x64 int обрежет
        _mutex_handle = k32.CreateMutexW(None, False, "Local\\WhisperKey")
        if not _mutex_handle:
            return True                              # мьютекс не создался — не мешаем работать
        return ctypes.get_last_error() != 183         # ERROR_ALREADY_EXISTS
    except Exception:
        return _acquire_lock_by_pidfile()


def _acquire_lock_by_pidfile() -> bool:
    """Запасной замок, если WinAPI недоступен. Путь абсолютный от файла проекта."""
    global _instance_lock_handle
    lock_path = os.path.join(BASE_DIR, "whisperkey.lock")
    try:
        if os.path.exists(lock_path):
            try:
                with open(lock_path, "r") as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    return False
            except Exception:
                pass
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


# ─── Микрофон ─────────────────────────────────────────────────────────────────

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
    """Открывает поток захвата. Возвращает True/False.

    Раньше исключение глоталось здесь же и функция ничего не возвращала, поэтому
    is_recording вставал в True при мёртвом потоке, а отказ микрофона доходил
    до пользователя как «Слишком короткая запись».
    """
    global audio_stream
    if audio_stream:
        try:
            audio_stream.stop()
            audio_stream.close()
        except Exception:
            pass
        audio_stream = None

    # Пара повторов дешевле сорванной диктовки: устройство бывает занято
    # ровно в момент открытия — только что подключилась USB-гарнитура,
    # переключилось «устройство связи», Bluetooth перехватил профиль.
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

def _start_capture_async(session_id: int) -> None:
    """Поднимает микрофон ВНЕ потока клавиатурного хука (платформенная точка 7).

    Windows-специфика, аналога которой на macOS нет. pynput ставит
    WH_KEYBOARD_LL и качает очередь сообщений в одном и том же потоке: сам
    хук только кладёт событие в очередь, а on_press вызывается уже из цикла
    сообщений — то есть пока on_press работает, поток НЕ качает очередь и хук
    позвать нельзя. Если это длится дольше LowLevelHooksTimeout (по умолчанию
    300 мс), Windows 7 и новее СНИМАЕТ хук молча — клавиша перестаёт работать
    до перезапуска программы, без единого сообщения.

    Открытие устройства под MME стоит сотни миллисекунд, а start_audio_stream
    делает до трёх попыток с паузами 0.25 и 0.5 с — то есть до полутора секунд
    прямо в этом потоке. Поэтому здесь только запуск: захват начнётся, как
    только поток поднимется, а is_recording поднят заранее и колбэк ничего
    не потеряет (до открытия его просто никто не зовёт).

    Уведомление «Запись…» уезжает сюда же и намеренно: оно должно приходить
    не раньше, чем микрофон начал отдавать сэмплы, иначе пользователь начинает
    говорить в ещё не открытое устройство и теряет первое слово — ровно та
    болезнь, от которой заводили предзапись.

    Если микрофон не открылся — сессия снимается, но только если её не успел
    забрать on_release: тогда фазу «processing» доводит delayed_stop.
    """
    def run():
        global is_recording, session_phase
        if start_audio_stream():
            with state_lock:
                stale = (active_session_id != session_id
                         or session_phase != "recording")
            if stale:
                # Клавишу отпустили быстрее, чем открылось устройство: стоп
                # в delayed_stop прошёл по ещё пустому audio_stream, и без
                # этой ветки микрофон остался бы занят до следующей диктовки
                # (горящий индикатор записи в трее и у камеры).
                print("[audio] Клавиша отпущена раньше, чем открылся микрофон")
                if not PREROLL_ENABLED:
                    stop_audio_stream()
                return
            notify("WhisperKey", "Запись...")
            print("[rec] Начата")
            return
        is_recording = False
        with state_lock:
            if active_session_id == session_id and session_phase == "recording":
                session_phase = "idle"
        schedule_idle_close()

    threading.Thread(target=run, daemon=True).start()

_idle_timer: threading.Timer | None = None
_idle_timer_lock = threading.Lock()

def ensure_audio_stream() -> bool:
    """Гарантирует живой поток захвата.

    Поток умирает при смене аудиоустройства, причём молча: объект остаётся,
    а сэмплы не идут. Поэтому проверяем не наличие объекта, а его активность.
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
    """Безопасное отключение микрофона без блокировки основного потока."""
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
    """Фоновая проверка доступности облака с подтверждением стабильности."""
    global cloud_status
    if cloud_status["check_in_progress"]: return

    def probe():
        cloud_status["check_in_progress"] = True
        try:
            headers = {
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'User-Agent': WIN_USER_AGENT,
            }
            response = requests.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=5)
            if response.status_code == 200:
                cloud_status["consecutive_success"] += 1
                if cloud_status["consecutive_success"] >= 1:
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

# ─── Уведомления (платформенная точка 2) ──────────────────────────────────────

NOTIFY_SOUND = True

def notify(title: str, message: str):
    """Уведомление на Windows: строка в консоль плюс системный звук.

    Асинхронно — по двум причинам, обеих на macOS нет. notify() зовётся изнутри
    state_lock и изнутри колбэка низкоуровневого хука клавиатуры: синхронный
    print, застрявший на выделении мышью в консоли (QuickEdit), заморозил бы
    всё приложение вместе с захваченным локом, а UnicodeEncodeError из хука
    останавливает listener pynput — клавиша умирает до перезапуска.

    Звук, а не только печать: консоль стоит за активным окном, а пользователь
    в этот момент смотрит в поле ввода. Без звука уход на локальную модель,
    вырезанный водяной знак и потерянный кусок остаются незамеченными — то есть
    вся работа по видимости деградаций пропадает. Тост через PowerShell
    сознательно НЕ используется: внешняя зависимость, 300-800 мс и мигание
    окном на каждое уведомление прямо в горячем пути клавиши.
    """
    def run_notify():
        try:
            print(f"\n>>> {title}: {message}")
        except Exception:
            pass
        if NOTIFY_SOUND and winsound is not None:
            try:
                msg = message.lower()
                # На старте записи и на «распознаю» молчим: звук в этот момент
                # попадёт в микрофон (при включённой предзаписи — гарантированно).
                # Сравнение по началу строки, а не по вхождению: «Слишком короткая
                # запись» — это отказ, и он обязан быть слышен.
                if not (msg.startswith("запись") or msg.startswith("распознаю")):
                    low = (title + " " + message).lower()
                    bad = any(w in low for w in (
                        "сбой", "не удалась", "частично", "недоступен", "занят",
                        "не распознана", "коротк", "молчит"))
                    winsound.MessageBeep(0x30 if bad else 0x00)
            except Exception:
                pass

    threading.Thread(target=run_notify, daemon=True).start()

# ─── Утилиты текста ───────────────────────────────────────────────────────────

def smart_grammar_fix(text: str) -> str:
    """Косметика пробелов и ничего больше.

    Убрано отсюда сознательно:
      - вставка пробела после .!? — ломала числа, версии и имена файлов
        ("версия 3.5" -> "версия 3. 5", "main.py" -> "main. py");
      - схлопывание ([,.!?])\\1+ — вместе с приведением "...." к "..." выше
        превращало многоточие в одну точку;
      - re.sub(r"(\\w+)ться", r"\\1ться") — шаблон тождественен замене, мёртвый код;
      - схлопывание Claude[а-яА-Я]+ — переписывало слово целиком ("Клауде" -> "Claude").
    Whisper сам расставляет пробелы после знаков корректно; чинить нечего.
    """
    if not text:
        return text
    text = re.sub(r'\s+([,.!?;:])', r'\1', text)   # пробел ПЕРЕД знаком — всегда лишний
    text = re.sub(r'[ \t]{2,}', ' ', text)          # двойные пробелы
    return text.strip()

# Союзы/предлоги в конце — фраза не завершена, точку не ставим.
# Английские when|where отсюда убраны: это регексп по РУССКОЙ речи, а четырёх
# нормальных русских союзов (когда, где, куда, откуда) в нём не было.
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

# ─── Таблица названий ────────────────────────────────────────────────────────
TERM_FIX = [
    # WhisperKey — модель ломает название шестью разными способами
    (r"\b(?:виспер\s?кей|висперкей|виспро[\s.]?кей|wispro[\s.]?ke[yй]|wisperk|whisper\s?key)\b", "WhisperKey"),
    (r"\bвиспр[оа]\b", "WhisperKey"),
    (r"\bвиспер\b", "WhisperKey"),
    # Gemini
    (r"\b(?:jiminy|джимм?и|джемини|гемини)\b", "Gemini"),
    # Claude. «клад» и «код» НЕ трогаем — это настоящие слова.
    (r"\bкл[оа]уд\w*\b", "Claude"),
    (r"\bклод\b", "Claude"),
    # Маркетплейсы
    (r"\badaxmarket\b", "Яндекс Маркет"),
    (r"\b(?:valberis|валберис|вайлдберриз|вайлдберис)\b", "Wildberries"),
    (r"\b(?:озон|ozon)\b", "Ozon"),
    # Прочее из корпуса
    (r"\bюджайл\w*\b", "Yougile"),
    (r"\bгитхаб\w*\b", "GitHub"),
    (r"\bдипграм\w*\b", "DeepGram"),
    (r"\bтелеграм\b", "Telegram"),
    # CEO. В корпусе Егора 23 из 23 вхождений «seo» — это «CEO to CEO».
    # Настоящего SEO у него нет ни разу; если появится — строку убрать.
    (r"\bseo\b", "CEO"),
]
_RX = [(re.compile(p, re.IGNORECASE), r) for p, r in TERM_FIX]

_TERM_FIX_RX: list = []   # компилируется при первом вызове

def fix_known_terms(text: str) -> tuple[str, int]:
    """Заменяет названия, записанные по звучанию, на правильные.

    Работает без обращения к сети и без второго прохода: чистая таблица.
    Замер 08.08.26 — на живом корпусе Егора (213 записей, 8590 слов) исправлено
    46 названий в 26 записях; на калибровке точность 79.3% -> 80.2%.
    """
    if not text:
        return text, 0
    if not _TERM_FIX_RX:
        _TERM_FIX_RX.extend((re.compile(p, re.IGNORECASE), r) for p, r in TERM_FIX)
    n = 0
    for rx, rep in _TERM_FIX_RX:
        text, k = rx.subn(rep, text)
        n += k
    return text, n

def transfer_terms(base_text: str, vocab_text: str) -> tuple[str, int]:
    """Подставляет названия продуктов из словарного прохода. Пословно.

    Замена участками целиком съедала соседние слова — на замере это стоило
    +5 ошибок против +9 выигранных. Здесь слова спариваются по позиции внутри
    участка расхождения, и подставляется ровно одно слово вместо одного,
    причём только из TERM_CANON.
    """
    if not base_text or not vocab_text:
        return base_text, 0
    a, b = base_text.split(), vocab_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out, used = list(a), 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb,
                                                       autojunk=False).get_opcodes():
        if tag != 'replace':
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            canon = TERM_CANON.get(nb[j1 + k])
            if canon:
                out[i1 + k] = canon
                used += 1
    return ' '.join(out), used

ENDING_MIN_STEM = 4   # короче — слишком легко совпасть случайно

def transfer_endings(base_text: str, donor_text: str) -> tuple[str, int]:
    """Правит окончание слова по донорскому проходу. Слово другим стать не может.

    Whisper слышит команду верно, но ставит форму по привычке субтитровых
    корпусов: «сохрани» -> «сохраняет», «выстрой» -> «выстроить»,
    «реализуй» -> «реализует». Основа при этом всегда совпадает — на неё
    и опираемся: замена разрешена, только если начала слов совпали минимум
    на ENDING_MIN_STEM букв и расходятся не более чем в трёх последних.
    Поэтому «длинными» -> «тебе» невозможно по построению.

    Замер 08.08.26 на калибровке (эталон — прочитанный вслух текст):
    перенесено 12 окончаний, исправлено 9 слов, испорчено 1, длина не изменилась.
    """
    if not base_text or not donor_text:
        return base_text, 0
    a, b = base_text.split(), donor_text.split()
    na = [_transfer_norm(x) for x in a]
    nb = [_transfer_norm(x) for x in b]
    out, used = list(a), 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, na, nb,
                                                       autojunk=False).get_opcodes():
        if tag != 'replace':
            continue
        for k in range(min(i2 - i1, j2 - j1)):
            x, y = na[i1 + k], nb[j1 + k]
            if x == y or len(x) < ENDING_MIN_STEM or len(y) < ENDING_MIN_STEM:
                continue
            common = 0
            for c1, c2 in zip(x, y):
                if c1 != c2:
                    break
                common += 1
            if common >= ENDING_MIN_STEM and common >= min(len(x), len(y)) - 3:
                out[i1 + k] = b[j1 + k]
                used += 1
    return ' '.join(out), used

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

# ─── Вставка текста (платформенные точки 3 и 7) ───────────────────────────────

VK_CONTROL = 0x11
VK_V = 0x56
VK_MENU_MASK = 0xE8        # код без назначения — им гасится одиночный Alt
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
ERROR_ACCESS_DENIED = 5

# Пауза перед возвратом буфера. 0.5 с из прежней версии — гонка: Ctrl+V
# асинхронен, окно читает буфер, когда доберётся до сообщения, и Electron-окна
# (Slack, VS Code) после тяжёлого декода регулярно не успевают — вставлялось
# СТАРОЕ содержимое буфера вместо диктовки.
CLIPBOARD_RESTORE_DELAY = 1.5


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32),
                ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]

class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32),
                ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]

class _INPUT_UNION(ctypes.Union):
    # Все три члена объявлены не для красоты: SendInput сверяет cbSize с размером
    # НАСТОЯЩЕЙ структуры INPUT, а её размер задаёт самый большой член (мышиный).
    # С одним только KEYBDINPUT структура вышла бы на 8 байт короче, и вызов
    # отвергался бы с ERROR_INVALID_PARAMETER.
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUT_UNION)]


_user32 = None

def _get_user32():
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
        u.SendInput.restype = ctypes.c_uint
        u.MapVirtualKeyW.argtypes = (ctypes.c_uint, ctypes.c_uint)
        u.MapVirtualKeyW.restype = ctypes.c_uint
        u.GetForegroundWindow.argtypes = ()
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_uint32))
        u.GetWindowThreadProcessId.restype = ctypes.c_uint32
        _user32 = u
    return _user32


def _foreground_input_blocked() -> bool:
    """True — активное окно принадлежит процессу выше нас по целостности (UIPI).

    Зачем отдельная проверка, хотя SendInput возвращает число событий: по
    документации Microsoft «This function fails when it is blocked by UIPI.
    Note that neither GetLastError nor the return value will indicate the
    failure». То есть при вставке в окно, запущенное от администратора,
    SendInput отчитывается об успехе, ввод при этом никуда не идёт — и решение
    «вставилось, можно вернуть буфер» уничтожает диктовку через
    CLIPBOARD_RESTORE_DELAY секунд. Ровно тот отказ, ради которого проверка
    возврата и заводилась, по возврату не виден.

    Признак, который работает: попытка открыть процесс окна с самым слабым
    правом PROCESS_QUERY_LIMITED_INFORMATION. Непривилегированный процесс
    получает на процессе с более высокой целостностью ERROR_ACCESS_DENIED —
    это то же самое условие, по которому UIPI режет ввод. Если WhisperKey
    сам запущен от администратора, открытие проходит, и путь обычный.

    Любая неопределённость трактуется в сторону сохранности текста: не смогли
    открыть — считаем, что вставка не подтверждена, и буфер не возвращаем.
    """
    try:
        u = _get_user32()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        pid = ctypes.c_uint32(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return False

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
        if handle:
            k32.CloseHandle(ctypes.c_void_p(handle))
            return False
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    except Exception:
        return False


def _send_keys(events: list) -> bool:
    """Шлёт настоящие виртуальные клавиши. True — событие принято системой.

    pynput тут не годится по механике: kb.press('v') под Windows превращается
    в KeyCode(char='v', vk=None), а win32-бэкенд отправляет такой символ как
    KEYEVENTF_UNICODE с wVk=0 (VK_PACKET). Это событие ВВОДА ТЕКСТА, а не
    нажатие клавиши: приложение получает WM_CHAR 'v' при зажатом Ctrl, и
    акселератор Ctrl+V из этого не рождается — вставка либо не срабатывает,
    либо в поле прилетает буква «v». Явный vk снимает и вторую проблему —
    раскладка перестаёт влиять вовсе.

    Возврат SendInput ловит только грубый отказ (событие не принято системой
    вовсе). Блокировку UIPI он НЕ ловит: документация Microsoft прямо говорит,
    что при ней ни возврат, ни GetLastError на отказ не указывают. Поэтому
    окно от администратора проверяется отдельно — _foreground_input_blocked.
    """
    user32 = _get_user32()
    n = len(events)
    arr = (_INPUT * n)()
    for i, (vk, is_up) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = _KEYBDINPUT(
            wVk=vk,
            wScan=user32.MapVirtualKeyW(vk, 0),
            dwFlags=(KEYEVENTF_KEYUP if is_up else 0),
            time=0,
            dwExtraInfo=0,
        )
    sent = user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
    if sent == n:
        return True
    err = ctypes.get_last_error()
    if err == ERROR_ACCESS_DENIED:
        print("[insert] Ввод заблокирован: активное окно запущено от администратора (UIPI)")
    else:
        print(f"[insert] SendInput отправил {sent} из {n} событий, код {err}")
    return False


def _send_ctrl_v():
    """Ctrl+V. True — ушло в окно, False — система отказала, None — не смогли позвать."""
    try:
        return _send_keys([(VK_CONTROL, False), (VK_V, False),
                           (VK_V, True), (VK_CONTROL, True)])
    except Exception as e:
        print(f"[insert] SendInput недоступен: {type(e).__name__}")
        return None


def mask_alt_press() -> None:
    """Гасит меню, которое Windows открывает на ОДИНОЧНОМ Alt.

    Аналога на macOS нет и взять его в эталоне неоткуда: правый Option сам
    по себе там не делает ничего. На Windows одиночное нажатие-отпускание Alt
    активирует строку меню или ленту активного окна (Проводник, Word, браузер),
    а наш сценарий — ровно одиночный Alt: зажал, поговорил, отпустил, ничего
    между. Фокус после этого уходит в меню, и Ctrl+V прилетает В МЕНЮ, а не
    в поле ввода. Достаточно, пока Alt ещё удерживается, «нажать» вместе с ним
    незанятую клавишу: система видит комбинацию Alt+X и меню не открывает
    (тот же приём применяют в AutoHotkey).

    Отправка идёт из отдельного потока: блокирующая работа внутри колбэка
    низкоуровневого хука (WH_KEYBOARD_LL) может превысить LowLevelHooksTimeout,
    после чего система перестаёт звать наш хук и клавиша умирает до перезапуска.
    """
    def run():
        try:
            _send_keys([(VK_MENU_MASK, False), (VK_MENU_MASK, True)])
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _insert_via_pynput(text: str) -> bool:
    """Запасная вставка, когда WinAPI позвать не удалось.

    Ctrl+V здесь идёт с ЯВНЫМ vk: kb.press('v') под Windows уходит как
    unicode-пакет и акселератором не становится (см. _send_keys). Если и это
    не вышло — посимвольный ввод: медленно, зато не зависит ни от буфера,
    ни от акселератора.
    """
    try:
        v_key = keyboard.KeyCode.from_vk(VK_V)
        kb.press(KeyboardKey.ctrl)
        kb.press(v_key)
        kb.release(v_key)
        kb.release(KeyboardKey.ctrl)
        return True
    except Exception:
        pass
    try:
        kb.type(text)
        print("[insert] Вставка посимвольным вводом")
        return True
    except Exception:
        return False


def _copy_to_clipboard(text: str, attempts: int = 3) -> bool:
    """Кладёт текст в буфер, переживая временную блокировку буфера.

    OpenClipboard эксклюзивен, и его на доли секунды держат Punto Switcher,
    менеджеры буфера (Ditto) и проброс буфера в RDP — очень частая связка.
    pyperclip в этот момент бросает PyperclipWindowsException; раньше оно
    уходило в общий except, и текст диктовки пропадал бесследно.
    """
    for i in range(attempts):
        try:
            pyperclip.copy(text)
            return True
        except Exception as e:
            print(f"[insert] Буфер занят ({type(e).__name__}), повтор {i + 1} из {attempts}")
            time.sleep(0.15 * (i + 1))
    return False


def _restore_clipboard_async(old_clipboard: str) -> None:
    """Возврат буфера обмена в фоне — не блокирует завершение вставки."""
    def run_restore():
        try:
            time.sleep(CLIPBOARD_RESTORE_DELAY)
            pyperclip.copy(old_clipboard)
        except Exception as e:
            print(f"[insert] clipboard restore error: {e}")
    threading.Thread(target=run_restore, daemon=True).start()


def direct_insert(text: str):
    """Вставка на Windows: буфер обмена плюс настоящий Ctrl+V.

    Три отличия от прежней версии, и каждое — это потерянный текст:
      1. успех ПРОВЕРЯЕТСЯ. pynput ничего не возвращает, поэтому раньше
         печаталось «[insert success]» даже когда окно не приняло ничего;
      2. при неудаче буфер НЕ восстанавливается: текст остаётся в нём и
         вставляется руками. Раньше через 0.5 с буфер затирался прежним
         содержимым — диктовка уничтожалась дважды, и в окно не попала,
         и из буфера стёрлась;
      3. пустой прежний буфер не возвращается: pyperclip отдаёт '' для
         скопированных ФАЙЛОВ (CF_HDROP) и картинок, и «восстановление»
         затирало пользовательскую копию пустотой.
    """
    try:
        old_clipboard = ""
        try:
            old_clipboard = pyperclip.paste() or ""
        except Exception as e:
            print(f"[insert] Прежний буфер не прочитан ({type(e).__name__})")

        if not _copy_to_clipboard(text):
            print("[insert fail] Буфер обмена заблокирован — текст не скопирован")
            notify("WhisperKey — вставка не удалась",
                   "Буфер занят другой программой, текст потерян")
            return

        time.sleep(0.1)   # окну нужен момент, чтобы увидеть новое содержимое буфера

        # Целостность активного окна спрашиваем ДО отправки: по документации
        # Microsoft при блокировке UIPI SendInput не сообщает об отказе ни
        # возвратом, ни GetLastError — то есть на возврат в этом случае
        # полагаться нельзя (см. _foreground_input_blocked).
        blocked = _foreground_input_blocked()

        inserted = _send_ctrl_v()
        if inserted is None:
            # WinAPI недоступен — остаётся pynput. Проверить успех здесь нечем,
            # поэтому путь именно запасной.
            inserted = _insert_via_pynput(text)
        # Явный отказ SendInput (inserted is False) назад не лечится, и пробовать
        # что-то ещё нельзя: pynput на Windows шлёт ввод ТЕМ ЖЕ SendInput, значит
        # упрётся в тот же запрет — но соврёт об успехе, и буфер будет затёрт.

        if inserted and not blocked:
            print(f"[insert success] '{text[:30]}...'")
            if RESTORE_CLIPBOARD and old_clipboard:
                _restore_clipboard_async(old_clipboard)
        elif blocked:
            # Текст ОСТАЁТСЯ в буфере — восстанавливать прежнее содержимое
            # нельзя, иначе диктовка исчезнет и из окна, и из буфера.
            print("[insert fail] Активное окно от администратора (UIPI): "
                  "текст остался в буфере обмена — нажми Ctrl+V")
            notify("WhisperKey — вставка не удалась",
                   "Окно запущено от администратора. Текст в буфере, нажми Ctrl+V")
        else:
            print("[insert fail] Текст остался в буфере обмена — нажми Ctrl+V")
            notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Ctrl+V")

    except Exception as e:
        print(f"[insert error] {e}")
        notify("WhisperKey — вставка не удалась", "Текст в буфере, нажми Ctrl+V")

# ─── Снятие водяных знаков ────────────────────────────────────────────────────

artifacts_removed: list[str] = []   # что вырезали в последней обработке — для уведомления
audio_dropouts: list[str] = []      # переполнения входного буфера за время записи
                                    # (сэмплы, выброшенные железом до распознавания)

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
        без ведома говорящего — прямое нарушение цели системы;
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
    """Сжатие длинных пауз. НЕ ВЫЗЫВАЕТСЯ — оставлено ровно как в эталоне.

    Убрано из тракта по замеру: на диктовке результат побайтово тот же,
    на длинной записи цепочка предобработки стоила 15.6% слов.
    """
    try:
        if len(audio_data) == 0: return audio_data
        window_size = int(SAMPLE_RATE * 0.1)
        n_windows = len(audio_data) // window_size
        if n_windows == 0: return audio_data
        windows = audio_data[:n_windows*window_size].reshape(-1, window_size)
        is_silent = np.max(np.abs(windows), axis=1) < threshold
        silent_diff = np.diff(is_silent.astype(int))
        starts = np.where(silent_diff == 1)[0] + 1
        ends = np.where(silent_diff == -1)[0] + 1
        if is_silent[0]: starts = np.insert(starts, 0, 0)
        if is_silent[-1]: ends = np.append(ends, n_windows)
        min_pause_windows = int(min_pause / 0.1)
        keep_pause_samples = int(keep_pause * SAMPLE_RATE)
        output_chunks = []
        last_idx = 0
        for s, e in zip(starts, ends):
            if (e - s) > min_pause_windows:
                output_chunks.append(audio_data[last_idx * window_size : s * window_size])
                output_chunks.append(np.zeros(keep_pause_samples, dtype=np.float32))
                last_idx = e
        output_chunks.append(audio_data[last_idx * window_size:])
        return np.concatenate(output_chunks) if output_chunks else audio_data
    except Exception as e:
        print(f"[compress error] {e}")
        return audio_data

def _change_tempo(audio_data, factor: float):
    """Меняет темп записи линейной интерполяцией по новой сетке отсчётов.

    Частота дискретизации в заголовке WAV остаётся прежней, поэтому для модели
    речь звучит быстрее, а слова остаются теми же — это не обработка звука
    и не фильтр, ничего не вырезается и не добавляется.
    """
    n = int(len(audio_data) / factor)
    if n < 2 or factor == 1.0:
        return audio_data
    grid = np.linspace(0, len(audio_data) - 1, n)
    return np.interp(grid, np.arange(len(audio_data)), audio_data).astype(np.float32)

def _restore_timeline(result: dict) -> None:
    """Возвращает таймкоды ответа к реальному времени записи.

    Звук уходит ускоренным в ASR_TEMPO раз, значит и segments[], и words[]
    приходят на сжатой шкале. Всё, что считает по времени — гейт плотности,
    подстановка из words[], снятие перекрытий при склейке длинных записей —
    рассчитано на реальные секунды. Умножаем обратно прямо здесь, чтобы
    остальной код об ускорении вообще не знал.
    """
    if ASR_TEMPO == 1.0:
        return
    for key in ('segments', 'words'):
        for item in (result.get(key) or []):
            for field in ('start', 'end'):
                v = item.get(field)
                if isinstance(v, (int, float)):
                    item[field] = v * ASR_TEMPO

def create_audio_wav(audio_data):
    """Упаковка звука в WAV. Единственная обработка — ускорение темпа.

    Всё остальное, что здесь было раньше, снято по замерам:
      - compress_silence: на диктовке результат побайтово тот же (совпадение 1.0000),
        на длинной записи цепочка стоила 15.6% слов;
      - паддинг 0.5 с тишины: приписывание чистой цифровой тишины к идентичному
        аудио сдвигает границы 30-секундных окон модели и отнимает 8-10% слов;
      - нормировка по пику: пользы не показала, а вместе с паддингом двигала таймлайн.

    ASR_TEMPO — замер 06.08.26 на 19 диктовках Егора против эталона SaluteSpeech:
      темп 1.00 (как было)  14.8% ошибок
      темп 1.03             13.3%   лучше на 10 записях, хуже на 3
      темп 0.97             13.9%   лучше на 7, хуже на 6
      +0.7 с тишины в начало 16.5%  заметно хуже
    Бутстрап 20000 пересборок для 1.03: −1.50 п.п., ДИ95 [−3.36; +0.34], хуже базы
    в 5% пересборок. То есть выигрыш вероятен, но на 19 записях не доказан строго —
    интервал краем задевает ноль. Порог решения: правка ничем не рискует (слова
    не трогаются, задержка не растёт), поэтому принята.
    Почему работает: у whisper окно 30 секунд и жёсткая привязка к темпу, сдвиг
    темпа меняет весь путь декодирования.
    Функция применяется ТОЛЬКО к облачному проходу — create_audio_wav вызывается
    из transcribe_cloud_turbo и больше нигде. Локальная модель и запись в
    корпус эталонов (_write_raw_recording_wav) получают исходный звук.
    """
    try:
        audio_data = np.asarray(audio_data, dtype=np.float32)
        if ASR_TEMPO != 1.0:
            audio_data = _change_tempo(audio_data, ASR_TEMPO)
        audio_data = np.clip(audio_data, -1.0, 1.0)

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

def transcribe_deepgram(audio_data) -> str | None:
    """Первая ступень каскада. Возвращает текст либо None — тогда работает Groq.

    Возврат None здесь никогда не означает потерю диктовки: вызывающий код
    молча уходит на whisper, и человек видит разницу только в строке [engine].

    Отказы разделены намеренно. Кончившиеся деньги, отозванный ключ и запрет
    по правам (401, 402, 403) лечиться повтором не могут — движок отключается
    на DEEPGRAM_BLOCK_SECONDS, чтобы каждая следующая диктовка не платила
    секундой ожидания за заведомо мёртвый запрос. Перегрузка и сбои сервера
    (429, 5xx) — наоборот, проходят сами, и здесь уместен короткий повтор.
    """
    if not DEEPGRAM_ENABLED:
        return None
    if time.time() < deepgram_status["blocked_until"]:
        return None

    wav_data = create_audio_wav(audio_data)
    if not wav_data:
        return None

    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/wav",
    }

    t0 = time.time()
    for attempt in range(DEEPGRAM_RETRIES):
        try:
            response = http_session.post(
                DEEPGRAM_URL, params=DEEPGRAM_PARAMS, headers=headers,
                data=wav_data, timeout=DEEPGRAM_TIMEOUT
            )

            if response.status_code == 200:
                try:
                    alt = response.json()["results"]["channels"][0]["alternatives"][0]
                except (KeyError, IndexError, ValueError) as e:
                    print(f"[deepgram] ответ не разобран ({type(e).__name__}) — ухожу на Groq")
                    return None
                text = (alt.get("transcript") or "").strip()
                deepgram_status["last_seconds"] = time.time() - t0
                if not text:
                    print("[deepgram] пусто — ухожу на Groq")
                    return None
                return text

            if response.status_code in (401, 402, 403):
                reason = {401: "ключ не принят", 402: "кончились деньги на счёте",
                          403: "доступ запрещён"}[response.status_code]
                deepgram_status["blocked_until"] = time.time() + DEEPGRAM_BLOCK_SECONDS
                deepgram_status["last_reason"] = reason
                mins = int(DEEPGRAM_BLOCK_SECONDS / 60)
                print(f"[deepgram] {reason} (HTTP {response.status_code}). "
                      f"Перехожу на Groq, повторю попытку через {mins} мин.")
                notify("WhisperKey — сменил движок",
                       f"Deepgram: {reason}. Работаю на Groq.")
                return None

            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < DEEPGRAM_RETRIES:
                time.sleep(0.5)
                continue

            print(f"[deepgram] статус {response.status_code} — ухожу на Groq")
            return None

        except requests.Timeout:
            print(f"[deepgram] не ответил за {DEEPGRAM_TIMEOUT:.0f}с — ухожу на Groq")
            return None
        except Exception as e:
            if attempt + 1 < DEEPGRAM_RETRIES:
                time.sleep(0.5)
                continue
            print(f"[deepgram] {type(e).__name__} — ухожу на Groq")
            return None

    return None

def transcribe_cloud_turbo(audio_data, allow_retry: bool = True, use_prompt: bool = True,
                           return_words: bool = False, prompt_override: str = ""):
    """Расшифровка через Groq whisper-large-v3.

    Возвращает строку текста либо None. При return_words=True — кортеж
    (текст, список слов с таймкодами): слова нужны для склейки перекрытий
    по времени, текстовое сравнение на стыке ненадёжно, потому что модель
    распознаёт один и тот же участок в разных кусках немного по-разному.
    Сорванные окна отдаёт через cloud_status['last_degenerate'].

    Нормировка входного звука отсюда убрана вместе со всей предобработкой:
    сдвиг амплитуды двигал границы 30-секундных окон модели и стоил слов.
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
        'User-Agent': WIN_USER_AGENT,
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
            # timeout=60, а не 15: порог нарезки теперь 15 минут, то есть в одном
            # запросе уходит вся запись целиком. 15 секунд на её загрузку
            # и распознавание не хватило бы гарантированно, и каждая длинная
            # диктовка молча падала бы на локальную модель.
            response = http_session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers, files=files, data=data, timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                _restore_timeline(result)
                # Текст самой модели — до нашей сборки. Нужен, чтобы при пропаже
                # куска было видно, кто его потерял: модель или конвейер.
                if not prompt:
                    cloud_status["last_api_text"] = (result.get('text') or '').strip()
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

            # 429 и 5xx лечатся ожиданием. Раньше здесь был единственный POST
            # и голый except: pass — любой такой ответ давал молчаливую дыру;
            # на 30-минутной записи так терялось 4 куска из 32.
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
    субтитровые водяные знаки. Прежняя win-нарезка (порог 30 с, куски по 20 с,
    поиск минимума энергии) шла БЕЗ перекрытия, поэтому слова на швах терялись
    безвозвратно и склеить их обратно было нечем.
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

    Кусок, который не распознался, раньше просто выпадал из спискового включения
    [p for p in ordered_parts if p]: дыра склеивалась встык, и приходило бодрое
    «Текст готов». Теперь на его месте остаётся видимая метка — текст честнее,
    чем гладкий обман.

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

def _recognize_with_whisper(audio: np.ndarray, dur: float) -> tuple[str, str, list, list]:
    """Вторая и третья ступени каскада: Groq, а при его отказе — локальная модель.

    Здесь живёт всё, что построено вокруг whisper: дробление длинных записей,
    гейт плотности, восстановление сорванных окон из words[], стилевой проход
    за знаками препинания и переносы названий и форм с него.

    Deepgram ничего этого не требует — он отдаёт готовый текст со знаками, —
    поэтому путь вынесен отдельно и при работающей первой ступени не трогается
    вовсе: ни одного лишнего запроса, ни одной лишней секунды.

    Возвращает (текст, текст до переносов, потерянные куски, качество кусков).
    """
    # Дробление длинной записи включается только за порогом в 15 минут:
    # замер на записи 29:45 дал одним куском 3810 слов и полноту 79.8%
    # против 3559 слов и 72.3% у нарезки по 60 с. Groq дробит файл у себя
    # внутри лучше, чем мы снаружи.
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
            # Словарь future -> idx, а не список: as_completed отдаёт futures
            # в произвольном порядке, а у упавшего результата индекса нет вовсе.
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

    # Склейка: перекрытие снимается по таймкодам слов, текстовое сравнение —
    # только запасной путь. Нечёткого сравнения нет намеренно.
    text, lost_marks = _join_chunks(
        ordered_parts, chunk_quality, chunk_offsets, chunk_words)

    if not text:
        if style_pool is not None:
            style_pool.shutdown(wait=False)
        return "", "", lost_marks, chunk_quality

    assembled = text                       # до переносов знаков/названий/форм
    print(f"[raw whisper] '{text}'")

    # Знаки препинания из второго прохода. Он уже почти наверняка завершён —
    # шёл параллельно. Ждём не дольше 8 секунд: текст важнее оформления,
    # и молчаливо задерживать вставку из-за знаков нельзя.
    if style_future is not None:
        try:
            styled = style_future.result(timeout=8.0)
            if styled:
                before = text
                text = transfer_punctuation(text, styled)
                if text != before:
                    print(f"[punct] знаки перенесены со второго прохода")
                # Названия продуктов — из того же прохода, отдельного запроса
                # он не требует. Слова берутся только из белого списка.
                text, n_terms = transfer_terms(text, styled)
                if n_terms:
                    print(f"[terms] названий поправлено: {n_terms}")
                # Формы команд — оттуда же. Слово может сменить только окончание.
                text, n_end = transfer_endings(text, styled)
                if n_end:
                    print(f"[forms] окончаний поправлено: {n_end}")
        except Exception as e:
            print(f"[punct] второй проход не дошёл ({type(e).__name__}) — "
                  f"текст остаётся без добавленных знаков")
        finally:
            style_pool.shutdown(wait=False)

    return text, assembled, lost_marks, chunk_quality

def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, cloud_status, session_phase
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        # Windows при запрещённом микрофоне («Параметры → Конфиденциальность →
        # Микрофон → Разрешить классическим приложениям») НЕ отдаёт ошибку
        # захвата — он отдаёт ТИШИНУ. На macOS отказ жёсткий (TCC), поэтому
        # в эталоне такой проверки нет и взять её оттуда неоткуда. Единственный
        # доступный признак — нулевой пик амплитуды.
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if peak < 1e-4:
            print(f"[mic] В записи только тишина (пик {peak:.6f})")
            notify("WhisperKey — микрофон молчит",
                   "Проверь Параметры → Конфиденциальность → Микрофон")
            return

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        # ─── Каскад из трёх ступеней ─────────────────────────────────────────
        # 1. Deepgram — точнее на 8 пунктов и вдвое быстрее (замер в шапке файла);
        # 2. Groq whisper со всей нашей обвязкой — когда Deepgram молчит,
        #    отключён, исчерпан или не отвечает;
        # 3. локальная модель внутри второй ступени — когда интернета нет вовсе.
        # Ступени идут ПО ОЧЕРЕДИ, а не одновременно: первая справляется быстрее,
        # чем вторая успела бы стартовать, и платить за оба движка сразу незачем.
        t_asr_start = time.time()
        full_raw_text = transcribe_deepgram(audio)
        if full_raw_text:
            engine = "deepgram"
            assembled_text = full_raw_text
            lost_marks: list = []
            chunk_quality: list[str] = ['deepgram']
            print(f"[engine] Deepgram {DEEPGRAM_MODEL}, "
                  f"{deepgram_status['last_seconds']:.1f}с")
            print(f"[raw deepgram] '{full_raw_text}'")
        else:
            engine = "whisper"
            print("[engine] Groq whisper (запасной путь)")
            full_raw_text, assembled_text, lost_marks, chunk_quality = \
                _recognize_with_whisper(audio, dur)

        t_asr_done = time.time()

        if not full_raw_text:
            print("[skip] Пустой результат")
            notify("WhisperKey", "Речь не распознана")
            return

        # Таблица названий работает ВСЕГДА, даже когда второго прохода не было:
        # короткая диктовка, отказ сети, исчерпанная квота. Сети не требует.
        full_raw_text, n_fix = fix_known_terms(full_raw_text)
        if n_fix:
            print(f"[terms] по таблице поправлено: {n_fix}")

        # LLM-полировка удалена. Модель llama-3.1-70b-versatile выведена Groq из
        # обслуживания (HTTP 400 model_decommissioned), и год вызов молча возвращал
        # сырой текст — это и было то качество, которое всех устраивало. Рабочая
        # llama-3.3 на замере переписывает 4.2-5.6% слов, а цель — дословность.
        # Отдельно: LLAMA_SKIP_MAX_WORDS = 0 делал условие len(words) > 0 истинным
        # для любого непустого текста, поэтому корректор звался НА КАЖДОЙ диктовке
        # и каждый раз тратил лишний round-trip на мёртвую модель.
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
                notify("WhisperKey", "Текст готов" + suffix)
        else:
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

# ─── Обработка клавиш (платформенная точка 7) ─────────────────────────────────

TRIGGER_VK = 165          # VK_RMENU


def _key_vk(key):
    """vk клавиши, чем бы она ни пришла из pynput.

    Тонкость, из-за которой сравнение «в лоб» не работает: колбэк получает либо
    KeyCode (у него есть .vk), либо член перечисления Key — а у члена enum
    своего .vk НЕТ, он лежит в .value. Поэтому hasattr(key, 'vk') на Key всегда
    False, и проверка «key.vk == 165» до члена перечисления не доходит вовсе.
    """
    vk = getattr(key, 'vk', None)
    if vk is None:
        vk = getattr(getattr(key, 'value', None), 'vk', None)
    return vk


def is_trigger(key):
    """Правый Alt. Код 61 из mac-версии удалён — это vk правого Option на macOS.

    На Windows правый Alt — это VK_RMENU = 165, а 61 в таблице VK не назначен
    (VK_OEM_PLUS = 187), то есть проверка на него давала только лишний шанс
    на ложное срабатывание от посторонней клавиши.

    ГЛАВНОЕ, и это ломало клавишу целиком: pynput на Windows отдаёт правый Alt
    как Key.alt_gr, а НЕ Key.alt_r. В pynput/keyboard/_win32.py объявлены обе
    константы с одним и тем же vk 165 (alt_r с флагом EXTENDEDKEY, alt_gr без
    него), из-за разных флагов они не схлопываются в псевдоним enum, а таблица
    listener'а строится словарём {key.value.vk: key} — и alt_gr, объявленный
    ПОЗЖЕ, затирает alt_r. То есть vk 165 всегда приезжает как Key.alt_gr.
    Проверено воспроизведением логики enum из pynput 1.8.2.
    Сравнение «key == Key.alt_r» при этом даёт False, а запасная ветка
    «hasattr(key, 'vk')» до члена перечисления не добирается (см. _key_vk) —
    в сумме триггер не срабатывал НИКОГДА, запись не начиналась.

    Поэтому здесь три пути сразу: оба члена перечисления и явный vk. Левый Alt
    (164) и общий Alt (18) под них не подпадают.

    Про AltGr: стандартная русская раскладка Windows флага KLLF_ALTGR не имеет,
    правый Alt в ней — обычный VK_RMENU, и триггер безопасен. На раскладках
    с настоящим AltGr (US-International, немецкая, польская) система перед
    RMENU подсовывает фантомный левый Ctrl — там ввод любого символа через
    AltGr будет стартовать запись. Это ограничение, а не дефект кода.
    """
    if key is None:
        return False
    if key is keyboard.Key.alt_r or key is getattr(keyboard.Key, 'alt_gr', None):
        return True
    try:
        return _key_vk(key) == TRIGGER_VK
    except Exception:
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
                    # trigger_held сознательно НЕ сбрасываем: клавиша ещё
                    # физически нажата, а on_press у pynput приходит и на
                    # автоповторе — сброс запустил бы попытки открыть микрофон
                    # каждые TRIGGER_DEBOUNCE_SEC, пока клавишу держат.
                    if not ensure_audio_stream():
                        session_phase = "idle"
                        return
                    # Порядок важен: буфер заполняется ДО поднятия is_recording,
                    # иначе колбэк успевает дописать блок в старый список, и этот
                    # блок (~32 мс речи) выбрасывается. Раньше здесь был обратный
                    # порядок — is_recording = True, потом recording_data = [].
                    recording_data = list(preroll_buffer)
                    preroll_buffer.clear()
                    audio_dropouts.clear()
                    is_recording = True

                    # Пока Alt ещё удерживается — гасим меню активного окна,
                    # иначе Ctrl+V после диктовки уйдёт в меню (см. mask_alt_press).
                    mask_alt_press()

                    # Уведомление ПОСЛЕ фактического начала захвата: раньше
                    # «говори» выдавалось до того, как микрофон отдавал первый
                    # сэмпл, и первое слово срезалось.
                    notify("WhisperKey", "Запись...")
                    pre = len(recording_data) * 512 / SAMPLE_RATE
                    # Нулевая предзапись при включённом режиме — признак того, что
                    # поток был мёртв. Молчать об этом нельзя: дальше запись уйдёт
                    # в никуда, а пользователь увидит «слишком короткая запись».
                    if pre <= 0:
                        print("[audio] ВНИМАНИЕ: предзапись пуста, поток был неисправен")
                    else:
                        print(f"[rec] Начата (предзапись {pre * 1000:.0f} мс)")
                else:
                    recording_data = []
                    is_recording = True
                    mask_alt_press()
                    # Открытие устройства и уведомление «Запись…» — внутри
                    # _start_capture_async. Открытие стоит сотни миллисекунд,
                    # а этот поток обязан освободиться немедленно; уведомление
                    # едет вместе с ним, чтобы «говори» по-прежнему приходило
                    # не раньше, чем микрофон реально начал отдавать сэмплы.
                    _start_capture_async(active_session_id)

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

        def delayed_stop():
            # global обязателен для ВСЕХ трёх имён. session_phase здесь его
            # не имел: объявление в on_release на вложенную функцию не
            # распространяется, поэтому "idle" ниже писалось в ЛОКАЛЬНУЮ
            # переменную, а модульная фаза навсегда оставалась "processing" —
            # и on_press с этого момента выходил по первой же проверке.
            # Итог: одно случайное короткое касание правого Alt намертво
            # выключало диктовку до перезапуска программы.
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
                notify("WhisperKey", "Слишком короткая запись")
                processing = False
                with state_lock:
                    if active_session_id == current_session_id:
                        session_phase = "idle"
                schedule_idle_close()
                return

            notify("WhisperKey", "Распознаю...")
            print("[rec] Остановлена (хвост захвачен)")
            threading.Thread(target=process_audio, args=(audio_snapshot, current_session_id), daemon=True).start()

        processing = True
        threading.Thread(target=delayed_stop, daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def _desktop_path() -> str:
    """Настоящий Рабочий стол (платформенная точка 4).

    os.path.expanduser("~/Desktop") на Windows врёт при перенаправлении папок
    в OneDrive — а оно включено по умолчанию на большинстве свежих домашних
    систем с учёткой Microsoft. Реальный путь тогда «C:\\Users\\X\\OneDrive\\
    Рабочий стол», а ~/Desktop не существует вовсе: ярлык не создаётся,
    и пользователь считает установку неудавшейся. Реестр OneDrive обновляет,
    поэтому спрашиваем систему, а не собираем путь руками.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        try:
            value, _ = winreg.QueryValueEx(key, "Desktop")
        finally:
            winreg.CloseKey(key)
        if value and os.path.isdir(value):
            return value
    except Exception:
        pass
    return os.path.expanduser("~/Desktop")

def create_desktop_launcher():
    """Создаёт на Рабочем столе .bat, который всегда запускает проект из своей папки."""
    try:
        desktop = _desktop_path()
        if not os.path.isdir(desktop):
            print(f"[setup] Рабочий стол не найден ({desktop}), ярлык не создан")
            return
        target_path = os.path.join(desktop, "WhisperKey.bat")
        if os.path.exists(target_path):
            return
        launcher_body = f'''@echo off
chcp 65001 >nul
title WhisperKey
cd /d "{BASE_DIR}"
call run_whisperkey.bat
'''
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(launcher_body)
        print(f"[setup] Ярлык на Рабочем столе: {target_path}")
    except Exception as e:
        print(f"[setup] Не удалось создать ярлык: {e}")

def main():
    global model
    if not acquire_single_instance_lock():
        print("[FATAL] WhisperKey уже запущен. Закрой предыдущий процесс перед новым стартом.")
        # Именно sys.exit с ненулевым кодом, а не return: run_whisperkey.bat
        # держит окно открытым только при ненулевом errorlevel. С обычным
        # выходом консоль закрывалась мгновенно, и это сообщение — как и любая
        # другая причина отказа стартовать — пользователю не показывалось
        # вообще: он видел мигнувшее окно и считал, что «ничего не произошло».
        sys.exit(2)

    print("\n" + "="*60)
    print(" WhisperKey v24 | Windows Edition | Дословная диктовка")
    print(" Created by Егор Нищук (Telegram: @Seikatsuma)")
    print("="*60)

    create_desktop_launcher()

    try:
        p = psutil.Process(os.getpid())
        # ABOVE_NORMAL, а не HIGH (платформенная точка 5). HIGH выше приоритета
        # активного окна, а рядом крутится локальный faster-whisper на двух
        # потоках — на слабой машине это заметный лаг мыши и ввода.
        # Строку p.nice(-10) из mac-версии сюда переносить НЕЛЬЗЯ: на Windows
        # psutil.nice ждёт класс приоритета, а не число nice, вызов бросил бы
        # исключение и приоритет молча не изменился бы.
        p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        # cpu_affinity сознательно не трогаем. На Windows он, в отличие
        # от macOS, полноценно работает — и именно поэтому опасен: прибивание
        # к паре ядер конкурирует с аудио-колбэком PortAudio и даёт потерю
        # сэмплов, которую видно только на слух.
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
    print(f"   промпт: {'выключен' if not ASR_CONTEXT_PROMPT else 'включён'}")
    try:
        print("Загрузка локальной модели (запасной вариант, если облако недоступно)...")
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2)
        print("Разогрев локальной модели...")
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)

        if USE_CLOUD or DEEPGRAM_ENABLED:
            print("Разогрев облачного соединения...")
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
            # Отказ здесь не фатален: работаем по старой схеме, а не падаем.
            if start_audio_stream():
                print(f"Предзапись включена: {PREROLL_SECONDS * 1000:.0f} мс, "
                      f"микрофон освобождается после {PREROLL_IDLE_TIMEOUT:.0f}с простоя")
                schedule_idle_close()
            else:
                print("Предзапись недоступна — микрофон не открылся, работаю по старой схеме")
    except Exception as e:
        print(f"[FATAL] {e}")
        sys.exit(1)      # чтобы .bat не закрыл окно с этим сообщением

    print("Готов! Зажми ПРАВЫЙ ALT для записи.")
    print("Если текст не вставляется в окно, запущенное от администратора —")
    print("запусти WhisperKey тоже от администратора (ввод режет UIPI).")
    notify("WhisperKey", "Готов к работе!")

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
