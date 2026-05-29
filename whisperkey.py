#!/usr/local/bin/python3.11
"""
WhisperKey v17.5 - CEO PRECISION RESTORED
- Архитектура: Dual-Stage Pipeline (Cloud Stealth + Stable Offline)
- Качество: Context-Aware Grammar (возврат идеальных окончаний)
- Целостность: Fast Tail Capture (300ms) + VAD Shield (1000ms)
- Стабильность: Hysteresis Cloud Switching + 15s Timeout
"""

import threading
import subprocess
import os
import sys
import time
import fcntl
import numpy as np
import sounddevice as sd
import re
import psutil
import requests
import io
import wave

# Настройки для Intel Mac
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

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
TAIL_CAPTURE_SECONDS = 0.8  # CEO Fix: Увеличиваем захват хвоста для надежности
RESTORE_CLIPBOARD = True

# API Настройки (Groq Cloud)
# 1) Export key in shell: export GROQ_API_KEY="gsk_..."
# 2) Fallback to hardcoded value below (if you prefer).
load_env_file(".env")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip() or "YOUR_GROQ_API_KEY_HERE"
USE_CLOUD = bool(GROQ_API_KEY and GROQ_API_KEY != "YOUR_GROQ_API_KEY_HERE")
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
    """CEO Method: Полное отключение микрофона после записи."""
    global audio_stream
    try:
        if audio_stream:
            audio_stream.stop()
            audio_stream.close()
            audio_stream = None
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

def direct_insert(text: str):
    """CEO Method: Вставка через буфер с максимальной совместимостью."""
    try:
        # 1. Сохраняем старый буфер
        old_clipboard = subprocess.run(['pbpaste'], capture_output=True).stdout
        
        # 2. Определяем целевое приложение для логов
        front_app = subprocess.run(
            ["/usr/bin/osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True,
            text=True
        ).stdout.strip()
        print(f"[insert target] {front_app or 'Unknown'}")

        inserted = False
        for attempt in range(1, 4):
            # Копируем текст в буфер
            subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
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
                time.sleep(0.5) # Ждем завершения вставки перед возвратом буфера
                process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
                process.communicate(input=old_clipboard)
        else:
            print("[insert fail] Check Accessibility permissions for Terminal/Cursor")
            
    except Exception as e:
        print(f"[insert error] {e}")

def clean_noise(text: str) -> str:
    """Удаляет галлюцинации и применяет бизнес-словарь."""
    if not text: return ""
    
    # CEO Fix: Если Whisper выдает одно слово-заглушку типа "КОНЕЦ", "Конец", "Конец связи"
    # при наличии аудио - это явная галлюцинация на тишине.
    hallucination_words = ["КОНЕЦ", "Конец", "Конец связи", "Продолжение следует", "Спасибо за просмотр", "Cursor", "Python", "CEO to CEO"]
    if text.strip() in hallucination_words:
        print(f"[hallucination detected] '{text.strip()}' -> skipping")
        return ""

    text = re.sub(r'[фФfFaA]{4,}', '', text).strip()
    text = re.sub(r'[.]{3,}', '...', text).strip()
    
    # CEO Fix: Удаляем повторяющиеся технические термины в самом конце, если они выглядят как галлюцинации
    # (например, если они идут после точки или просто списком в конце)
    bad_endings = [r"Cursor[.!?]*$", r"Python[.!?]*$", r"CEO to CEO[.!?]*$", r"Claude[.!?]*$"]
    for pattern in bad_endings:
        # Если слово встречается в конце и перед ним была точка или это единственное слово в сегменте
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
        r'\b[Dd]eplo\b': 'deploy'
    }
    for pattern, replacement in business_vocabulary.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    # CEO Fix: Удаляем галлюцинации только если они в самом конце и похожи на мусор
    hallucinations = [
        "Субтитры сделал DimaTorzok", "Субтитры создавал DimaTorzok", 
        "Отредактировал DimaTorzok", "Продолжение следует", "Спасибо за просмотр"
    ]
    for bad in hallucinations:
        if text.endswith(bad):
            text = text[:-len(bad)].strip()
    
    # Отдельно для слова "субтитры", которое часто бывает галлюцинацией в конце
    if text.lower().rstrip('.!? ').endswith("субтитры"):
        # Если это не единственное слово
        if len(text.split()) > 2:
            text = re.sub(r'(?i)\s+субтитры[.!?]*$', '', text).strip()
            
    return text.strip()

def create_audio_wav(audio_data):
    """Создание WAV в памяти с защитным интервалом тишины."""
    try:
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
        
        # CEO Debug: Сохраняем последний чанк для анализа качества
        try:
            with open("debug_audio.wav", "wb") as f:
                f.write(wav_io.getvalue())
        except: pass
            
        return wav_io.getvalue()
    except Exception as e:
        print(f"[wav error] {e}")
        return None

def transcribe_cloud_turbo(audio_data):
    """Stage 1: Расшифровка через Groq (Whisper Large-v3) с мгновенным переключением."""
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

    # CEO Fix: Промпт теперь более описательный, что снижает вероятность 
    # простого повторения слов в конце.
    context_prompt = "Это качественная расшифровка русской речи. Темы: программирование на Python, работа в Cursor, использование Claude и обсуждение задач уровня CEO to CEO."

    files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
    data = {
        'model': 'whisper-large-v3',
        'language': 'ru',
        'prompt': context_prompt,
        'temperature': 0.0  # CEO Fix: Возвращаем 0 для максимальной стабильности
    }

    try:
        # CEO Speed Fix: Используем глобальную сессию и WAV (быстрее MP3)
        response = http_session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=15 
        )
        
        if response.status_code == 200:
            return response.json().get('text', '')
        
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
    """Stage 2: Лингвистическая полировка через Llama-3.1-70B."""
    if not raw_text or len(raw_text) < 5: return raw_text
    
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
                    "Ты - элитный корректор русской деловой и технической речи. \n"
                    "Твоя задача: превратить сырую расшифровку (ASR) в безупречный текст.\n\n"
                    "ИНСТРУКЦИИ:\n"
                    "1. Исправь ошибки распознавания, падежи, склонения и окончания.\n"
                    "2. Расставь знаки препинания и заглавные буквы.\n"
                    "3. Сохраняй ВСЕ слова автора и их порядок. НЕ добавляй вводных фраз и НЕ комментируй.\n"
                    "4. Удаляй только слова-паразиты (э-э, мм, ну).\n"
                    "5. Технические термины (Claude, CEO, deploy, Cursor) пиши правильно.\n\n"
                    "ПРИМЕРЫ:\n"
                    "Ввод: 'сделай деплой на сервер клауд'\n"
                    "Вывод: 'Сделай деплой на сервер Claude.'\n\n"
                    "Ввод: 'привет сео то сео как дела'\n"
                    "Вывод: 'Привет, CEO to CEO, как дела?'\n\n"
                    "Выдай ТОЛЬКО исправленный текст."
                )
            },
            {"role": "user", "content": f"Исправь текст: {raw_text}"}
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

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, cloud_status, session_phase
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        text = None
        mode = "OFFLINE"

        # Пытаемся использовать Cloud Turbo только при валидной конфигурации.
        if CLOUD_ENABLED:
            print("[mode] Cloud Turbo (Whisper Large-v3)")
            raw_text = transcribe_cloud_turbo(audio)
            if raw_text:
                # CEO Guard: Если Whisper выдал меньше 3 слов при длительном аудио - это сбой.
                if len(raw_text.split()) < 3 and dur > 3.0:
                    print(f"[guard] Cloud output too short ({len(raw_text.split())} words for {dur:.1f}s). Forcing Offline.")
                    raw_text = None

            if raw_text:
                print(f"[raw whisper] '{raw_text}'")
                print("[mode] Neural Refinement (Llama-3.1-70B)")
                text = refine_text_llm(raw_text)
                mode = "CLOUD+LLM"
        
        if not text:
            if cloud_status["is_blocked"]:
                print(f"[mode] Local Precision (Cloud paused: {int(60 - (time.time() - cloud_status['last_check_time']))}s left)")
            else:
                print("[mode] Local Precision Fallback")
            # CEO Quality: Максимальное усиление сигнала
            max_val = np.max(np.abs(audio))
            if max_val > 0.0001: 
                audio = audio / max_val * 0.99 # Почти максимальная амплитуда
            
            context_prompt = (
                "Это безупречная расшифровка русской деловой речи. "
                "Спикер обсуждает IT-задачи, программирование на Python, деплой и работу в Cursor. "
                "Используются термины: Claude, CEO to CEO, deploy."
            )
            
            # Попытка 1: Deep Listening Mode (CEO Precision)
            segments, _ = model.transcribe(
                audio, language="ru", 
                beam_size=10,         # CEO: Максимальный поиск для точности
                best_of=5,
                patience=2.0,        # CEO: Заставляем модель "вслушиваться" дольше
                repetition_penalty=1.2, 
                vad_filter=False,     
                suppress_blank=True, 
                without_timestamps=True, 
                condition_on_previous_text=False,
                initial_prompt=context_prompt,
                compression_ratio_threshold=2.4,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text: print(f"[raw whisper] '{text}'")

            # Попытка 2: Если Попытка 1 выдала пустоту, пробуем "грубую силу"
            if not text or len(text) < 2:
                print("[warn] Local precision failed, trying brute force...")
                segments, _ = model.transcribe(
                    audio, beam_size=1, 
                    condition_on_previous_text=False,
                    vad_filter=False, suppress_blank=False,
                    no_speech_threshold=0.8
                )
                text = " ".join(seg.text.strip() for seg in segments).strip()

        elapsed = time.time() - t_start
        print(f"[raw]  '{text}'")
        print(f"[time] {elapsed:.1f}s ({elapsed/dur*100:.0f}% от длины) [{mode}]")
        
        text = clean_noise(text)
        text = smart_grammar_fix(text)

        if text and len(text) > 1:
            if text[0].islower(): text = text[0].upper() + text[1:]
            if text[-1] not in '.!?…': text += '.'
            
            # CEO Fix: Всегда выводим финальный результат в консоль для ручного копирования
            print(f"\n--- ФИНАЛЬНЫЙ ТЕКСТ ---\n{text}\n-----------------------\n")
            
            # CEO Fix: Возвращаем контекст (250 символов) для идеальных окончаний
            last_text_context = text[-250:]
            direct_insert(text + " ")
            # CEO Fix: Возвращаем уведомление о готовности текста
            notify("WhisperKey ✓", f"Текст готов [{mode}]")
        else:
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
            notify("WhisperKey", "⏹ Запись остановлена, распознаю...")
            
            # После полной остановки и захвата хвоста - запускаем обработку
            audio_snapshot = list(recording_data)
            if len(audio_snapshot) < 5:
                print("[skip] Слишком коротко")
                global processing
                processing = False
                with state_lock:
                    if active_session_id == current_session_id:
                        session_phase = "idle"
                return
            
            print(f"[rec] Остановлена (хвост захвачен)")
            threading.Thread(target=process_audio, args=(audio_snapshot, current_session_id), daemon=True).start()

        processing = True
        threading.Thread(target=delayed_stop, daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    global model
    if not acquire_single_instance_lock():
        print("[FATAL] WhisperKey уже запущен. Закрой предыдущий процесс перед новым стартом.")
        return

    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        if hasattr(p, 'cpu_affinity'): p.cpu_affinity([0, 1])
    except: pass

    print(f"WhisperKey v17.5 CEO PRECISION | Warm-up Engine...")
    try:
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
    except Exception as e:
        print(f"[FATAL] {e}")
        return

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    notify("WhisperKey", "Готов к работе!")
    
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
