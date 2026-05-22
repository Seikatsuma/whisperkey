#!/usr/local/bin/python3.11
"""
WhisperKey v15.0 - CEO HYBRID INTELLIGENCE
- Архитектура: Dual-Stage Pipeline (Whisper + Llama-3 Refinement)
- Режимы: Оффлайн (локально) / Гибридный (облако + локальный fallback)
- Скорость: Groq LPU Acceleration (~0.5с в облаке)
- Качество: Whisper Large-v3 + LLM-коррекция
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

# Глобальные переменные состояния
GROQ_API_KEY = ""
USE_CLOUD = False

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False
last_text_context = ""  # Буфер для хранения контекста предыдущей фразы
audio_stream = None     # Динамический поток аудио

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """CEO Method: Асинхронное уведомление."""
    try:
        subprocess.Popen(["osascript", "-e", f'display notification "{message}" with title "{title}"'])
    except Exception:
        pass

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
    """CEO Method: Вставка через буфер обмена с восстановлением старого содержимого."""
    try:
        old_clipboard = subprocess.run(['pbpaste'], capture_output=True).stdout
        subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
        time.sleep(0.1)
        script = 'tell application "System Events" to key code 9 using command down'
        subprocess.run(["osascript", "-e", script], capture_output=True)
        time.sleep(0.2)
        process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
        process.communicate(input=old_clipboard)
        print(f"[insert success] '{text[:20]}...' inserted and clipboard restored")
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
    for bad in ["Субтитры", "субтитры", "Продолжение следует", "Спасибо за просмотр"]:
        text = text.replace(bad, "")
    return text.strip()

def check_internet():
    """Проверка наличия интернета."""
    try:
        requests.get("https://api.groq.com", timeout=1)
        return True
    except:
        return False

def compress_audio_mp3(audio_data):
    """Сжатие аудио в MP3 для мгновенной передачи в облако."""
    try:
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())
        wav_data = wav_io.getvalue()

        process = subprocess.Popen(
            ['ffmpeg', '-i', 'pipe:0', '-f', 'mp3', '-acodec', 'libmp3lame', '-ab', '64k', 'pipe:1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        mp3_data, _ = process.communicate(input=wav_data)
        return mp3_data
    except Exception as e:
        print(f"[compress error] {e}")
        return None

def transcribe_cloud_turbo(audio_data):
    """Stage 1: Расшифровка через Groq (Whisper Large-v3)."""
    if not GROQ_API_KEY: return None
    try:
        mp3_data = compress_audio_mp3(audio_data)
        if not mp3_data: return None
        files = {'file': ('audio.mp3', io.BytesIO(mp3_data), 'audio/mp3')}
        headers = {'Authorization': f'Bearer {GROQ_API_KEY}'}
        data = {
            'model': 'whisper-large-v3',
            'language': 'ru',
            'prompt': f"Claude, CEO to CEO. {last_text_context}. Команды для ИИ."
        }
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers, files=files, data=data, timeout=30
        )
        if response.status_code == 200:
            return response.json().get('text', '')
        return None
    except Exception as e:
        print(f"[cloud error] {e}")
        return None

def refine_text_llm(raw_text):
    """Stage 2: Интеллектуальная коррекция текста через Llama-3 (Groq)."""
    if not raw_text or len(raw_text) < 5: return raw_text
    try:
        headers = {
            'Authorization': f'Bearer {GROQ_API_KEY}',
            'Content-Type': 'application/json'
        }
        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {
                    "role": "system", 
                    "content": (
                        "Ты - CEO-ассистент. Твоя задача: исправить ошибки распознавания речи, "
                        "расставить знаки препинания и исправить окончания слов, чтобы текст звучал профессионально. "
                        "НЕ меняй смысл. НЕ добавляй ничего от себя. Выдай ТОЛЬКО исправленный текст."
                    )
                },
                {"role": "user", "content": f"Исправь этот текст: {raw_text}"}
            ],
            "temperature": 0.1
        }
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=10
        )
        if response.status_code == 200:
            refined = response.json()['choices'][0]['message']['content'].strip()
            refined = refined.strip('"')
            return refined
        return raw_text
    except:
        return raw_text

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list):
    global processing, last_text_context
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return

        print(f"[rec] {dur:.1f}s → распознаю...")
        notify("WhisperKey", "Распознаю...")
        t_start = time.time()

        text = None
        mode = "OFFLINE"

        if USE_CLOUD and check_internet():
            print("[mode] Cloud Turbo (Whisper Large-v3)")
            raw_text = transcribe_cloud_turbo(audio)
            if raw_text:
                print("[mode] Neural Refinement (Llama-3)")
                text = refine_text_llm(raw_text)
                mode = "CLOUD+LLM"

        if not text:
            print("[mode] Local Precision Fallback")
            max_val = np.max(np.abs(audio))
            if max_val > 0.01: audio = audio / max_val * 0.95
            context_prompt = (
                f"Внедри. Поправь. Сделай. Посмотри. Проанализируй. Порти. Деплой. "
                f"Это грамотная русская речь, команды для ИИ. "
                f"Соблюдай падежи, склонения и правильные окончания слов. "
                f"Контекст: {last_text_context}. Термины: Claude, CEO to CEO, deploy."
            )
            segments, _ = model.transcribe(
                audio, language="ru", beam_size=5, patience=2.0,
                repetition_penalty=1.1, hotwords="Claude CEO deploy деплой",
                vad_filter=True, vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=300),
                suppress_blank=True, without_timestamps=True, initial_prompt=context_prompt
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()

        elapsed = time.time() - t_start
        print(f"[raw]  '{text}'")
        print(f"[time] {elapsed:.1f}s ({elapsed/dur*100:.0f}% от длины) [{mode}]")
        
        text = clean_noise(text)
        if text and len(text) > 1:
            if text[0].islower(): text = text[0].upper() + text[1:]
            if text[-1] not in '.!?…': text += '.'
            last_text_context = text[-100:]
            direct_insert(text + " ")
            notify("WhisperKey ✓", f"Текст готов [{mode}]")
        else:
            print("[skip] Пустой результат")
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
        try:
            is_recording = True
            recording_data = []
            audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                latency='low', blocksize=512,
                callback=lambda d,f,t,s: recording_data.append(d.copy()) if is_recording else None
            )
            audio_stream.start()
            notify("WhisperKey", "🎙 Запись...")
            print("[rec] Начата")
        except Exception as e:
            print(f"[audio error] {e}")
            is_recording = False

def on_release(key):
    global is_recording, processing, audio_stream
    if is_trigger(key) and is_recording:
        is_recording = False
        if audio_stream:
            audio_stream.stop()
            audio_stream.close()
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
    global model, GROQ_API_KEY, USE_CLOUD

    print("\n--- WhisperKey v15.0 CEO HYBRID ---")
    print("Выберите режим работы:")
    print("1. ОФФЛАЙН (Только локальная модель, интернет не нужен)")
    print("2. ГИБРИДНЫЙ (Облако Groq для скорости + Локальный fallback)")
    
    choice = input("\nВведите 1 или 2: ").strip()
    
    if choice == "2":
        print("\nДля ГИБРИДНОГО режима нужен API ключ Groq.")
        print("Получить бесплатно: https://console.groq.com/keys")
        key = input("Введите ваш API ключ: ").strip()
        if key:
            GROQ_API_KEY = key
            USE_CLOUD = True
            print("\n[OK] Гибридный режим активирован.")
        else:
            print("\n[!] Ключ не введен. Переход в ОФФЛАЙН режим.")
    else:
        print("\n[OK] Оффлайн режим активирован.")

    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        if hasattr(p, 'cpu_affinity'): p.cpu_affinity([0, 1])
    except: pass

    print("\nЗагрузка локальной модели...")
    try:
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
        print("Разогрев...")
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)
        print("Система готова.")
    except Exception as e:
        print(f"[FATAL] {e}")
        return

    print("\nГотов! Зажми ПРАВЫЙ OPTION для записи.")
    
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
