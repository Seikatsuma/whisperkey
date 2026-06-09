#!/usr/bin/env python3
"""
WhisperKey v23.5 - Windows Edition
- Архитектура: Dual-Stage Pipeline (Cloud Stealth + Stable Offline)
- Качество: Context-Aware Grammar (возврат идеальных окончаний)
- Целостность: Fast Tail Capture (300ms) + VAD Shield (1000ms)
- Стабильность: Windows Native Compatibility
"""
from __future__ import annotations

import threading
import subprocess
import os
import sys
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import re
import psutil
import requests
import io
import wave
import pyperclip

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
SAMPLE_RATE = 16000
TRIGGER_KEY = keyboard.Key.alt_r
MODEL_PATH  = "small" 
TAIL_CAPTURE_SECONDS = 0.6  
RESTORE_CLIPBOARD = True
SAVE_DEBUG_AUDIO = False  
ASR_CONTEXT_PROMPT = "Русская деловая речь. IT, программирование, технические задачи."
NARRATOR_LOOP_PATTERN = r'(?:спикер|смикер|speaker)\s+говорит'
BOH_TAIL_MARKERS = [
    "редактор субтитров",
    "корректор",
    "продолжение следует",
    "субтитры сделал",
    "субтитры подогнал",
    "subtitles by",
    "thanks for watching"
]
CLOUD_WHISPER_MODEL = "whisper-large-v3"
PARALLEL_CLOUD_CHUNKS = True
MAX_CLOUD_WORKERS = 4
LLAMA_SKIP_MAX_WORDS = 0
HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "продолжение следует",
    "голос за кадром",
]

# API Настройки (Groq Cloud)
load_env_file(".env")
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
TRIGGER_DEBOUNCE_SEC = 0.35
session_counter = 0
state_lock = threading.Lock()
session_phase = "idle"   
active_session_id = 0
audio_stream = None 

# CEO Cloud Management
cloud_status = {
    "is_blocked": False,
    "last_check_time": 0,
    "check_in_progress": False,
    "consecutive_success": 0  
}
kb = KeyboardController()
_instance_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """Windows version: Гарантирует один активный процесс через файл-флаг."""
    lock_path = "whisperkey.lock"
    try:
        if os.path.exists(lock_path):
            # Проверяем, живой ли процесс
            try:
                with open(lock_path, "r") as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    return False
            except:
                pass
        
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))
        return True
    except:
        return False

def audio_callback(indata, frames, time_info, status):
    if is_recording:
        recording_data.append(indata.copy())

def start_audio_stream():
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
    global cloud_status
    if cloud_status["check_in_progress"]: return
    
    def probe():
        cloud_status["check_in_progress"] = True
        try:
            headers = {'Authorization': f'Bearer {GROQ_API_KEY}'}
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

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """Windows version: Уведомление через консоль (самый надежный метод)."""
    print(f"\n>>> {title}: {message}")

def smart_grammar_fix(text: str) -> str:
    if not text: return text
    text = re.sub(r"(\w+)ться", r"\1ться", text)
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    text = re.sub(r'([,.!?])\1+', r'\1', text)
    text = re.sub(r'([,.!?])(?=[^\s])', r'\1 ', text)
    text = re.sub(r'Claude[а-яА-Я]+', 'Claude', text)
    return text.strip()

_INCOMPLETE_ENDING_RE = re.compile(
    r'\b(?:и|а|но|или|либо|чтобы|что|как|если|when|where|'
    r'который|которая|которое|которые|которых|которому|которой|'
    r'при|для|на|в|во|с|со|у|о|об|от|до|без|через|про|над|под|'
    r'перед|после|между|среди|по|к|ко|из)\s*$',
    re.IGNORECASE,
)

def apply_smart_sentence_ending(text: str) -> str:
    if not text or len(text) <= 1:
        return text
    if text[0].islower():
        text = text[0].upper() + text[1:]
    stripped = text.rstrip()
    if not stripped:
        return text
    if stripped[-1] in '.!?…':
        return stripped
    tail_check = stripped.rstrip('.,;:')
    if _INCOMPLETE_ENDING_RE.search(tail_check):
        return stripped
    return stripped + '.'

def _restore_clipboard_async(old_clipboard: str) -> None:
    def run_restore():
        try:
            time.sleep(0.5)
            pyperclip.copy(old_clipboard)
        except: pass
    threading.Thread(target=run_restore, daemon=True).start()

def direct_insert(text: str):
    """Windows version: Вставка через Ctrl+V."""
    try:
        old_clipboard = pyperclip.paste()
        pyperclip.copy(text)
        time.sleep(0.1) 
        
        kb.press(KeyboardKey.ctrl)
        kb.press('v')
        kb.release('v')
        kb.release(KeyboardKey.ctrl)
        
        print(f"[insert success] '{text[:30]}...'")
        if RESTORE_CLIPBOARD:
            _restore_clipboard_async(old_clipboard)
    except Exception as e:
        print(f"[insert error] {e}")

def strip_asr_artifacts(text: str) -> str:
    cleaned = text.strip()
    if not cleaned: return cleaned
    loop_matches = list(re.finditer(NARRATOR_LOOP_PATTERN, cleaned, flags=re.IGNORECASE))
    if len(loop_matches) >= 2:
        cut_pos = loop_matches[0].start()
        cleaned = cleaned[:cut_pos].strip()
    if not cleaned: return cleaned
    lower = cleaned.lower()
    tail_start = int(len(lower) * 0.7)
    for marker in BOH_TAIL_MARKERS:
        idx = lower.find(marker, tail_start)
        if idx != -1:
            cleaned = cleaned[:idx].strip()
            lower = cleaned.lower()
            tail_start = int(len(lower) * 0.7)
    return cleaned.strip()

def should_skip_llm(raw_text: str) -> bool:
    words = raw_text.split()
    if len(words) > LLAMA_SKIP_MAX_WORDS:
        return False
    lower = raw_text.lower()
    if any(t in lower for t in HALLUCINATION_TRIGGERS):
        return False
    if any(m in lower for m in BOH_TAIL_MARKERS):
        return False
    if len(re.findall(NARRATOR_LOOP_PATTERN, raw_text, flags=re.IGNORECASE)) >= 2:
        return False
    return True

def clean_noise(text: str) -> str:
    if not text: return ""
    text = strip_asr_artifacts(text)
    if not text: return ""
    hallucination_words = ["КОНЕЦ", "Конец", "Конец связи", "Cursor", "Python", "CEO to CEO"]
    if text.strip() in hallucination_words: return ""
    text = re.sub(r'[фФfFaA]{4,}', '', text).strip()
    text = re.sub(r'[.]{3,}', '...', text).strip()
    bad_endings = [r"Cursor[.!?]*$", r"Python[.!?]*$", r"CEO to CEO[.!?]*$", r"Claude[.!?]*$"]
    for pattern in bad_endings:
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
        r'\b[Dd]eplo\b': 'deploy',
    }
    for pattern, replacement in business_vocabulary.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text.strip()

def compress_silence(audio_data, threshold=0.01, min_pause=1.5, keep_pause=0.5):
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

def create_audio_wav(audio_data):
    try:
        audio_data = compress_silence(audio_data)
        silence_padding = np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32)
        audio_data = np.append(audio_data, silence_padding)
        wav_io = io.BytesIO()
        with wave.open(wav_io, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            max_v = np.max(np.abs(audio_data))
            if max_v > 0: audio_data = audio_data / max_v
            wf.writeframes((audio_data * 32767).astype(np.int16).tobytes())
        return wav_io.getvalue()
    except Exception as e:
        print(f"[wav error] {e}")
        return None

def transcribe_cloud_turbo(audio_data):
    global cloud_status
    if cloud_status["is_blocked"]:
        if time.time() - cloud_status["last_check_time"] > 60:
            background_cloud_probe()
        return None
    if not GROQ_API_KEY: return None
    max_val = np.max(np.abs(audio_data))
    if max_val > 0.0001: audio_data = audio_data / max_val * 0.98
    wav_data = create_audio_wav(audio_data)
    if not wav_data: return None
    headers = {'Authorization': f'Bearer {GROQ_API_KEY}'}
    files = {'file': ('audio.wav', io.BytesIO(wav_data), 'audio/wav')}
    data = {
        'model': CLOUD_WHISPER_MODEL,
        'language': 'ru',
        'prompt': ASR_CONTEXT_PROMPT,
        'temperature': 0.0,
        'response_format': 'verbose_json'
    }
    try:
        response = http_session.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data, timeout=15)
        if response.status_code == 200:
            result = response.json()
            segments = result.get('segments', [])
            valid_text = []
            has_sentence_ending = False
            for seg in segments:
                seg_text = seg.get('text', '').strip()
                if not seg_text: continue
                no_speech_prob = seg.get('no_speech_prob', 0.0)
                if no_speech_prob > 0.6 and len(seg_text) < 15: continue
                if has_sentence_ending and no_speech_prob > 0.65: continue
                valid_text.append(seg_text)
                if re.search(r'[.!?…]\s*$', seg_text): has_sentence_ending = True
            return " ".join(valid_text) if valid_text else result.get('text', '')
        if response.status_code == 403:
            cloud_status["is_blocked"] = True
            cloud_status["last_check_time"] = time.time()
            background_cloud_probe()
            return None
    except: pass
    return None

def refine_text_llm(raw_text):
    if not raw_text or len(raw_text) < 5: return raw_text
    context_tail = last_text_context[-40:] if last_text_context else ""
    headers = {'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'}
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {
                "role": "system", 
                "content": (
                    "Ты - эксперт-лингвист. Твоя цель: превратить сырой ASR-текст в безупречный.\n"
                    "ПРАВИЛА:\n1. Исправляй фонетику.\n2. СТРОГИЙ ЗАПРЕТ на выдумку фамилий.\n"
                    "3. НЕ добавляй контекст.\n4. Восстанавливай падежи и пунктуацию.\n"
                    "5. Если фраза обрывается - не дописывай.\nВыдай ТОЛЬКО чистый текст."
                )
            },
            {"role": "user", "content": f"Контекст: ...{context_tail}\nСырой текст ASR: {raw_text}"}
        ],
        "temperature": 0.0
    }
    try:
        response = http_session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            refined = response.json()['choices'][0]['message']['content'].strip()
            words_raw = len(raw_text.split())
            words_refined = len(refined.split())
            if words_refined >= words_raw - 1 and words_refined >= words_raw * 0.9:
                return refined.strip('"')
    except: pass
    return raw_text

def _transcribe_local(chunk: np.ndarray) -> str:
    max_val = np.max(np.abs(chunk))
    if max_val > 0.0001: chunk = chunk / max_val * 0.99
    segments, _ = model.transcribe(chunk, language="ru", beam_size=5, initial_prompt=ASR_CONTEXT_PROMPT)
    return " ".join(seg.text.strip() for seg in segments).strip()

def _transcribe_one_chunk(chunk: np.ndarray, chunk_idx: int, n_chunks: int) -> tuple[int, str | None]:
    chunk_text = None
    chunk_dur = len(chunk) / SAMPLE_RATE
    use_cloud = CLOUD_ENABLED and not cloud_status.get("is_blocked")
    if use_cloud:
        raw_text = transcribe_cloud_turbo(chunk)
        if raw_text:
            lower = raw_text.lower()
            if any(t in lower for t in HALLUCINATION_TRIGGERS):
                cleaned = strip_asr_artifacts(raw_text)
                if cleaned and (len(cleaned.split()) >= 3 or chunk_dur <= 5.0): chunk_text = cleaned
            elif len(raw_text.split()) < 3 and chunk_dur > 5.0: pass
            else: chunk_text = raw_text
    if not chunk_text:
        print(f"[local] Сегмент {chunk_idx + 1}: offline decode")
        chunk_text = _transcribe_local(chunk) or None
    return chunk_idx, chunk_text

def process_audio(audio_snapshot: list, session_id: int):
    global processing, last_text_context, cloud_status, session_phase
    try:
        if not audio_snapshot: return
        audio = np.concatenate(audio_snapshot, axis=0).flatten().astype(np.float32)
        dur = len(audio) / SAMPLE_RATE
        if dur < 0.5: return
        print(f"[rec] {dur:.1f}s → распознаю...")
        t_start = time.time()
        audio_chunks = []
        if dur > 30.0:
            current_pos = 0
            chunk_size_samples = int(SAMPLE_RATE * 20)
            while current_pos < len(audio):
                end_pos = min(current_pos + chunk_size_samples, len(audio))
                if end_pos < len(audio):
                    search_window = int(SAMPLE_RATE * 3)
                    search_start = max(end_pos - search_window, current_pos + int(SAMPLE_RATE * 5))
                    search_end = min(end_pos + search_window, len(audio) - int(SAMPLE_RATE * 1))
                    sub_audio = audio[search_start:search_end]
                    if len(sub_audio) > SAMPLE_RATE:
                        win = int(SAMPLE_RATE * 0.1)
                        energies = [np.max(np.abs(sub_audio[j:j+win])) for j in range(0, len(sub_audio)-win, win)]
                        if energies:
                            min_energy_idx = np.argmin(energies)
                            end_pos = search_start + (min_energy_idx * win) + (win // 2)
                audio_chunks.append(audio[current_pos:end_pos])
                current_pos = end_pos
        else: audio_chunks = [audio]
        n_chunks = len(audio_chunks)
        ordered_parts = [None] * n_chunks
        if PARALLEL_CLOUD_CHUNKS and n_chunks > 1 and CLOUD_ENABLED and not cloud_status.get("is_blocked"):
            with ThreadPoolExecutor(max_workers=MAX_CLOUD_WORKERS) as pool:
                futures = [pool.submit(_transcribe_one_chunk, chunk, idx, n_chunks) for idx, chunk in enumerate(audio_chunks)]
                for fut in as_completed(futures):
                    idx, chunk_text = fut.result()
                    if chunk_text: ordered_parts[idx] = chunk_text
        else:
            for idx, chunk in enumerate(audio_chunks):
                _, chunk_text = _transcribe_one_chunk(chunk, idx, n_chunks)
                if chunk_text: ordered_parts[idx] = chunk_text
        final_segments = [p for p in ordered_parts if p]
        full_raw_text = " ".join(final_segments).strip()
        if not full_raw_text:
            notify("WhisperKey", "Речь не распознана")
            return
        if should_skip_llm(full_raw_text): text = full_raw_text
        else: text = refine_text_llm(full_raw_text)
        text = clean_noise(text)
        text = smart_grammar_fix(text)
        if text and len(text) > 1:
            text = apply_smart_sentence_ending(text)
            print(f"\n--- ФИНАЛЬНЫЙ ТЕКСТ ---\n{text}\n-----------------------\n")
            last_text_context = text[-40:]
            direct_insert(text + " ")
            notify("WhisperKey", "Текст готов")
        else: notify("WhisperKey", "Речь не распознана")
    except Exception as e: print(f"[error] {e}")
    finally:
        processing = False
        with state_lock:
            if active_session_id == session_id: session_phase = "idle"

def is_trigger(key):
    if key == keyboard.Key.alt_r: return True
    return False

def on_press(key):
    global is_recording, recording_data, processing, trigger_held, last_trigger_ts, session_counter, active_session_id, session_phase
    now = time.time()
    if is_trigger(key) and not trigger_held:
        if now - last_trigger_ts < TRIGGER_DEBOUNCE_SEC: return
        with state_lock:
            if session_phase != "idle": return
            last_trigger_ts = now
            trigger_held = True
            session_counter += 1
            active_session_id = session_counter
            session_phase = "recording"
            notify("WhisperKey", "🎙 Запись...")
            try:
                start_audio_stream()
                is_recording = True
                recording_data = []
                if USE_CLOUD:
                    def warm_groq():
                        try: http_session.options("https://api.groq.com/openai/v1/audio/transcriptions", timeout=1.0)
                        except: pass
                    threading.Thread(target=warm_groq, daemon=True).start()
            except:
                is_recording = False
                session_phase = "idle"

def on_release(key):
    global is_recording, processing, trigger_held, session_counter, session_phase
    if is_trigger(key): trigger_held = False
    if is_trigger(key) and is_recording:
        with state_lock:
            if session_phase != "recording": return
            current_session_id = active_session_id
            session_phase = "processing"
        def delayed_stop():
            time.sleep(TAIL_CAPTURE_SECONDS)
            global is_recording
            is_recording = False 
            stop_audio_stream()
            audio_snapshot = list(recording_data)
            if len(audio_snapshot) < 10:
                notify("WhisperKey", "⚠️ Слишком короткая запись")
                global processing
                processing = False
                with state_lock:
                    if active_session_id == current_session_id: session_phase = "idle"
                return
            notify("WhisperKey", "⏹ Распознаю...")
            threading.Thread(target=process_audio, args=(audio_snapshot, current_session_id), daemon=True).start()
        processing = True
        threading.Thread(target=delayed_stop, daemon=True).start()

def create_desktop_launcher():
    try:
        desktop = os.path.expanduser("~/Desktop")
        current_dir = os.path.dirname(os.path.abspath(__file__))
        launcher_name = "WhisperKey.bat"
        target_path = os.path.join(desktop, launcher_name)
        source_path = os.path.join(current_dir, "run_whisperkey.bat")
        if not os.path.exists(target_path) and os.path.exists(source_path):
            import shutil
            shutil.copy2(source_path, target_path)
    except: pass

def main():
    global model
    if not acquire_single_instance_lock():
        print("[FATAL] WhisperKey уже запущен.")
        return
    print("\n" + "="*60)
    print(" 🎙️  WhisperKey v23.5 | Windows Edition")
    print(" Created by Егор Нищук (Telegram: @Seikatsuma)")
    print("="*60)
    create_desktop_launcher()
    print(" Статус: Готов к работе.")
    print("—"*60 + "\n")
    try:
        p = psutil.Process(os.getpid())
        p.nice(psutil.HIGH_PRIORITY_CLASS)
    except: pass
    try:
        print("Загрузка локальной модели...")
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8", cpu_threads=2)
        print("Система готова. Зажми ПРАВЫЙ ALT для записи.")
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()
    except Exception as e:
        print(f"[FATAL] {e}")

if __name__ == "__main__":
    main()
