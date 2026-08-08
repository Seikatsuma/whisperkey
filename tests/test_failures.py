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
    """В WAV попадает ровно исходный звук, изменённый только по темпу.

    Раньше здесь резались паузы и дописывалась тишина — и то и другое
    сдвигало границы 30-секундных окон модели и стоило слов. Единственная
    оставшаяся обработка — ASR_TEMPO, и она обязана быть ровно такой,
    какой заявлена: никакого паддинга сверх пересчёта сетки.
    """
    import io, wave
    audio = np.full(int(M["SAMPLE_RATE"] * 3), 0.5, dtype=np.float32)
    data = M["create_audio_wav"](audio)
    with wave.open(io.BytesIO(data), "rb") as wf:
        frames = wf.getnframes()
    expected = int(len(audio) / M["ASR_TEMPO"]) if M["ASR_TEMPO"] != 1.0 else len(audio)
    check("Только пересчёт по темпу, без паддинга", abs(frames - expected) <= 1,
          f"на входе {len(audio)}, темп {M['ASR_TEMPO']}, ожидалось {expected}, в WAV {frames}")

    # Частота дискретизации в заголовке НЕ меняется: именно поэтому речь для
    # модели звучит быстрее. Подмена частоты вернула бы исходное звучание
    # и обнулила бы весь эффект.
    with wave.open(io.BytesIO(data), "rb") as wf:
        check("Частота дискретизации не тронута", wf.getframerate() == M["SAMPLE_RATE"],
              f"{wf.getframerate()} вместо {M['SAMPLE_RATE']}")


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


def run_short_scenario(chunk_behaviour, styled, seconds=30.0):
    """Прогон записи, на которой работает ВТОРОЙ проход за знаками препинания.

    30 секунд: выше порога PUNCT_TRANSFER_MIN_SECONDS и ниже порога нарезки,
    то есть путь ровно тот, каким идёт обычная длинная диктовка.
    """
    INSERTED.clear()
    NOTIFIED.clear()
    M["_transcribe_one_chunk"] = chunk_behaviour
    M["transcribe_cloud_turbo"] = styled
    M["CLOUD_ENABLED"] = True
    M["cloud_status"]["is_blocked"] = False
    M["process_audio"](make_audio(seconds), session_id=1)
    return "".join(INSERTED), list(NOTIFIED)


def _base_chunk(chunk, idx, n):
    return idx, "первое слово второе слово третье слово", "cloud", []


def test_style_pass_crash_does_not_lose_text():
    """Падение второго прохода не должно стоить пользователю текста."""
    def boom(*a, **k):
        raise RuntimeError("Groq недоступен")
    text, notes = run_short_scenario(_base_chunk, boom)
    check("Знаки: падение второго прохода — текст на месте",
          "первое слово второе слово третье слово" in text.lower(), f"вставлено: {text!r}")
    check("Знаки: падение второго прохода — уведомление есть",
          any("готов" in m.lower() for _, m in notes), f"уведомления: {notes}")


def test_style_pass_empty_does_not_lose_text():
    """Пустой ответ второго прохода — текст остаётся без добавленных знаков."""
    text, _ = run_short_scenario(_base_chunk, lambda *a, **k: None)
    check("Знаки: пустой второй проход — текст на месте",
          "первое слово второе слово третье слово" in text.lower(), f"вставлено: {text!r}")


def test_style_pass_cannot_inject_its_words():
    """Второй проход идёт с промптом. Его слова не имеют права попасть в текст."""
    def styled(*a, **k):
        return "Да, конечно. Хорошо. А что дальше?"
    text, _ = run_short_scenario(_base_chunk, styled)
    low = text.lower()
    check("Знаки: слова второго прохода отброшены",
          "конечно" not in low and "дальше" not in low, f"вставлено: {text!r}")
    check("Знаки: свои слова сохранены",
          "первое слово второе слово третье слово" in low, f"вставлено: {text!r}")


def test_style_pass_marks_are_applied():
    """Когда слова совпали — знаки со второго прохода переносятся."""
    def styled(*a, **k):
        return "Первое слово, второе слово. Третье слово."
    text, _ = run_short_scenario(_base_chunk, styled)
    check("Знаки: точка и запятая перенесены",
          "," in text and "." in text, f"вставлено: {text!r}")
    check("Знаки: слова не изменились",
          [w.strip(".,!?").lower() for w in text.split()][:6]
          == ["первое", "слово", "второе", "слово", "третье", "слово"],
          f"вставлено: {text!r}")


def test_microphone_dropouts_are_visible():
    """Переполнение входного буфера обязано доходить до пользователя.

    Это единственный класс потерь, который распознавание не лечит: сэмплы
    выброшены железом, звука уже нет. Если промолчать, пользователь спишет
    пропавшие слова на модель и будет крутить не ту ручку.
    """
    def ok(chunk, idx, n):
        return idx, "первое слово второе слово", "cloud", []

    M["audio_dropouts"].clear()
    M["audio_dropouts"].append("input overflow")
    text, notes = run_short_scenario(ok, lambda *a, **k: None)
    said = " ".join(m for _, m in notes).lower()
    check("Пропуск микрофона виден в уведомлении", "микрофон" in said,
          f"уведомления: {notes}")
    check("Пропуск микрофона не съел текст",
          "первое слово" in text.lower(), f"вставлено: {text!r}")

    M["audio_dropouts"].clear()
    text, notes = run_short_scenario(ok, lambda *a, **k: None)
    said = " ".join(m for _, m in notes).lower()
    check("Без пропусков — обычное уведомление", "микрофон" not in said,
          f"уведомления: {notes}")


def test_paste_never_fires_on_unconfirmed_clipboard():
    """Главное правило вставки: не подтвердился буфер — не жмём Cmd+V.

    Именно эта гонка приводила к тому, что в документ приезжал СТАРЫЙ буфер
    вместо продиктованного текста: программа клала текст, спала 0.1 с наугад
    и жала вставку, не проверив, что система успела.
    """
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
        # Буфер всегда отдаёт чужое — подтверждения не будет никогда.
        M["_clipboard_now"] = lambda: "ЧУЖОЙ СТАРЫЙ ТЕКСТ".encode("utf-8")
        M["kb"].type = lambda t: typed.append(t)
        M["CLIPBOARD_WAIT_SEC"] = 0.05          # чтобы тест не ждал полторы секунды
        NOTIFIED.clear()
        REAL_INSERT("мой продиктованный текст")
    finally:
        M["subprocess"].run, M["_clipboard_now"], M["kb"].type = orig_run, orig_now, orig_type

    check("Буфер не подтверждён — Cmd+V не отправлен",
          "/usr/bin/osascript" not in calls, f"вызовы: {calls}")
    check("Текст не потерян — набран напрямую",
          typed and "мой продиктованный текст" in typed[0], f"набрано: {typed}")


def test_clipboard_restore_skipped_if_user_copied_own():
    """Прежний буфер не возвращается, если человек успел скопировать своё.

    Возврат через 0.5 с обгонял медленные приложения, и они забирали уже
    восстановленный старый текст. Теперь пауза дольше, а перед возвратом
    проверяется, что в буфере всё ещё наш текст.
    """
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


# ─── Каскад движков ───────────────────────────────────────────────────────────
# Deepgram — первая ступень, Groq — вторая, локальная модель — третья.
# Проверяется главное свойство каскада: отказ ЛЮБОЙ ступени не должен стоить
# человеку диктовки. Все переходы обязаны быть молчаливыми для результата
# и видимыми в терминале.

class DGResp:
    """Ответ Deepgram, каким его видит transcribe_deepgram."""
    def __init__(self, code, transcript=None, broken=False):
        self.status_code = code
        self.headers = {}
        self.text = "тело ответа"
        self._transcript = transcript
        self._broken = broken

    def json(self):
        if self._broken:
            return {"неожиданная": "форма"}
        return {"results": {"channels": [{"alternatives": [
            {"transcript": self._transcript}]}]}}


def run_cascade(dg_behaviour, whisper_text="запасной путь сработал", seconds=45.0):
    """Прогон process_audio с подменённым ответом Deepgram.

    Возвращает (вставленный текст, уведомления, сколько раз звали whisper).
    """
    INSERTED.clear()
    NOTIFIED.clear()
    whisper_calls = []

    def fake_chunk(chunk, idx, n):
        whisper_calls.append(idx)
        return idx, whisper_text, "cloud", []

    M["_transcribe_one_chunk"] = fake_chunk
    orig_post = M["http_session"].post
    M["http_session"].post = dg_behaviour
    M["DEEPGRAM_ENABLED"] = True
    M["deepgram_status"]["blocked_until"] = 0.0
    try:
        M["process_audio"](make_audio(seconds), session_id=1)
    finally:
        M["http_session"].post = orig_post
        M["DEEPGRAM_ENABLED"] = False
        M["deepgram_status"]["blocked_until"] = 0.0
    return "".join(INSERTED), list(NOTIFIED), whisper_calls


def test_deepgram_goes_first_and_whisper_stays_idle():
    """Когда первая ступень отвечает, вторая не должна тратить ни запроса.

    Это не только деньги: на пятиминутной записи Groq отвечает 5.6 с против
    1.6 с у Deepgram, и лишний вызов был бы прямой задержкой вставки.
    """
    text, notes, whisper = run_cascade(
        lambda *a, **k: DGResp(200, "текст от первой ступени"))
    check("Каскад: текст от Deepgram вставлен", "текст от первой ступени" in text.lower(),
          f"вставлено: {text!r}")
    check("Каскад: whisper не вызывался", not whisper, f"вызовов whisper: {whisper}")


def test_deepgram_empty_falls_back_to_whisper():
    """Пустой ответ — не повод объявлять диктовку нераспознанной."""
    text, notes, whisper = run_cascade(lambda *a, **k: DGResp(200, ""))
    check("Пустой Deepgram: ушли на whisper", bool(whisper), "whisper не вызвался")
    check("Пустой Deepgram: текст всё равно есть", "запасной путь сработал" in text.lower(),
          f"вставлено: {text!r}")


def test_deepgram_broken_json_falls_back():
    """Неожиданная форма ответа не должна ронять диктовку."""
    text, notes, whisper = run_cascade(lambda *a, **k: DGResp(200, None, broken=True))
    check("Битый ответ: ушли на whisper", bool(whisper), "whisper не вызвался")
    check("Битый ответ: текст доставлен", "запасной путь сработал" in text.lower(),
          f"вставлено: {text!r}")


def test_deepgram_out_of_money_blocks_and_warns():
    """Кончились деньги — работаем дальше, но человек об этом узнаёт.

    402 повтором не лечится, поэтому движок отключается на четверть часа:
    иначе каждая следующая диктовка платила бы ожиданием за мёртвый запрос.
    """
    text, notes, whisper = run_cascade(lambda *a, **k: DGResp(402))
    said = " ".join(f"{n} {m}" for n, m in notes).lower()
    check("Нет денег: текст доставлен", "запасной путь сработал" in text.lower(),
          f"вставлено: {text!r}")
    check("Нет денег: whisper подхватил", bool(whisper), "whisper не вызвался")
    check("Нет денег: человек предупреждён", "deepgram" in said or "движок" in said,
          f"уведомления: {notes}")


def test_deepgram_blocked_makes_no_request():
    """Пока действует блокировка, к Deepgram не должно уходить ни одного запроса."""
    import time as _time
    calls = []

    def counting_post(url=None, *a, **k):
        # Считаем ТОЛЬКО запросы к Deepgram: по этому же http_session идёт
        # стилевой проход whisper, и без разделения тест ловил бы его.
        if url and M["DEEPGRAM_URL"] in str(url):
            calls.append(1)
        return DGResp(200, "не должно быть вызвано")

    INSERTED.clear()
    NOTIFIED.clear()
    M["_transcribe_one_chunk"] = lambda chunk, idx, n: (idx, "через whisper", "cloud", [])
    orig_post = M["http_session"].post
    M["http_session"].post = counting_post
    M["DEEPGRAM_ENABLED"] = True
    M["deepgram_status"]["blocked_until"] = _time.time() + 600
    try:
        M["process_audio"](make_audio(45.0), session_id=1)
    finally:
        M["http_session"].post = orig_post
        M["DEEPGRAM_ENABLED"] = False
        M["deepgram_status"]["blocked_until"] = 0.0
    check("Блокировка: ни одного запроса", not calls, f"запросов: {len(calls)}")
    check("Блокировка: текст всё равно доставлен", "через whisper" in "".join(INSERTED).lower(),
          f"вставлено: {INSERTED}")


def test_deepgram_timeout_falls_back():
    """Молчание движка не должно превращаться в молчание программы."""
    class FakeTimeout(Exception):
        pass
    orig_timeout = M["requests"].Timeout
    M["requests"].Timeout = FakeTimeout

    def timing_out(*a, **k):
        raise FakeTimeout("не дождались")

    try:
        text, notes, whisper = run_cascade(timing_out)
    finally:
        M["requests"].Timeout = orig_timeout
    check("Таймаут: ушли на whisper", bool(whisper), "whisper не вызвался")
    check("Таймаут: текст доставлен", "запасной путь сработал" in text.lower(),
          f"вставлено: {text!r}")


def test_deepgram_params_stay_verbatim():
    """Настройки движка — часть требования дословности, а не вкусовщина.

    smart_format переписывает «двадцать пять» в «25» и стоил 2.8 пункта
    на замере; filler_words выкидывает «ну» и «вот», которые Егор произносит.
    Обе настройки менялись замером, поэтому закреплены проверкой.
    """
    p = M["DEEPGRAM_PARAMS"]
    check("Дословность: smart_format выключен", p.get("smart_format") == "false",
          f"smart_format={p.get('smart_format')}")
    check("Дословность: filler_words включён", p.get("filler_words") == "true",
          f"filler_words={p.get('filler_words')}")
    check("Дословность: числа не переписываются", p.get("numerals") == "false",
          f"numerals={p.get('numerals')}")
    check("Модель nova-2 (nova-3 на русском слабее)", p.get("model") == "nova-2",
          f"model={p.get('model')}")
    check("Язык задан явно", p.get("language") == "ru", f"language={p.get('language')}")


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
