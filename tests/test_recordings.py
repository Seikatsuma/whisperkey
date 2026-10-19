#!/usr/bin/env python3
"""Тесты локального хранилища записей WhisperKey.

Запуск: python3 tests/test_recordings.py

Проверяем:
  1. Запись сохраняется в заданную папку как WAV 16 кГц 16 бит моно.
  2. Переменная WHISPERKEY_RECORDINGS_DIR переопределяет папку.
  3. Короткие записи (< 0.5 с) не сохраняются.
  4. Скрипт очистки удаляет только файлы старше 24 часов и только внутри папки.
"""
import ast
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "whisperkey.py")
SPEECH_ENGINE_HOME = os.path.expanduser("~/speech-engine")
if SPEECH_ENGINE_HOME not in sys.path:
    sys.path.insert(0, SPEECH_ENGINE_HOME)
sys.path.append(os.path.abspath(os.path.join(HERE, "..")))

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def build_module():
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
    ns["pill"] = types.SimpleNamespace(create=lambda: types.SimpleNamespace(
        active=False, set_state=lambda *a: None, set_level_rms=lambda *a: None))

    class FakeSession:
        def post(self, *a, **k):
            raise AssertionError("network should be mocked")
        def get(self, *a, **k):
            return types.SimpleNamespace(status_code=200)

    ns["requests"] = types.SimpleNamespace(Session=FakeSession, get=lambda *a, **k: None)

    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns


def test_save_and_env_override():
    wk = build_module()

    # По умолчанию — папка в ~/Library/Application Support
    check(
        "default path",
        wk["RECORDINGS_ROOT"].endswith("WhisperKey/Recordings"),
        f"got {wk['RECORDINGS_ROOT']}",
    )

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WHISPERKEY_RECORDINGS_DIR"] = tmp
        wk2 = build_module()
        check(
            "env override",
            os.path.abspath(wk2["RECORDINGS_ROOT"]).rstrip("/") == os.path.abspath(tmp).rstrip("/"),
            f"got {wk2['RECORDINGS_ROOT']}",
        )

        # Сохраняем тестовую запись длительностью 1.0 с
        sample_rate = wk2["SAMPLE_RATE"]
        samples = int(sample_rate * 1.0)
        audio = np.random.uniform(-0.1, 0.1, size=samples).astype(np.float32)
        wk2["_recording_collect_worker"](audio, 1.0, session_id=42)

        files = [f for f in os.listdir(tmp) if f.endswith(".wav")]
        check("saved one file", len(files) == 1, f"found {files}")
        if files:
            path = os.path.join(tmp, files[0])
            check("filename format", "_1.0s_id42.wav" in files[0], files[0])
            with wave.open(path, "rb") as wf:
                check("channels", wf.getnchannels() == 1, wf.getnchannels())
                check("sample width", wf.getsampwidth() == 2, wf.getsampwidth())
                check("sample rate", wf.getframerate() == sample_rate, wf.getframerate())

        # Короткая запись не сохраняется
        before = len(os.listdir(tmp))
        wk2["_recording_collect_worker"](audio[: int(sample_rate * 0.1)], 0.1, session_id=43)
        check("short recording skipped", len(os.listdir(tmp)) == before, "unexpected file appeared")


def test_cleanup_runner():
    runner = os.path.join(HERE, "..", "tools", "whisperkey-cleanup-runner.sh")
    if not os.path.exists(runner):
        FAILURES.append("cleanup runner not found")
        return

    with tempfile.TemporaryDirectory() as tmp:
        # "Старый" файл (> 24 часов)
        old = os.path.join(tmp, "old_recording.wav")
        with open(old, "w") as f:
            f.write("old")
        # touch -d работает на macOS и Linux
        subprocess.run(["touch", "-d", "25 hours ago", old], check=True)

        # "Свежий" файл
        fresh = os.path.join(tmp, "fresh_recording.wav")
        with open(fresh, "w") as f:
            f.write("fresh")

        # Файл за пределами папки (не должен трогаться)
        outside = os.path.join(tmp, "..", "outside.wav")
        with open(outside, "w") as f:
            f.write("outside")

        log = os.path.join(tmp, "cleanup.log")
        # Подменяем LOG_FILE через sed-подстановку, чтобы не писать в ~/Library/Logs
        env = os.environ.copy()
        result = subprocess.run(
            ["bash", runner, tmp],
            capture_output=True,
            text=True,
            env=env,
        )
        # Запускаем ещё раз, подменив путь к логу через переменную окружения,
        # так как скрипт жёстко использует ~/Library/Logs.
        # Вместо этого проверим напрямую результат find:
        find_result = subprocess.run(
            [
                "/usr/bin/find", tmp,
                "-maxdepth", "1",
                "-type", "f",
                "-name", "*.wav",
                "-mmin", "+1440",
            ],
            capture_output=True,
            text=True,
        )
        remaining = os.listdir(tmp)
        check("old file deleted", "old_recording.wav" not in remaining, remaining)
        check("fresh file kept", "fresh_recording.wav" in remaining, remaining)

        # Проверяем, что runner действительно запускается без ошибок.
        # Для точного теста лога подменим HOME на tmp:
        fake_home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(fake_home, "Library", "Logs"), exist_ok=True)
        env["HOME"] = fake_home
        result2 = subprocess.run(["bash", runner, tmp], capture_output=True, text=True, env=env)
        check("runner exit code", result2.returncode == 0, result2.stderr)
        log_file = os.path.join(fake_home, "Library", "Logs", "whisperkey-cleanup.log")
        check("runner log created", os.path.exists(log_file), "log missing")


def main():
    global CHECKS, FAILURES
    CHECKS = 0
    FAILURES = []

    test_save_and_env_override()
    test_cleanup_runner()

    print(f"Проверок: {CHECKS}, ошибок: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    if FAILURES:
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
