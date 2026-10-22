"""Контекст выполнения: ключи, http-сессия, состояние ступеней. Отдельно от `Engine`
(engine.py), чтобы `dictation.py`/`calls.py` могли на него ссылаться без цикличного
импорта (`Engine` использует и `dictation`, и `calls`).

Всё изменяемое состояние (`CloudState`, `DeepgramState`, `Throttle`) держится ЗДЕСЬ,
по одному набору на `Context` — то есть на процесс/бота, который его создал. Раньше
(в WhisperKey) это были модульные глобальные переменные; при нескольких ботах на одном
сервере, использующих общий пакет, модульные глобальные были бы гонкой между процессами
разных ботов, если бы пакет когда-нибудь стал общим *сервисом*, а не библиотекой,
импортируемой в каждый процесс отдельно. Поскольку сейчас это именно библиотека
(каждый бот — свой процесс), гонки нет и без Context, но явный контекст всё равно
лучше: тесты могут создать свежий Context на каждый сценарий, не деля состояние
между прогонами (раньше именно это делало тесты WhisperKey чувствительными к порядку).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .groq_engine import CloudState, Throttle
from .deepgram_engine import DeepgramState
from .profiles import Profile


@dataclass
class Context:
    profile: Profile
    sample_rate: int = 16000
    groq_api_key: str = ""
    deepgram_api_key: str = ""
    remote_asr_url: str = ""     # мост на серверный каскад (http_bridge), пусто = ступени нет
    remote_asr_token: str = ""   # X-Bridge-Token, если мост выставлен наружу
    remote_last_error: str = ""  # причина последнего отказа моста — для [engine fallback]
    remote_reviver: object = None  # callable()->bool: переподнять мост СРАЗУ после обрыва
                                   # (ставит клиент, напр. сторож туннеля WhisperKey на Mac).
                                   # На сервере мост локальный — reviver не нужен.
    yandex_api_key: str = ""     # резервная облачная ступень без VPN (Yandex SpeechKit)
    yandex_folder_id: str = ""   # folderId — нужен только для IAM-аккаунтов, не для Api-Key
    yandex_last_error: str = ""
    local_model: object = None   # faster_whisper.WhisperModel-подобный объект или None
    engine_mode: str = "auto"    # auto | remote | local — выбор стратегии каскада (ASR_ENGINE)
    session: requests.Session = field(default_factory=requests.Session)
    cloud_state: CloudState = field(default_factory=CloudState)
    deepgram_state: DeepgramState = field(default_factory=DeepgramState)
    throttle: Throttle = None

    def __post_init__(self):
        if self.throttle is None:
            self.throttle = Throttle(self.profile.min_request_interval)
