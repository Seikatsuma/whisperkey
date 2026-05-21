"""
WhisperKey v10.0 - CEO EDITION
- Архитектура: Atomic Clipboard Injection (решает проблему раскладки)
- Логика: Полное автоопределение языка (RU/EN)
- Надежность: Исправлена вставка длинных текстов
"""

import threading
import subprocess
import os
import sys
import time
import numpy as np
import sounddevice as sd
import re
import platform

# Настройки для Intel Mac
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

from faster_whisper import WhisperModel
from pynput import keyboard

# ─── Настройки ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_SIZE  = "small" 

# ─── Состояние ────────────────────────────────────────────────────────────────
is_recording   = False
recording_data = []
model          = None
processing     = False

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    try:
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'], capture_output=True)
    except Exception: pass

def direct_insert(text: str):
    """CEO Method: Вставка через буфер обмена. Игнорирует раскладку клавиатуры."""
    try:
        # 1. Копируем текст в буфер обмена macOS
        process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
        process.communicate(input=text.encode('utf-8'))
        
        # 2. Небольшая пауза для стабилизации буфера
        time.sleep(0.1)
        
        # 3. Эмулируем Cmd+V (Key code 9 - это 'V')
        # Это работает всегда, независимо от языка ввода
        script = 'tell application "System Events" to key code 9 using command down'
        subprocess.run(["osascript", "-e", script], capture_output=True)
        
        print(f"[insert success] '{text[:20]}...' inserted via Clipboard")
    except Exception as e:
        print(f"[insert error] {e}")

def noise_gate(audio: np.ndarray, threshold: float = 0.02) -> np.ndarray:
    """Отсекаем шум, оставляем только сигнал."""
    if len(audio) == 0: return audio
    if np.abs(audio).mean() < threshold:
        return np.array([], dtype=np.float32)
    return audio

# ─── Транскрибация ────────────────────────────────────────────────────────────

def process_audio(audio_snapshot: list):
    global processing
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten()
        
        # 1. Очистка сигнала
        audio = noise_gate(audio)
        
        if len(audio) / SAMPLE_RATE < 0.4:
            print("[skip] Слишком короткий фрагмент")
            return

        notify("WhisperKey", "Анализирую...")
        t_start = time.time()
        
        # 2. Нормализация уровня
        max_val = np.max(np.abs(audio))
        if max_val > 0.01: audio = audio / max_val * 0.9

        # 3. РАСШИФРОВКА (Уровень: Expert)
        # Убираем жесткую привязку к языку для свободного EN/RU
        segments, _ = model.transcribe(
            audio,
            language=None, 
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            initial_prompt="Смешанная русская и английская речь. Пиши грамотно, расставляй знаки препинания."
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        
        # 4. ФИЛЬТРАЦИЯ ГАЛЛЮЦИНАЦИЙ
        # Удаляем только аномальные повторы (шум)
        text = re.sub(r'[фФfF]{4,}', '', text).strip()
        text = re.sub(r'[.]{3,}', '...', text).strip()

        duration = time.time() - t_start
        print(f"[raw] '{text}'")
        print(f"[time] {duration:.1f}s")

        if text and len(text) > 1:
            # Автоматическое форматирование
            if len(text) > 1: text = text[0].upper() + text[1:]
            if text[-1] not in ['.', '!', '?', '…']: text += '.'
            
            direct_insert(text + " ")
            notify("WhisperKey ✓", "Текст вставлен")
        else:
            notify("WhisperKey", "Не удалось распознать")
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

def on_release(key):
    global is_recording, processing
    if key == TRIGGER_KEY and is_recording:
        is_recording = False
        processing = True
        audio_snapshot = list(recording_data)
        threading.Thread(target=process_audio, args=(audio_snapshot,), daemon=True).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    global model
    print(f"Запуск WhisperKey v10.0 CEO Edition. Модель {MODEL_SIZE}...")
    
    # Загружаем модель с оптимизацией под 2 ядра
    try:
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=2)
    except Exception as e:
        print(f"Ошибка загрузки: {e}")
        sys.exit(1)

    print("Готов! Зажми ПРАВЫЙ OPTION.")
    notify("WhisperKey", "Готов к работе!")
    
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", 
                        callback=lambda d,f,t,s: recording_data.append(d.copy()) if is_recording else None):
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

if __name__ == "__main__":
    main()
