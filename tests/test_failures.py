#!/usr/bin/env python3
"""Стенд отказов: что видит пользователь, когда что-то ломается.

Запуск:  python3 tests/test_failures.py

Гоняет НАСТОЯЩИЙ process_audio из whisperkey.py. Заглушены только внешние края:
сеть, микрофон, локальная модель, вставка текста и уведомления. Проверяется
единственное — доходит ли до пользователя текст и узнаёт ли он о деградации.
Прежняя версия на первом же сценарии отдавала пустоту и ноль уведомлений.
"""
import ast
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def build_module():
    """Собирает модуль из исходника, вырезая всё, что требует железа и сети."""
    source = open(SRC, encoding="utf-8").read()
    tree = ast.parse(source)

    SKIP_IMPORTS = {"sounddevice", "faster_whisper", "pynput", "psutil", "requests"}
    body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or ""
            names = [a.name for a in node.names]
            if mod.split(".")[0] in SKIP_IMPORTS or any(n.split(".")[0] in SKIP_IMPORTS for n in names):
                continue
        if isinstance(node, ast.Try):
            # try: import sounddevice ... except OSError: sys.exit(1)
            src_seg = ast.get_source_segment(source, node) or ""
            if "sounddevice" in src_seg:
                continue
        if isinstance(node, ast.If):
            src_seg = ast.get_source_segment(source, node) or ""
            if "__main__" in src_seg:
                continue
        body.append(node)

    ns = {"__name__": "whisperkey_under_test", "__file__": SRC}
    # Заглушки внешнего мира
    kb = types.SimpleNamespace(press=lambda *a: None, release=lambda *a: None, type=lambda *a: None)
    ns["sd"] = types.SimpleNamespace(InputStream=lambda **kw: types.SimpleNamespace(
        start=lambda: None, stop=lambda: None, close=lambda: None))
    ns["WhisperModel"] = lambda *a, **k: None
    ns["keyboard"] = types.SimpleNamespace(
        Key=types.SimpleNamespace(alt_r="alt_r"), Listener=object)
    ns["KeyboardController"] = lambda: kb
    ns["KeyboardKey"] = types.SimpleNamespace(cmd="cmd")
    ns["psutil"] = types.SimpleNamespace(Process=lambda pid: types.SimpleNamespace(
        nice=lambda v: None, cpu_affinity=lambda v: None))

    class FakeSession:
        def post(self, *a, **k):
            raise AssertionError("сеть должна быть замокана в каждом сценарии")
        def head(self, *a, **k):
            return None
        def options(self, *a, **k):
            return None

    ns["requests"] = types.SimpleNamespace(Session=FakeSession, get=lambda *a, **k: None)

    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns


M = build_module()

# Перехватываем всё, что уходит наружу
INSERTED = []
NOTIFIED = []
M["direct_insert"] = lambda text: INSERTED.append(text)
M["notify"] = lambda title, message: NOTIFIED.append((title, message))
M["schedule_eval_sample_collect"] = lambda *a, **k: None
M["finalize_eval_sample_meta"] = lambda *a, **k: None
M["print"] = lambda *a, **k: None
M["CLOUD_ENABLED"] = True
M["cloud_status"]["is_blocked"] = False


def make_audio(seconds):
    """Список блоков по 512 сэмплов, как их отдаёт микрофон."""
    n_blocks = int(seconds * M["SAMPLE_RATE"] / 512)
    return [np.full((512, 1), 0.1, dtype=np.float32) for _ in range(n_blocks)]


def run_scenario(chunk_behaviour, seconds=1100.0):
    """Прогоняет process_audio, подменив распознавание одного куска."""
    INSERTED.clear()
    NOTIFIED.clear()
    M["_transcribe_one_chunk"] = chunk_behaviour
    M["process_audio"](make_audio(seconds), session_id=1)
    return "".join(INSERTED), list(NOTIFIED)


# ─── Сценарии ─────────────────────────────────────────────────────────────────

def test_all_ok():
    def ok(chunk, idx, n):
        return idx, f"текст_куска_{idx}", "cloud", []
    text, notes = run_scenario(ok)
    check("Контроль: текст вставлен", "текст_куска_0" in text.lower(), f"вставлено: {text!r}")
    check("Контроль: уведомление есть", any("готов" in m.lower() for _, m in notes),
          f"уведомления: {notes}")


def test_exception_in_one_chunk():
    """Раньше: вставлено пусто, уведомлений ноль. Три живых куска выброшены."""
    def boom(chunk, idx, n):
        if idx == 1:
            raise RuntimeError("сеть отвалилась")
        return idx, f"текст_куска_{idx}", "cloud", []
    text, notes = run_scenario(boom)
    check("Отказ 1 куска: остальные дошли", "текст_куска_0" in text.lower() and "текст_куска_2" in text.lower(),
          f"вставлено: {text!r}")
    check("Отказ 1 куска: есть метка потери", "[не распознано" in text,
          f"вставлено: {text!r}")
    joined = " ".join(f"{n} {m}" for n, m in notes).lower()
    check("Отказ 1 куска: пользователь уведомлён",
          notes and ("частично" in joined or "потеряно" in joined),
          f"уведомления: {notes}")


def test_chunk_returns_none():
    """Раньше: дыра до 23 секунд склеивалась встык и рапортовалась «Текст готов»."""
    def none_chunk(chunk, idx, n):
        if idx == 1:
            return idx, None, "lost", []
        return idx, f"текст_куска_{idx}", "cloud", []
    text, notes = run_scenario(none_chunk)
    check("Пустой кусок: метка в тексте", "[не распознано" in text, f"вставлено: {text!r}")
    check("Пустой кусок: не рапортует «готов»",
          not any(m == "Текст готов" for _, m in notes), f"уведомления: {notes}")


def test_local_fallback_is_visible():
    """Уход на слабую локальную модель должен быть виден."""
    def local(chunk, idx, n):
        return idx, f"текст_куска_{idx}", "local" if idx == 0 else "cloud", []
    text, notes = run_scenario(local)
    check("Локальная модель: текст есть", "текст_куска_0" in text.lower(), f"вставлено: {text!r}")
    check("Локальная модель: пользователь предупреждён",
          any("локальная" in m.lower() or "частично" in str(n).lower() for n, m in notes),
          f"уведомления: {notes}")


def test_all_chunks_lost():
    def all_lost(chunk, idx, n):
        return idx, None, "lost", []
    text, notes = run_scenario(all_lost)
    check("Всё потеряно: уведомление есть", len(notes) > 0, f"уведомления: {notes}")


def test_short_recording_not_chunked():
    """Короткая диктовка обрабатывается одним куском — без швов и меток."""
    calls = []
    def single(chunk, idx, n):
        calls.append(n)
        return idx, "короткая фраза целиком", "cloud", []
    text, notes = run_scenario(single, seconds=45.0)
    check("Короткая: один кусок", calls and calls[0] == 1, f"кусков: {calls}")
    check("Короткая: без меток", "[не распознано" not in text, f"вставлено: {text!r}")


def test_http_429_retries():
    """429 должен приводить к повтору, а не к молчаливой дыре."""
    attempts = {"n": 0}

    class Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload or {}
        def json(self):
            return self._payload

    class Session:
        def post(self, *a, **k):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return Resp(429)
            return Resp(200, {"text": "получилось со второго раза",
                              "segments": [{"start": 0.0, "end": 5.0,
                                            "text": "получилось со второго раза",
                                            "no_speech_prob": 0.0}],
                              "words": []})
        def head(self, *a, **k): return None
        def options(self, *a, **k): return None

    M["http_session"] = Session()
    M["GROQ_API_KEY"] = "test-key"
    M["time"].sleep = lambda s: None   # не ждём в тесте
    audio = np.full(int(M["SAMPLE_RATE"] * 5), 0.1, dtype=np.float32)
    result = M["transcribe_cloud_turbo"](audio)
    check("429: было 3 попытки", attempts["n"] == 3, f"попыток {attempts['n']}")
    check("429: текст получен", result == "получилось со второго раза", f"получено {result!r}")


def test_http_500_retries():
    attempts = {"n": 0}

    class Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload or {}
        def json(self):
            return self._payload

    class Session:
        def post(self, *a, **k):
            attempts["n"] += 1
            return Resp(500)
        def head(self, *a, **k): return None
        def options(self, *a, **k): return None

    M["http_session"] = Session()
    M["GROQ_API_KEY"] = "test-key"
    M["time"].sleep = lambda s: None
    audio = np.full(int(M["SAMPLE_RATE"] * 5), 0.1, dtype=np.float32)
    result = M["transcribe_cloud_turbo"](audio)
    check("500: исчерпаны попытки", attempts["n"] == 4, f"попыток {attempts['n']}")
    check("500: вернул None", result is None, f"получено {result!r}")


def test_no_preprocessing_in_wav():
    """WAV должен содержать ровно столько сэмплов, сколько было на входе."""
    import io, wave
    audio = np.full(int(M["SAMPLE_RATE"] * 3), 0.5, dtype=np.float32)
    data = M["create_audio_wav"](audio)
    with wave.open(io.BytesIO(data), "rb") as wf:
        frames = wf.getnframes()
    check("Без паддинга", frames == len(audio),
          f"на входе {len(audio)} сэмплов, в WAV {frames} (разница {frames - len(audio)})")


def test_idle_release_and_recovery():
    """Микрофон отпускается по простою и переоткрывается, если поток умер.

    Вечно открытый поток вреден не нагрузкой, а тем, что молча умирает при смене
    аудиоустройства: объект остаётся, сэмплы не идут, запись уходит в никуда.
    """
    state = {"active": True, "opened": 0, "stopped": 0}

    class FakeStream:
        def __init__(self):
            state["opened"] += 1
        @property
        def active(self):
            return state["active"]
        def start(self): pass
        def stop(self): state["stopped"] += 1
        def close(self): pass

    M["sd"] = types.SimpleNamespace(InputStream=lambda **kw: FakeStream())
    M["audio_stream"] = None
    M["is_recording"] = False
    M["session_phase"] = "idle"

    # Живой поток переоткрывать не надо
    check("Простой: поток открылся", M["ensure_audio_stream"](), "не открылся")
    opened_after_first = state["opened"]
    M["ensure_audio_stream"]()
    check("Живой поток не переоткрывается", state["opened"] == opened_after_first,
          f"открытий {state['opened']}, было {opened_after_first}")

    # Поток умер (сменилось устройство) — обязан переоткрыться
    state["active"] = False
    M["ensure_audio_stream"]()
    check("Мёртвый поток переоткрыт", state["opened"] == opened_after_first + 1,
          f"открытий {state['opened']}")

    # Освобождение по простою
    state["active"] = True
    M["_close_idle_stream"]()
    check("Микрофон отпущен по простою", M["audio_stream"] is None,
          f"поток остался: {M['audio_stream']}")

    # Во время записи микрофон не отпускается
    M["ensure_audio_stream"]()
    M["is_recording"] = True
    M["_close_idle_stream"]()
    check("Во время записи не отпускается", M["audio_stream"] is not None,
          "поток закрыли посреди записи")
    M["is_recording"] = False


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            FAILURES.append(f"{t.__name__}: упал с {type(e).__name__}: {e}\n"
                            + traceback.format_exc(limit=3))

    print()
    print("=" * 70)
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} из {CHECKS} проверок")
        print("=" * 70)
        for f in FAILURES:
            print(f"  ✗ {f}")
        print()
        return 1
    print(f"СТЕНД ОТКАЗОВ ПРОЙДЕН: {CHECKS} проверок, {len(tests)} сценариев")
    print("=" * 70)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
