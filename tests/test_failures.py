#!/usr/bin/env python3
"""Стенд отказов WhisperKey: что видит пользователь, когда что-то ломается.

Запуск:  python3 tests/test_failures.py

09.08.26 — каскад (Deepgram/Groq/локальная модель, гейт плотности, ретраи, 429/5xx,
дробление и склейка) переехал в общий пакет speech_engine и там же обзавёлся СВОИМ
стендом отказов (см. `~/speech-engine/tests/test_dictation_failures.py` и
`test_calls_failures.py`, профили dictation/calls) — прогоняется независимо и
покрывает то, что раньше проверялось здесь через глубокий mock `_transcribe_one_chunk`/
`transcribe_cloud_turbo`/`http_session`. Дублировать эти сценарии здесь незачем:
логика физически в другом файле, глубокий mock внутренностей каскада отсюда бы её
не достал (process_audio зовёт РЕАЛЬНЫЙ `speech_engine.Engine.recognize`, а не
локальные функции).

Что остаётся ЗДЕСЬ — граница whisperkey.py и её собственная логика:
  1. process_audio корректно РЕАГИРУЕТ на результат каскада (RecognitionResult) —
     вставляет текст, формирует уведомление, видит деградацию. Проверяется
     подменой `_engine.recognize` на канонические сценарии результата — тот же
     принцип, что раньше был "подмени `_transcribe_one_chunk`", только на один
     уровень выше, на границе пакета, а не внутри его сейчас уже приватных функций.
  2. Один интеграционный smoke-тест ГОНЯЕТ настоящий `_engine.recognize` через
     фиктивную HTTP-сессию целиком — доказывает, что монтаж (whisperkey.py ->
     speech_engine.Engine -> requests) в принципе работает, а не только что
     process_audio правильно реагирует на заранее слепленный результат.
  3. Всё платформенное, что каскада никогда не касалось: машина простоя микрофона,
     гонки буфера обмена, видимость пропусков микрофона.
"""
import ast
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")
SPEECH_ENGINE_HOME = os.path.expanduser("~/speech-engine")
if SPEECH_ENGINE_HOME not in sys.path:
    sys.path.insert(0, SPEECH_ENGINE_HOME)
import speech_engine
from speech_engine.types import RecognitionResult

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def build_module():
    """Собирает модуль из исходника, вырезая всё, что требует железа и сети.

    speech_engine НЕ в списке пропуска: пакет не трогает ни микрофон, ни модель
    при импорте, и process_audio должен получить настоящий импортированный пакет,
    а не заглушку — иначе тест проверял бы не тот монтаж, что в проде.
    """
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
            src_seg = ast.get_source_segment(source, node) or ""
            if "sounddevice" in src_seg:
                continue
        if isinstance(node, ast.If):
            src_seg = ast.get_source_segment(source, node) or ""
            if "__main__" in src_seg:
                continue
        body.append(node)

    ns = {"__name__": "whisperkey_under_test", "__file__": SRC}
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
        def get(self, *a, **k):
            # background_cloud_probe зовёт session.get() — не должен падать AttributeError
            # в фоновом потоке, если сценарий его случайно разбудит.
            return types.SimpleNamespace(status_code=200)

    class FakeTimeout(Exception):
        pass

    ns["requests"] = types.SimpleNamespace(Session=FakeSession, get=lambda *a, **k: None,
                                           Timeout=FakeTimeout)

    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns


M = build_module()

# Перехватываем всё, что уходит наружу
INSERTED = []
NOTIFIED = []
REAL_INSERT = M["direct_insert"]          # настоящая — для тестов вставки
M["direct_insert"] = lambda text: INSERTED.append(text)
M["notify"] = lambda title, message: NOTIFIED.append((title, message))
M["schedule_eval_sample_collect"] = lambda *a, **k: None
M["finalize_eval_sample_meta"] = lambda *a, **k: None
M["print"] = lambda *a, **k: None


def make_audio(seconds):
    """Список блоков по 512 сэмплов, как их отдаёт микрофон."""
    n_blocks = int(seconds * M["SAMPLE_RATE"] / 512)
    return [np.full((512, 1), 0.1, dtype=np.float32) for _ in range(n_blocks)]


def run_with_result(fake_result, seconds=45.0):
    """Подменяет _engine.recognize на заранее слепленный результат каскада и
    гоняет process_audio целиком — проверяем реакцию, не повторяем логику каскада."""
    INSERTED.clear()
    NOTIFIED.clear()
    real_recognize = M["_engine"].recognize
    if isinstance(fake_result, Exception):
        M["_engine"].recognize = lambda audio, dur: (_ for _ in ()).throw(fake_result)
    else:
        M["_engine"].recognize = lambda audio, dur: fake_result
    try:
        M["process_audio"](make_audio(seconds), session_id=1)
    finally:
        M["_engine"].recognize = real_recognize
    return "".join(INSERTED), list(NOTIFIED)


# ─── 1. process_audio реагирует на RecognitionResult ───────────────────────────

def test_all_ok():
    text, notes = run_with_result(RecognitionResult(
        text="текст куска ноль", engine="groq", chunk_quality=["cloud"]))
    check("Контроль: текст вставлен", "текст куска ноль" in text.lower(), f"вставлено: {text!r}")
    check("Контроль: уведомление о готовности", any("готов" in m.lower() for _, m in notes),
          f"уведомления: {notes}")


def test_deepgram_result_reported():
    text, notes = run_with_result(RecognitionResult(
        text="текст от deepgram", engine="deepgram", chunk_quality=["deepgram"],
        meta={"deepgram_seconds": 1.6}))
    check("Deepgram: текст вставлен", "текст от deepgram" in text.lower(), f"вставлено: {text!r}")
    check("Deepgram: уведомление о готовности", any("готов" in m.lower() for _, m in notes),
          f"уведомления: {notes}")


def test_empty_result_not_inserted():
    """Раньше: пустой результат мог тихо пройти дальше. Теперь — явный отказ."""
    text, notes = run_with_result(RecognitionResult(text="", engine=""))
    check("Пустой результат: ничего не вставлено", text == "", f"вставлено: {text!r}")
    said = " ".join(m for _, m in notes).lower()
    check("Пустой результат: уведомление есть", "не распознан" in said, f"уведомления: {notes}")


def test_lost_chunks_are_visible():
    """Раньше: дыра в записи склеивалась молча, приходило «Текст готов»."""
    text, notes = run_with_result(RecognitionResult(
        text="первый кусок [не распознано 00:57] третий кусок", engine="groq",
        chunk_quality=["cloud", "lost", "cloud"],
        lost_marks=["[не распознано 00:57]"]))
    check("Потерянный кусок: метка дошла до пользователя", "[не распознано" in text,
          f"вставлено: {text!r}")
    said = " ".join(f"{n} {m}" for n, m in notes).lower()
    check("Потерянный кусок: уведомление предупреждает", "потеряно" in said, f"уведомления: {notes}")


def test_local_engine_fallback_is_visible():
    """Уход на слабую локальную модель должен быть виден, не молчать."""
    text, notes = run_with_result(RecognitionResult(
        text="текст с локальной модели", engine="local", chunk_quality=["local"]))
    check("Локальная модель: текст доставлен", "текст с локальной модели" in text.lower(),
          f"вставлено: {text!r}")
    said = " ".join(f"{n} {m}" for n, m in notes).lower()
    check("Локальная модель: пользователь предупреждён", "локальная" in said, f"уведомления: {notes}")


def test_engine_exception_does_not_crash_and_notifies():
    """Раньше: падение каскада ДО notify оставляло пользователя без единого сигнала."""
    text, notes = run_with_result(RuntimeError("Groq и Deepgram оба недоступны"))
    check("Исключение каскада: ничего не вставлено", text == "", f"вставлено: {text!r}")
    said = " ".join(f"{n} {m}" for n, m in notes).lower()
    check("Исключение каскада: пользователь предупреждён", "сбой" in said, f"уведомления: {notes}")


def test_terms_fixed_does_not_break_flow():
    """terms_fixed>0 — просто счётчик для лога, не должен влиять на вставку (кроме
    обычного финального оформления — заглавная первая буква, см. CAPITALIZE_FIRST)."""
    text, notes = run_with_result(RecognitionResult(
        text="докрути WhisperKey", engine="groq", chunk_quality=["cloud"], terms_fixed=1))
    check("terms_fixed: текст вставлен (с обычной капитализацией)",
          "докрути whisperkey" in text.lower(), f"вставлено: {text!r}")


# ─── 2. Интеграционный smoke — весь монтаж целиком, не только реакция ─────────

def test_full_cascade_integration_smoke():
    """Гоняет НАСТОЯЩИЙ _engine.recognize через фиктивную HTTP-сессию: доказывает,
    что whisperkey.py -> speech_engine.Engine -> requests смонтированы верно,
    а не только что process_audio умеет реагировать на готовый результат."""
    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self.headers = {}
            self._payload = payload
        def json(self):
            return self._payload

    def fake_post(url, *a, **k):
        if "deepgram" in url:
            return Resp(200, {"results": {"channels": [{"alternatives": [
                {"transcript": ""}]}]}})   # Deepgram молчит — уходим на Groq
        return Resp(200, {
            "text": "интеграционный текст",
            "segments": [{"start": 0.0, "end": 2.0, "text": "интеграционный текст"}],
            "words": [{"word": "интеграционный", "start": 0.0, "end": 1.0},
                      {"word": "текст", "start": 1.0, "end": 2.0}],
        })

    INSERTED.clear()
    NOTIFIED.clear()
    M["http_session"].post = fake_post
    M["DEEPGRAM_ENABLED"] = True
    M["_engine"].ctx.deepgram_api_key = "test-deepgram-key"
    M["_engine"].ctx.groq_api_key = "test-groq-key"
    try:
        M["process_audio"](make_audio(3.0), session_id=1)
    finally:
        M["DEEPGRAM_ENABLED"] = False
        M["_engine"].ctx.deepgram_api_key = ""
        M["_engine"].ctx.groq_api_key = ""
    text = "".join(INSERTED)
    check("Интеграция: текст дошёл через настоящий Engine", "интеграционный текст" in text.lower(),
          f"вставлено: {text!r}")


# ─── 3. Платформенное — не касается каскада ────────────────────────────────────

def test_idle_release_and_recovery():
    """Микрофон отпускается по простою и переоткрывается, если поток умер."""
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

    check("Простой: поток открылся", M["ensure_audio_stream"](), "не открылся")
    opened_after_first = state["opened"]
    M["ensure_audio_stream"]()
    check("Живой поток не переоткрывается", state["opened"] == opened_after_first,
          f"открытий {state['opened']}, было {opened_after_first}")

    state["active"] = False
    M["ensure_audio_stream"]()
    check("Мёртвый поток переоткрыт", state["opened"] == opened_after_first + 1,
          f"открытий {state['opened']}")

    state["active"] = True
    M["_close_idle_stream"]()
    check("Микрофон отпущен по простою", M["audio_stream"] is None,
          f"поток остался: {M['audio_stream']}")

    M["ensure_audio_stream"]()
    M["is_recording"] = True
    M["_close_idle_stream"]()
    check("Во время записи не отпускается", M["audio_stream"] is not None,
          "поток закрыли посреди записи")
    M["is_recording"] = False


def test_microphone_dropouts_are_visible():
    """Переполнение входного буфера обязано доходить до пользователя."""
    M["audio_dropouts"].clear()
    M["audio_dropouts"].append("input overflow")
    text, notes = run_with_result(RecognitionResult(
        text="первое слово второе слово", engine="groq", chunk_quality=["cloud"]))
    said = " ".join(m for _, m in notes).lower()
    check("Пропуск микрофона виден в уведомлении", "микрофон" in said, f"уведомления: {notes}")
    check("Пропуск микрофона не съел текст", "первое слово" in text.lower(), f"вставлено: {text!r}")

    M["audio_dropouts"].clear()
    text, notes = run_with_result(RecognitionResult(
        text="первое слово второе слово", engine="groq", chunk_quality=["cloud"]))
    said = " ".join(m for _, m in notes).lower()
    check("Без пропусков — обычное уведомление", "микрофон" not in said, f"уведомления: {notes}")


def test_paste_never_fires_on_unconfirmed_clipboard():
    """Главное правило вставки: не подтвердился буфер — не жмём Cmd+V."""
    if "_clipboard_now" not in M or "subprocess" not in M:
        check("Буфер: проверка не применима к этой версии", True, "вставка через Windows API")
        return
    calls = []

    class FakeProc:
        returncode = 0
        stdout = "ЧУЖОЙ СТАРЫЙ ТЕКСТ".encode("utf-8")

    def fake_run(cmd, *a, **k):
        calls.append(cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd))
        return FakeProc()

    orig_run, orig_now, orig_type = M["subprocess"].run, M["_clipboard_now"], M["kb"].type
    typed = []
    try:
        M["subprocess"].run = fake_run
        M["_clipboard_now"] = lambda: "ЧУЖОЙ СТАРЫЙ ТЕКСТ".encode("utf-8")
        M["kb"].type = lambda t: typed.append(t)
        M["CLIPBOARD_WAIT_SEC"] = 0.05
        NOTIFIED.clear()
        REAL_INSERT("мой продиктованный текст")
    finally:
        M["subprocess"].run, M["_clipboard_now"], M["kb"].type = orig_run, orig_now, orig_type

    check("Буфер не подтверждён — Cmd+V не отправлен",
          "/usr/bin/osascript" not in calls, f"вызовы: {calls}")
    check("Текст не потерян — набран напрямую",
          typed and "мой продиктованный текст" in typed[0], f"набрано: {typed}")


def test_clipboard_restore_skipped_if_user_copied_own():
    """Прежний буфер не возвращается, если человек успел скопировать своё."""
    if "_restore_clipboard_async" not in M or "subprocess" not in M:
        check("Возврат буфера: проверка не применима к этой версии", True, "вставка через Windows API")
        return
    import time as _time
    restored = []
    orig_run = M["subprocess"].run
    orig_now = M["_clipboard_now"]
    try:
        M["subprocess"].run = lambda cmd, *a, **k: restored.append(k.get("input"))
        M["_clipboard_now"] = lambda: "ЧЕЛОВЕК СКОПИРОВАЛ СВОЁ".encode("utf-8")
        M["CLIPBOARD_RESTORE_SEC"] = 0.05
        M["_restore_clipboard_async"]("старый буфер".encode("utf-8"), "наш текст".encode("utf-8"))
        _time.sleep(0.3)
    finally:
        M["subprocess"].run, M["_clipboard_now"] = orig_run, orig_now
    check("Чужую копию не затираем", not restored, f"записано: {restored}")


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
