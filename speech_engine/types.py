"""Общие типы данных пакета.

Каскад распознавания (deepgram_engine/groq_engine/dictation) работает с "сырыми"
словарями — той же формой, в которой Deepgram/Groq отдают JSON (`{"word":.., "start":..,
"end":..}`). Так исторически устроен код, перенесённый из WhisperKey, и трогать форму
здесь рискованно: гейт плотности, склейка кусков и перенос знаков в оригинале завязаны
именно на dict, а не на объект.

`Word` — датакласс нужен только на границе `align`/`vote` (профиль calls): там сравниваются
две независимые гипотезы транскрибации, и статическая форма снижает риск опечатки в ключе
словаря. Конвертация dict -> Word делается один раз на входе в align/vote, дальше каскад
как работал с dict, так и работает.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Word:
    """Одно распознанное слово с таймкодом. Единица сравнения в align()/vote_merge()."""
    text: str
    start: float
    end: float


@dataclass
class RecognitionResult:
    """Итог одного вызова speech_engine.recognize().

    text            — финальный текст (после fix_known_terms; для dictation — ещё и
                       после transfer_punctuation/transfer_terms/transfer_endings).
    engine          — какая ступень дала результат: "deepgram" | "groq" | "groq_vote" | "local".
    assembled_text  — текст ДО переноса знаков/названий/форм (для профиля dictation;
                       для остальных путей совпадает с text). Нужен для диагностики —
                       по нему видно, что именно добавил второй проход.
    lost_marks      — список меток [не распознано MM:SS] по потерянным кускам.
    chunk_quality   — по одной метке на кусок: 'cloud'|'cloud_retry'|'local'|'lost'|'vote'.
    degenerate      — окна, где сорвался гейт плотности (для диагностики/аудита).
    terms_fixed     — сколько замен сделал fix_known_terms.
    meta            — всё остальное, что может пригодиться вызывающему (сырые тексты
                       голосующих проходов, статистика склейки и т.п.) — не часть контракта,
                       не проверяется тестами, только для логов/отладки.
    """
    text: str
    engine: str
    assembled_text: str = ""
    lost_marks: list[str] = field(default_factory=list)
    chunk_quality: list[str] = field(default_factory=list)
    degenerate: list[dict] = field(default_factory=list)
    terms_fixed: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text)
