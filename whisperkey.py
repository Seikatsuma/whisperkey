#!/usr/local/bin/python3.11
"""
WhisperKey v11.7 - CEO ULTIMATE PRECISION
- Архитектура: Precision Tuning (beam_size=5, patience=2.0)
- Качество: Repetition Penalty + Hotwords (идеальные окончания)
- Грамматика: Context-Aware + Smart Refiner
- Надежность: Atomic Clipboard Injection
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
        # 1. Сохраняем текущее содержимое буфера обмена
        old_clipboard = subprocess.run(['pbpaste'], capture_output=True).stdout
        
        # 2. Копируем новый текст и вставляем
        subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
        time.sleep(0.1)
        script = 'tell application "System Events" to key code 9 using command down'
        subprocess.run(["osascript", "-e", script], capture_output=True)
        
        # 3. Даем системе время на вставку и возвращаем старый буфер
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

        max_val = np.max(np.abs(audio))
        if max_val > 0.01: audio = audio / max_val * 0.95

        # CEO Architect Edition: Ультимативный промпт для контроля окончаний.
        context_prompt = (
            f"Внедри. Поправь. Сделай. Посмотри. Проанализируй. Порти. Деплой. "
            f"Это грамотная русская речь, команды для ИИ. "
            f"Соблюдай падежи, склонения и правильные окончания слов. "
            f"Контекст: {last_text_context}. Термины: Claude, CEO to CEO, deploy."
        )
        
        # РАСШИФРОВКА (CEO Quality Edition - Precision Tuning)
        segments, _ = model.transcribe(
            audio,
            language="ru",
            beam_size=5,
            patience=2.0,
            repetition_penalty=1.1,            # Помогает избежать "заиканий" в окончаниях
            hotwords="Claude CEO deploy деплой", # Приоритетные слова
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=400,
                speech_pad_ms=300              # Увеличили до 300мс, чтобы точно не резать хвосты
            ),
            suppress_blank=True,
            without_timestamps=True,
            initial_prompt=context_prompt
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = time.time() - t_start
        print(f"[raw]  '{text}'")
        print(f"[time] {elapsed:.1f}s ({elapsed/dur*100:.0f}% от длины)")
        
        text = clean_noise(text)
        text = smart_grammar_fix(text)

        if text and len(text) > 1:
            if text[0].islower(): text = text[0].upper() + text[1:]
            if text[-1] not in '.!?…': text += '.'
            last_text_context = text[-100:]
            direct_insert(text + " ")
            notify("WhisperKey ✓", "Текст вставлен")
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
            
            # CEO "Low-Latency" Audio Logic: Экстремально быстрое включение микрофона
            audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, 
                channels=1, 
                dtype="float32",
                latency='low',         # Запрос минимальной задержки у macOS
                blocksize=512,         # Минимальный размер буфера для мгновенного старта
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
        
        # Мгновенно останавливаем и закрываем поток микрофона
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
    global model
    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        if hasattr(p, 'cpu_affinity'): p.cpu_affinity([0, 1])
    except: pass

    print(f"WhisperKey v11.7 CEO Ultimate Precision | Final Tuning...")
    try:
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
        print("Разогрев модели (Warm-up)...")
        model.transcribe(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32), language="ru", beam_size=1)
        print("Режим Ultimate Precision: АКТИВИРОВАН")
    except Exception as e:
        print(f"[FATAL] {e}")
        return

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    
    # Мы убрали InputStream из main, теперь он создается динамически в on_press
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

if __name__ == "__main__":
    main()
