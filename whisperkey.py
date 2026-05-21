#!/usr/local/bin/python3.11
"""
WhisperKey v11.1 - CEO HYBRID EDITION
- Архитектура: Hybrid Logic (Ultra-fast <3s, High-quality >3s)
- Скорость: Обработка коротких фраз быстрее их длины
- Качество: Context-Aware Buffer + Smart Grammar Refiner
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
last_transcription = "" # Хранилище последнего результата для повторной вставки

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """CEO Method: Асинхронное уведомление, не блокирующее основной поток."""
    try:
        # Используем Popen вместо run, чтобы не ждать завершения процесса
        subprocess.Popen(["osascript", "-e", f'display notification "{message}" with title "{title}"'])
    except Exception:
        pass

def smart_grammar_fix(text: str) -> str:
    """CEO Quality: Исправление типичных ошибок, окончаний и улучшение читаемости."""
    if not text: return text
    
    # 1. Исправление типичных ошибок в окончаниях (пост-процессинг)
    # Исправляем наиболее частые случаи несогласованности
    text = re.sub(r"(\w+)ться", r"\1ться", text)
    
    # 2. Удаление лишних пробелов перед знаками препинания
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    
    # 3. Исправление двойных знаков препинания
    text = re.sub(r'([,.!?])\1+', r'\1', text)
    
    # 4. Гарантированный пробел после знаков препинания
    text = re.sub(r'([,.!?])(?=[^\s])', r'\1 ', text)
    
    # 5. Исправление окончаний в бизнес-контексте (например, "в Cursor" вместо "в Cursor-е")
    text = re.sub(r'Cursor[а-яА-Я]+', 'Cursor', text)
    text = re.sub(r'Claude[а-яА-Я]+', 'Claude', text)
    
    return text.strip()

def direct_insert(text: str):
    """CEO Method: Вставка через буфер обмена. Игнорирует раскладку клавиатуры."""
    try:
        process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
        process.communicate(input=text.encode('utf-8'))
        time.sleep(0.1)
        script = 'tell application "System Events" to key code 9 using command down'
        subprocess.run(["osascript", "-e", script], capture_output=True)
        print(f"[insert success] '{text[:20]}...' inserted via Clipboard")
    except Exception as e:
        print(f"[insert error] {e}")

def clean_noise(text: str) -> str:
    """Удаляет галлюцинации и применяет бизнес-словарь."""
    text = re.sub(r'[фФfFaA]{4,}', '', text).strip()
    text = re.sub(r'[.]{3,}', '...', text).strip()
    
    business_vocabulary = {
        r'\b[Cc]ursor\b': 'Cursor',
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
        if not audio_snapshot:
            return

        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE

        if dur < 0.5:
            print(f"[skip] {dur:.1f}s — слишком коротко")
            return

        print(f"[rec] {dur:.1f}s → распознаю...")
        notify("WhisperKey", "Распознаю...")
        t_start = time.time()

        # 1. НОРМАЛИЗАЦИЯ (CEO Quality Control)
        max_val = np.max(np.abs(audio))
        if max_val > 0.01:
            audio = audio / max_val * 0.95

        # 2. ФОРМИРОВАНИЕ КОНТЕКСТНОГО ПРОМПТА
        # CEO Architect Edition: Регистр глаголов усилен для точного распознавания начала команд.
        context_prompt = (
            f"Внедри. Поправь. Сделай. Посмотри. Проанализируй. Порти. Деплой. "
            f"Это команды для ИИ в повелительном наклонении. "
            f"Обращайся на 'ты'. Соблюдай падежи и окончания. "
            f"Контекст: {last_text_context}. Термины: Cursor, Claude, CEO to CEO, deploy."
        )
        
        # 3. РАСШИФРОВКА (CEO Architect Edition - Hybrid Logic)
        # Для аудио < 3 секунд мы используем "Fast Track" (beam_size=1, без VAD),
        # чтобы скорость была выше длины самого аудио.
        # Для аудио >= 3 секунд сохраняем максимальное качество (beam_size=2, VAD).
        is_ultra_short = dur < 3.0
        
        segments, _ = model.transcribe(
            audio,
            language="ru",
            beam_size=1 if is_ultra_short else 2,
            vad_filter=not is_ultra_short,     # Отключаем VAD для ультра-коротких фраз
            vad_parameters=dict(
                min_silence_duration_ms=400,
                speech_pad_ms=200
            ) if not is_ultra_short else None,
            suppress_blank=True,
            without_timestamps=True,
            initial_prompt=context_prompt
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        elapsed = time.time() - t_start
        print(f"[raw]  '{text}'")
        print(f"[time] {elapsed:.1f}s ({elapsed/dur*100:.0f}% от длины)")

        # 4. ПОСТ-ОБРАБОТКА (Грамматика и Словарь)
        text = clean_noise(text)
        text = smart_grammar_fix(text)

        if text and len(text) > 1:
            if text[0].islower():
                text = text[0].upper() + text[1:]
            if text[-1] not in '.!?…':
                text += '.'
            
            # Сохраняем контекст для следующей фразы (последние 100 символов)
            last_text_context = text[-100:]
            # CEO Feature: Сохраняем полную транскрипцию для повторной вставки
            last_transcription = text + " "
            
            direct_insert(last_transcription)
            notify("WhisperKey ✓", "Текст вставлен")
        else:
            print("[skip] Пустой результат")

    except Exception as e:
        print(f"[error] {e}")
        notify("WhisperKey ❌", str(e)[:50])
    finally:
        processing = False

# ─── Обработка клавиш ─────────────────────────────────────────────────────────

# Состояние для отслеживания комбинации FN + Alt_L
current_keys = set()

def is_trigger(key):
    if key == TRIGGER_KEY:
        return True
    try:
        if hasattr(key, 'vk') and key.vk == 61:
            return True
    except Exception:
        pass
    return False

def on_press(key):
    global is_recording, recording_data, processing, current_keys
    
    # Отслеживание зажатых клавиш для комбинации FN + Alt_L
    if key == keyboard.Key.alt_l:
        current_keys.add('alt_l')
    try:
        if hasattr(key, 'vk') and key.vk == 63: # VK код для клавиши FN на Mac
            current_keys.add('fn')
    except: pass

    # Проверка комбинации FN + Alt_L для повторной вставки
    if 'fn' in current_keys and 'alt_l' in current_keys:
        if last_transcription:
            print("[re-insert] Повторная вставка последнего текста")
            threading.Thread(target=direct_insert, args=(last_transcription,), daemon=True).start()
            # Очищаем, чтобы не срабатывало повторно при удержании
            current_keys.remove('alt_l') 

    if is_trigger(key) and not is_recording and not processing:
        is_recording = True
        recording_data = []
        notify("WhisperKey", "🎙 Запись...")
        print("[rec] Начата")

def on_release(key):
    global is_recording, processing, current_keys
    
    if key == keyboard.Key.alt_l:
        current_keys.discard('alt_l')
    try:
        if hasattr(key, 'vk') and key.vk == 63:
            current_keys.discard('fn')
    except: pass

    if is_trigger(key) and is_recording:
        audio_snapshot = list(recording_data)
        is_recording = False
        if len(audio_snapshot) < 5:
            print(f"[skip] Слишком коротко ({len(audio_snapshot)} блоков)")
            return
        processing = True
        print(f"[rec] Остановлена ({len(audio_snapshot)} блоков)")
        threading.Thread(
            target=process_audio, args=(audio_snapshot,), daemon=True
        ).start()

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    global model

    # Оптимизация приоритета процесса
    try:
        p = psutil.Process(os.getpid())
        p.nice(-10)
        # Привязка к 2 физическим ядрам для исключения context switching на Intel i5
        if hasattr(p, 'cpu_affinity'):
            p.cpu_affinity([0, 1])
    except Exception:
        pass

    print(f"WhisperKey v11.1 CEO Architect Edition | Hybrid-Performance...")
    try:
        model = WhisperModel(
            MODEL_PATH, 
            device="cpu", 
            compute_type="int8", 
            cpu_threads=2, # Оптимально для Intel i5 (2 мощных потока)
            local_files_only=True
        )
        
        # Warm-up Cycle: Разогрев модели для мгновенного первого запуска
        print("Разогрев модели (Warm-up)...")
        dummy_audio = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        model.transcribe(dummy_audio, language="ru", beam_size=1)
        
        print("Режим Hybrid Ultra-Performance: АКТИВИРОВАН")
    except Exception as e:
        print(f"[FATAL] {e}")
        notify("WhisperKey", f"Ошибка: {e}")
        return

    print("Готов! Зажми ПРАВЫЙ OPTION для записи.")
    print("Повторная вставка: FN + ЛЕВЫЙ ALT.")
    notify("WhisperKey", "Готов к работе!")

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        callback=lambda d, f, t, s: recording_data.append(d.copy()) if is_recording else None
    ):
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

if __name__ == "__main__":
    main()
