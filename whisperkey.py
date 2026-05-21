#!/usr/bin/env python3
"""
WhisperKey v8.3 - CROSS-PLATFORM
Поддержка: macOS, Windows, Linux
"""

import threading
import subprocess
import os
import sys
import time
import numpy as np
import sounddevice as sd
import platform

# Оптимизации для Intel
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

from faster_whisper import WhisperModel
from pynput import keyboard

# ─── Настройки ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_SIZE  = "small" 
LANGUAGE    = "ru"

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """Универсальные уведомления."""
    system = platform.system()
    try:
        if system == "Darwin": # macOS
            subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'], capture_output=True)
        elif system == "Windows":
            # Требует pip install win10toast (опционально)
            print(f"[{title}] {message}")
        else: # Linux
            subprocess.run(["notify-send", title, message], capture_output=True)
    except Exception:
        print(f"[{title}] {message}")

def direct_insert(text: str):
    """Универсальная вставка текста."""
    system = platform.system()
    try:
        if system == "Darwin": # macOS
            safe_text = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')
            script = f'''tell application "System Events"
                set frontmostProcess to first process whose frontmost is true
                tell frontmostProcess
                    keystroke "{safe_text}"
                end tell
            end tell'''
            subprocess.run(["osascript", "-e", script], capture_output=True)
        else:
            # Для Windows и Linux используем контроллер клавиатуры pynput
            kb = keyboard.Controller()
            kb.type(text)
    except Exception as e:
        print(f"[insert error] {e}")

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list):
    global processing
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten()
        if len(audio) / SAMPLE_RATE < 0.4: return

        notify("WhisperKey", "Распознаю...")
        t_start = time.time()
        
        segments, _ = model.transcribe(
            audio,
            language=LANGUAGE,
            beam_size=2,
            vad_filter=True,
            initial_prompt="Это качественная русская речь. English words allowed."
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = time.time() - t_start
        print(f"[timing] {elapsed:.2f}s")

        if text:
            if len(text) > 1: text = text[0].upper() + text[1:]
            if text[-1] not in ['.', '!', '?', '…']: text += '.'
            direct_insert(text + " ")
            notify("WhisperKey ✓", text[:50] + "...")
    except Exception as e:
        print(f"[error] {e}")
    finally:
        processing = False

# ─── Обработка клавиш ─────────────────────────────────────────────────────────

def on_press(key):
    global is_recording, recording_data, processing
    if key == TRIGGER_KEY and not is_recording and not processing:
        is_recording = True
        recording_data = []
        notify("WhisperKey", "🎙 Запись...")
        print("[recording] Начата")

def on_release(key):
    global is_recording, processing
    if key == TRIGGER_KEY and is_recording:
        is_recording = False
        processing = True
        print("[recording] Остановлена")
        audio_snapshot = list(recording_data)
        threading.Thread(target=process_audio, args=(audio_snapshot,), daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    global model
    print(f"Запуск WhisperKey v8.3 ({platform.system()})...")
    
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=2)
    
    print("Готов! Зажми ПРАВЫЙ OPTION.")
    notify("WhisperKey", "Готов к работе!")
    
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", 
                        callback=lambda d,f,t,s: recording_data.append(d.copy()) if is_recording else None):
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

if __name__ == "__main__":
    main()
