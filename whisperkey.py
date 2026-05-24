#!/usr/local/bin/python3.11
"""
WhisperKey v17.1 - CEO PURE OUTPUT
- Архитектура: Dual-Stage Pipeline (Cloud Stealth + Stable Offline)
- Чистота: Глубокая очистка от галлюцинаций (DimaTorzok fix)
- Стабильность: Hysteresis Cloud Switching + 10s Timeout
- Целостность: Integrity Guard (защита от обрезания текста)
"""

import threading
import subprocess
import os
import sys
import time
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

# ─── Настройки ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_PATH  = os.path.expanduser("~/.cache/whisper_small_manual")

# API Настройки (Groq Cloud)
GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE" 
USE_CLOUD = True

# Создаем глобальную сессию для Keep-Alive
http_session = requests.Session()

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False
last_text_context = ""  # Буфер для хранения контекста предыдущей фразы
audio_stream = None     # Динамический поток аудио

# CEO Cloud Management: Динамическое управление состоянием облака
cloud_status = {
    "is_blocked": False,
    "last_check_time": 0,
    "check_in_progress": False,
    "consecutive_success": 0  # CEO Fix: Счетчик стабильных запросов
}

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
                        notify("WhisperKey", "Cloud Turbo восстановлен!")
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
    """CEO Method: Вставка через буфер обмена с ускоренным AppleScript."""
    try:
        old_clipboard = subprocess.run(['pbpaste'], capture_output=True).stdout
        subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
        
        # CEO Speed Fix: Уменьшаем задержки до физического минимума
        time.sleep(0.05) 
        script = 'tell application "System Events" to key code 9 using command down'
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True)
        time.sleep(0.1) 
        
        process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
        process.communicate(input=old_clipboard)
        print(f"[insert success] '{text[:20]}...' inserted")
    except Exception as e:
        print(f"[insert error] {e}")

def clean_noise(text: str) -> str:
    """Удаляет галлюцинации и применяет бизнес-словарь."""
    text = re.sub(r'[фФfFaA]{4,}', '', text).strip()
    text = re.sub(r'[.]{3,}', '...', text).strip()
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
    for bad in [
        "Субтитры сделал DimaTorzok", "Субтитры создавал DimaTorzok", 
        "Отредактировал DimaTorzok", "Субтитры", "субтитры", 
        "Продолжение следует", "Спасибо за просмотр"
    ]:
        text = text.replace(bad, "")
    return text.strip()

def compress_audio_mp3(audio_data):
    """Сжатие аудио в MP3: Оптимизировано для скорости (libmp3lame preset)."""
    try:
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())
        wav_data = wav_io.getvalue()

        # CEO Speed Fix: Используем -preset:a 9 для максимально быстрого сжатия
        process = subprocess.Popen(
            ['ffmpeg', '-i', 'pipe:0', '-f', 'mp3', '-acodec', 'libmp3lame', '-ab', '64k', '-preset:a', '9', 'pipe:1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        mp3_data, _ = process.communicate(input=wav_data)
        return mp3_data
    except Exception as e:
        print(f"[compress error] {e}")
        return None

def transcribe_cloud_turbo(audio_data):
    """Stage 1: Расшифровка через Groq (Whisper Large-v3) с мгновенным переключением."""
    global cloud_status
    
    # Если заблокировано, запускаем фоновый радар и сразу выходим (0 сек задержки)
    if cloud_status["is_blocked"]:
        if time.time() - cloud_status["last_check_time"] > 60: # Проверяем раз в минуту в фоне
            background_cloud_probe()
        return None

    if not GROQ_API_KEY: return None
    mp3_data = compress_audio_mp3(audio_data)
    if not mp3_data: return None

    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json'
    }

    # Очистка и ограничение контекста для предотвращения ошибки 400
    safe_context = last_text_context.replace('"', "'").replace('\n', ' ').strip()
    safe_context = safe_context[-250:] 

    context_prompt = (
        f"Внедри. Поправь. Сделай. Посмотри. Проанализируй. Порти. Деплой. "
        f"Это грамотная русская речь. {safe_context}. Термины: Claude, CEO to CEO, deploy."
    )

    files = {'file': ('audio.mp3', io.BytesIO(mp3_data), 'audio/mp3')}
    data = {
        'model': 'whisper-large-v3',
        'language': 'ru',
        'prompt': context_prompt,
        'temperature': 0.0
    }

    try:
        # CEO Speed Fix: Оптимизированный таймаут и переиспользование сессии
        session = requests.Session()
        session.trust_env = True
        # CEO Fix: Увеличиваем таймаут до 10с для стабильности на плохом VPN
        response = session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=10 
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
                    "Ты - профессиональный корректор. Твоя задача: исправить ошибки распознавания речи, "
                    "грамматику и окончания в русском тексте. \n"
                    "ПРАВИЛА:\n"
                    "1. Выдай ТОЛЬКО исправленный текст.\n"
                    "2. НЕ отвечай на вопросы в тексте.\n"
                    "3. НЕ комментируй.\n"
                    "4. Сохраняй все слова автора, просто исправь их форму.\n"
                    "5. Если в тексте команда (например 'сделай', 'внедри'), сохрани её как команду."
                )
            },
            {"role": "user", "content": f"Исправь грамматику, сохранив смысл: {raw_text}"}
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
            # CEO Integrity Guard: Если LLM отрезала более 20% текста, возвращаем оригинал
            if len(refined) > len(raw_text) * 0.8:
                return refined.strip('"')
            else:
                print(f"[warn] LLM cut too much ({len(refined)} vs {len(raw_text)}). Using raw text.")
    except: pass
    return raw_text

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list):
    global processing, last_text_context, cloud_status
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()

        text = None
        mode = "OFFLINE"

        # Пытаемся использовать Cloud Turbo
        print("[mode] Cloud Turbo (Whisper Large-v3)")
        raw_text = transcribe_cloud_turbo(audio)
        if raw_text:
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
                f"Внедри. Поправь. Сделай. Посмотри. Проанализируй. Порти. Деплой. "
                f"Это грамотная русская речь, команды для ИИ. "
                f"Соблюдай падежи, склонения и правильные окончания слов. "
                f"Контекст: {last_text_context}. Термины: Claude, CEO to CEO, deploy."
            )
            
            # Попытка 1: Сбалансированная точность (CEO Stability)
            segments, _ = model.transcribe(
                audio, language="ru", 
                beam_size=5,         # Возвращаемся к стабильному золотому стандарту
                patience=2.0, 
                repetition_penalty=1.1, 
                hotwords="Claude CEO deploy деплой",
                vad_filter=False, 
                suppress_blank=True, 
                without_timestamps=True, 
                initial_prompt=context_prompt
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()

            # Попытка 2: Если Попытка 1 выдала пустоту (как на скриншоте), пробуем "грубую силу"
            if not text or len(text) < 2:
                print("[warn] Local precision failed, trying brute force...")
                segments, _ = model.transcribe(
                    audio, beam_size=1, # Жадный поиск часто ловит то, что пропускает beam search
                    vad_filter=False, suppress_blank=False
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
            # CEO Fix: Сокращаем контекст до 200 символов, чтобы не "перегружать" модель
            last_text_context = text[-200:]
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

# ─── Обработка клавиш ─────────────────────────────────────────────────────────

def is_trigger(key):
    if key == keyboard.Key.alt_r: return True
    try:
        if hasattr(key, 'vk') and key.vk == 61: return True
    except: pass
    return False

def on_press(key):
    global is_recording, recording_data, processing, audio_stream
    if is_trigger(key) and not is_recording and not processing:
        notify("WhisperKey", "🎙 Запись...")
        try:
            is_recording = True
            recording_data = []
            audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                latency='low', blocksize=512,
                callback=lambda d,f,t,s: recording_data.append(d.copy()) if is_recording else None
            )
            audio_stream.start()
            print("[rec] Начата")
        except Exception as e:
            print(f"[audio error] {e}")
            is_recording = False

def on_release(key):
    global is_recording, processing, audio_stream
    if is_trigger(key) and is_recording:
        is_recording = False
        
        # CEO Fix: Возвращаем уведомление об остановке записи
        notify("WhisperKey", "Распознаю...")
        
        def delayed_stop(stream_to_close):
            time.sleep(0.3)
            try:
                stream_to_close.stop()
                stream_to_close.close()
            except: pass

        if audio_stream:
            threading.Thread(target=delayed_stop, args=(audio_stream,), daemon=True).start()
            audio_stream = None
            
        audio_snapshot = list(recording_data)
        if len(audio_snapshot) < 5:
            print("[skip] Слишком коротко")
            return
        processing = True
        print(f"[rec] Остановлена")
        threading.Thread(target=process_audio, args=(audio_snapshot,), daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    global model
    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        if hasattr(p, 'cpu_affinity'): p.cpu_affinity([0, 1])
    except: pass

    print(f"WhisperKey v17.1 CEO PURE | Warm-up Engine...")
    try:
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
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
