import ast
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import speech_engine
from speech_engine import dictation


def build_engine(mac, mode):
    tree = ast.parse((ROOT / 'whisperkey.py').read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == '_engine' for t in node.targets))
    namespace = dict(speech_engine=speech_engine, DICTATION=speech_engine.DICTATION,
                     GROQ_API_KEY='test', DEEPGRAM_API_KEY='test', SAMPLE_RATE=16000,
                     http_session=None, os=os,
                     sys=SimpleNamespace(platform='darwin' if mac else 'linux'))
    with patch.dict(os.environ, {'ASR_ENGINE': mode, 'REMOTE_ASR_URL': ''}):
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<engine>', 'exec'), namespace)
    return namespace['_engine']


class LocalWarmupTests(unittest.TestCase):
    def test_warmup_consumes_lazy_segments(self):
        tree = ast.parse((ROOT / 'whisperkey.py').read_text())
        warmup = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == '_warm_local_model')
        namespace = {'np': np, 'SAMPLE_RATE': 16000}
        exec(compile(ast.Module(body=[warmup], type_ignores=[]), '<warmup>', 'exec'), namespace)
        consumed = []

        class Model:
            def transcribe(self, audio, **kwargs):
                self.audio = audio
                self.kwargs = kwargs

                def segments():
                    consumed.append(True)
                    yield SimpleNamespace(text='')
                    consumed.append(True)

                return segments(), None

        model = Model()
        namespace['_warm_local_model'](model)
        self.assertEqual(consumed, [True, True])
        self.assertEqual(model.audio.shape, (8000,))
        self.assertEqual(model.kwargs, {'language': 'ru', 'beam_size': 1})


class ServerFirstTests(unittest.TestCase):
    def test_mac_auto_uses_server_before_cloud(self):
        engine = build_engine(True, 'auto')
        engine.ctx.remote_asr_url = 'http://127.0.0.1:8092/audio/transcriptions'
        with patch.object(dictation, 'transcribe_remote', return_value='Проверка записи.') as remote, \
             patch.object(dictation, 'transcribe_deepgram', side_effect=AssertionError('Прямой запрос до сервера')), \
             patch.object(dictation, '_recognize_with_groq_cascade', side_effect=AssertionError('Локальный каскад до сервера')):
            result = engine.recognize(np.zeros(16000, dtype=np.float32), 1.0)
        self.assertEqual(result.engine, 'remote')
        remote.assert_called_once()

    def test_explicit_modes_and_other_platforms(self):
        self.assertEqual(build_engine(True, 'local').ctx.engine_mode, 'local')
        self.assertEqual(build_engine(True, 'remote').ctx.engine_mode, 'remote')
        self.assertEqual(build_engine(False, 'auto').ctx.engine_mode, 'auto')

    def test_reconnected_bridge_keeps_priority(self):
        engine = build_engine(True, 'auto')
        with patch.object(dictation, 'transcribe_deepgram', return_value='Резерв.') as direct:
            result = engine.recognize(np.zeros(16000, dtype=np.float32), 1.0)
        self.assertEqual(result.engine, 'deepgram')
        direct.assert_called_once()
        engine.ctx.remote_asr_url = 'http://127.0.0.1:8092/audio/transcriptions'
        with patch.object(dictation, 'transcribe_remote', return_value='Сервер.') as remote, \
             patch.object(dictation, 'transcribe_deepgram', side_effect=AssertionError('Лишнее ожидание')):
            result = engine.recognize(np.zeros(16000, dtype=np.float32), 1.0)
        self.assertEqual(result.engine, 'remote')
        remote.assert_called_once()

    def test_failed_bridge_preserves_fallback(self):
        engine = build_engine(True, 'auto')
        engine.ctx.remote_asr_url = 'http://127.0.0.1:8092/audio/transcriptions'
        with patch.object(dictation, 'transcribe_remote', return_value=''), \
             patch.object(dictation, 'transcribe_deepgram', return_value='Резерв.'):
            result = engine.recognize(np.zeros(16000, dtype=np.float32), 1.0)
        self.assertEqual(result.engine, 'deepgram')
        self.assertTrue(result.meta['fallbacks'])


if __name__ == '__main__':
    unittest.main()
