"""Профиль dictation: Deepgram → Groq (со всей обвязкой) → локальная модель, по очереди.

Перенесено из WhisperKey `whisperkey.py` (`_recognize_with_whisper`, `_transcribe_one_chunk`,
`_needs_retry`, и верхнеуровневая логика каскада, которая раньше жила прямо в
`process_audio`). Ступени идут ПО ОЧЕРЕДИ, не параллельно: первая справляется быстрее,
чем вторая успела бы стартовать (замер: 1.6с против 5.6с на пятиминутной записи) —
см. `~/whisperkey/Whisper на MAC/agent.md`.

`fix_known_terms` применяется ВСЕГДА, независимо от того, какая ступень дала текст —
это было явное поведение `process_audio` (строка "Таблица названий работает ВСЕГДА").
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import chunking
from .context import Context
from .deepgram_engine import maybe_probe_deepgram_if_blocked, transcribe_deepgram
from .density_gate import needs_retry
from .groq_engine import maybe_probe_if_blocked, transcribe_groq
from .local_engine import transcribe_local
from .netcheck import internet_down, reset_cache as _reset_net_cache
from .remote_engine import transcribe_remote
from .yandex_engine import transcribe_yandex
from .terms import fix_known_terms
from .transfer import transfer_punctuation
from .terms import transfer_terms
from .types import RecognitionResult
from .vote import has_loop
from .watermarks import strip_asr_artifacts, boilerplate_only

logger = logging.getLogger("speech_engine.dictation")

HALLUCINATION_TRIGGERS = [
    "спикер говорит",
    "смикер говорит",
    "голос за кадром",
]


def _transcribe_one_chunk(chunk: np.ndarray, chunk_idx: int, n_chunks: int, ctx: Context,
                          allow_cloud: bool = True):
    """Распознаёт один кусок. Возвращает (индекс, текст|None, метка_качества, слова)."""
    chunk_text = None
    chunk_words: list = []
    quality = 'lost'
    chunk_dur = len(chunk) / ctx.sample_rate
    use_cloud = allow_cloud and bool(ctx.groq_api_key) and not ctx.cloud_state.is_blocked

    if use_cloud:
        # use_prompt=True — как в оригинале (implicit default в transcribe_cloud_turbo).
        # Для профиля dictation groq_context_prompt="" всегда, так что на практике
        # это тождественно пустому промпту; для профиля calls (groq_context_prompt
        # непуст) это тот самый путь, которым VERBATIM_PROMPT уходит в основной запрос.
        raw_text, chunk_words = transcribe_groq(
            chunk, api_key=ctx.groq_api_key, profile=ctx.profile, sample_rate=ctx.sample_rate,
            state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
            use_prompt=True, return_words=True)
        degenerate = list(ctx.cloud_state.last_degenerate or [])
        if raw_text:
            lower = raw_text.lower()
            if any(t in lower for t in HALLUCINATION_TRIGGERS):
                cleaned, _removed = strip_asr_artifacts(raw_text)
                if cleaned and (len(cleaned.split()) >= 3 or chunk_dur <= 5.0):
                    chunk_text, quality = cleaned, 'cloud'
                else:
                    logger.info("guard: сегмент %d пуст после чистки", chunk_idx + 1)
            else:
                chunk_text, quality = raw_text, 'cloud'

        # ВНИМАНИЕ — сохранено дословно из оригинала (whisperkey.py, `_transcribe_one_chunk`):
        # условие включает "and ctx.profile.groq_context_prompt", а у профиля dictation
        # этот промпт ВСЕГДА пуст ("" ложно) — то есть в dictation эта ветка переспроса
        # НИКОГДА не срабатывает на практике (мёртвый код при пустом промпте). Это
        # поведение оригинала, не баг переноса: перенесено намеренно как есть, чтобы
        # не поменять число запросов к Groq по сравнению с уже измеренным WhisperKey.
        # Для профиля calls groq_context_prompt непуст — там ветка живая.
        if degenerate and ctx.profile.groq_context_prompt and \
                needs_retry(degenerate, chunk_words, ctx.profile.density_gate_min_words_per_sec):
            logger.info("gate: сегмент %d — переспрашиваю без промпта", chunk_idx + 1)
            retry_text, retry_words = transcribe_groq(
                chunk, api_key=ctx.groq_api_key, profile=ctx.profile, sample_rate=ctx.sample_rate,
                state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
                use_prompt=False, return_words=True)
            if retry_text and len(retry_text.split()) > len((chunk_text or '').split()):
                chunk_text, quality, chunk_words = retry_text, 'cloud_retry', retry_words

    if not chunk_text and ctx.local_model is not None:
        logger.info("local: сегмент %d — локальная модель (качество ниже)", chunk_idx + 1)
        try:
            chunk_text = transcribe_local(chunk, ctx.local_model) or None
            quality = 'local' if chunk_text else 'lost'
            chunk_words = []
        except Exception as e:
            logger.warning("local error: сегмент %d: %s", chunk_idx + 1, type(e).__name__)
            chunk_text, quality = None, 'lost'

    return chunk_idx, chunk_text, quality, chunk_words


def _recognize_with_groq_cascade(audio: np.ndarray, dur: float, ctx: Context,
                                 allow_cloud: bool = True):
    """Вторая и третья ступени: Groq, а при его отказе — локальная модель.

    Возвращает (текст, текст_до_переносов, потерянные_куски, качество_кусков).
    """
    p = ctx.profile
    if allow_cloud:
        maybe_probe_if_blocked(api_key=ctx.groq_api_key, state=ctx.cloud_state, session=ctx.session)
    audio_chunks, chunk_offsets = chunking.split_audio(
        audio, dur, ctx.sample_rate, p.chunk_threshold_seconds, p.chunk_size_seconds,
        p.chunk_overlap_seconds)
    n_chunks = len(audio_chunks)

    style_pool = style_future = None
    if (allow_cloud and p.punct_transfer_min_seconds <= dur <= p.punct_transfer_max_seconds
            and ctx.groq_api_key and not ctx.cloud_state.is_blocked and p.style_prompt):
        style_pool = ThreadPoolExecutor(max_workers=1)
        style_future = style_pool.submit(
            transcribe_groq, audio, api_key=ctx.groq_api_key, profile=p, sample_rate=ctx.sample_rate,
            state=ctx.cloud_state, throttle=ctx.throttle, session=ctx.session,
            allow_retry=False, prompt_override=p.style_prompt)

    ordered_parts: list = [None] * n_chunks
    chunk_quality: list[str] = ['lost'] * n_chunks
    chunk_words: list[list] = [[] for _ in range(n_chunks)]

    parallel_ok = (allow_cloud and p.parallel_cloud_chunks and n_chunks > 1
                  and ctx.groq_api_key and not ctx.cloud_state.is_blocked)

    if parallel_ok:
        with ThreadPoolExecutor(max_workers=p.max_cloud_workers) as pool:
            futures = {
                pool.submit(_transcribe_one_chunk, chunk, idx, n_chunks, ctx, allow_cloud): idx
                for idx, chunk in enumerate(audio_chunks)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    idx, chunk_text, quality, words = fut.result()
                except Exception as e:
                    logger.warning("сегмент %d упал: %s", idx + 1, type(e).__name__)
                    chunk_text, quality, words = None, 'lost', []
                ordered_parts[idx] = chunk_text
                chunk_quality[idx] = quality
                chunk_words[idx] = words
    else:
        for idx, chunk in enumerate(audio_chunks):
            try:
                _, chunk_text, quality, words = _transcribe_one_chunk(
                    chunk, idx, n_chunks, ctx, allow_cloud)
            except Exception as e:
                logger.warning("сегмент %d упал: %s", idx + 1, type(e).__name__)
                chunk_text, quality, words = None, 'lost', []
            ordered_parts[idx] = chunk_text
            chunk_quality[idx] = quality
            chunk_words[idx] = words

    text, lost_marks = chunking.join_chunks(ordered_parts, chunk_quality, chunk_offsets, chunk_words)

    if not text:
        if style_pool is not None:
            style_pool.shutdown(wait=False)
        return "", "", lost_marks, chunk_quality

    assembled = text

    if style_future is not None:
        try:
            styled = style_future.result(timeout=8.0)
            if styled:
                text = transfer_punctuation(text, styled)
                text, _n_terms = transfer_terms(text, styled)
                # transfer_endings отключён 28.10.26: подставлял окончания
                # донорского прохода вместо услышанных. На калибровке 08.08.26
                # давал +9 исправлений команд против -1 поломки, но Егор
                # выбрал дословность без подмены словоформ («как услышал,
                # так и пиши, пусть криво») — приоритет сильнее замера.
        except Exception as e:
            logger.info("punct: второй проход не дошёл (%s) — текст без добавленных знаков",
                       type(e).__name__)
        finally:
            style_pool.shutdown(wait=False)

    return text, assembled, lost_marks, chunk_quality


# --- Слияние двух моделей (bimodel merge, паттерн deepgram-devs/bimodel-
# transcription): Deepgram иногда проглатывает слово целиком («деплой» →
# пробел, жалоба 28.10.26). Второй проход Groq на том же звуке идёт
# ПАРАЛЛЕЛЬНО первому — слияние по меткам слов: пересечение по времени →
# слово основной модели, покрытие только у Groq → его слово. Никакой
# геометрии пауз: решают две разметки, а не энергия звука — пауза-раздумье
# даёт пустоту у обеих и ничего не вставляет.
_MERGE_OVERLAP_S = 0.15   # окна, ближе которого слова — «одно и то же место»

# Приказ Егора 28.10.26: вставлять ВСЁ, что вторая модель услышала в пробеле —
# без порога уверенности и без фильтра «шумовых» слов («пофиг что музыка что-то
# вставит, мне вообще все равно — главное, чтобы все, что он слышал, добавлял»).
# Реальный кейс: nova-3 написала 9 слов за 12с, вторая модель — 13 правильных,
# а фильтры отсекли всё. Оставлен только дедуп соседей (1.5с): схлоп повтора подряд отменён —
# законные «да да», «ну ну» важнее редкого артефакта whisper.


def _norm_word(w: str) -> str:
    return w.strip().strip('.,!?;:—-…()[]«»"\'').lower()


def _merge_word_streams(dg_words: list, g_words: list,
                        out_inserts: list | None = None) -> list | None:
    """Возвращает объединённый список слов по времени или None (сливать нечего).
    out_inserts, если передан, получает вставленные из второй модели слова —
    для самообучения словаря (см. _learn_from_merge)."""
    if not dg_words:
        return None
    norm = []
    for w in g_words or []:
        word = (w.get("word") or "").strip()
        if not word:
            continue
        norm.append({"start": w.get("start") or 0.0, "end": w.get("end") or 0.0,
                     "word": word})
    if not norm:
        return None

    def overlaps(a, b):
        return a["start"] < b["end"] + _MERGE_OVERLAP_S and                a["end"] > b["start"] - _MERGE_OVERLAP_S

    inserts = []
    for gw in norm:
        if any(overlaps(gw, dw) for dw in dg_words):
            continue
        inserts.append(gw)
    if not inserts:
        return None

    # дедупликация соседей: «деплой» рядом с уже стоящим «деплой» — не дублим
    kept = [(w["start"], w["word"]) for w in dg_words]
    n_ins = 0
    for gw in inserts:
        if any(_norm_word(gw["word"]) == _norm_word(k[1])
               for k in kept if abs(k[0] - gw["start"]) < 1.5):
            continue
        kept.append((gw["start"], gw["word"]))
        n_ins += 1
        if out_inserts is not None:
            out_inserts.append(gw["word"])
    if not n_ins:
        return None
    kept.sort(key=lambda t: t[0])
    return [w for _, w in kept]


# Живое самообучение словаря (28.10.26): слово, которое nova-3 потеряла, а
# вторая модель услышала и слияние вставило, — точный сигнал «термин вне
# словаря». Пишем в data/merge_learned.json — ОТДЕЛЬНО от learned_keyterms.json:
# тот синкается с моста целиком и выученное локально затирался бы.
# deepgram_engine читает оба файла (кэш по mtime — без перезапуска).
# Латиница — с первого раза (в русской речи англ. слово = термин почти всегда),
# кириллица — со второго (страховка от разового шума whisper). Жалоба Егора:
# «playbook ушёл в пробел, второй раз то же самое» — теперь второй раз слово
# уже в подсказках.
_LEARN_PATH = Path(__file__).resolve().parent.parent / "data" / "merge_learned.json"
_LEARN_LOCK = threading.Lock()
_LEARN_LATIN = re.compile(r"[A-Za-z]")


def _learn_from_merge(inserted: list) -> None:
    words = [_norm_word(w) for w in inserted]
    words = [w for w in words
             if len(w) >= 3 and w.isalpha()]
    if not words:
        return
    with _LEARN_LOCK:
        try:
            data = json.loads(_LEARN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"terms": [], "counts": {}}
        terms = data.setdefault("terms", [])
        known = {_norm_word(t) for t in terms}
        counts = data.setdefault("counts", {})
        changed = False
        for w in words:
            if w in known:
                continue
            counts[w] = counts.get(w, 0) + 1
            need = 1 if _LEARN_LATIN.search(w) else 2
            if counts[w] >= need:
                terms.append(w)
                known.add(w)
            changed = True
        if changed:
            data["updated"] = time.strftime("%Y-%m-%d %H:%M")
            tmp = _LEARN_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(_LEARN_PATH)


def recognize(audio: np.ndarray | None, dur: float | None, ctx: Context, *,
              deepgram_upload: tuple[bytes, str] | None = None,
              audio_future=None) -> RecognitionResult:
    """Полный каскад dictation: Deepgram → Groq → мост/Yandex → локальная модель.

    Мост (`http_bridge`) передаёт исходный файл: `deepgram_upload=(байты, mime)` уходит
    в Deepgram как есть, а декодирование ffmpeg идёт параллельно в `audio_future`
    (Future → (audio, dur)) и ждётся только после ответа Deepgram. Deepgram не
    принял файл (не сеть и не ключ) — повтор обычным WAV из декодированного звука.

    fix_known_terms применяется всегда, вне зависимости от того, какая ступень
    дала текст (это поведение перенесено из process_audio дословно).

    `ctx.engine_mode` меняет стратегию (см. ASR_ENGINE в .env):
      "auto"   — порядок как описан выше (по умолчанию);
      "remote" — мост пробуется ПЕРВЫМ: режим для сети, где Deepgram и Groq
                 заведомо недоступны (нет VPN), чтобы не жечь их таймауты зря;
      "local"  — только локальная модель, сеть не трогается вовсе.
    Причина каждого переключения ступени складывается в meta["fallbacks"] —
    вызывающий код печатает её строкой `[engine fallback] ...`, чтобы переход
    на запасной путь не оставался невидимым (жалоба 19.10.26: «без VPN не
    работает» при молча уходящем в local каскаде).
    """
    p = ctx.profile
    mode = (ctx.engine_mode or "auto").strip().lower()
    fallbacks: list[str] = []

    def _resolve_audio() -> None:
        nonlocal audio, dur, audio_future
        if audio_future is not None:
            audio, dur = audio_future.result()
            audio_future = None

    if mode != "auto" or not (p.deepgram_enabled and ctx.deepgram_api_key):
        _resolve_audio()
        deepgram_upload = None

    # Сетевой блок первой ступени лечится пробой (VPN мог включиться в любой
    # момент); блоки ключа (401/402/403) проба не трогает — см. функцию.
    maybe_probe_deepgram_if_blocked(state=ctx.deepgram_state, session=ctx.session)

    full_raw_text = None
    engine = ""
    assembled_text = ""
    lost_marks: list = []
    chunk_quality: list[str] = []
    degenerate: list = []
    remote_tried = False
    remote_attempts = 0
    deepgram_reasked = False
    offline = False

    def _check_offline() -> bool:
        # Вызывается только ПОСЛЕ отказа ступени: на счастливом пути проба
        # не тратит ни миллисекунды. Сети нет → облачные ступени и подъём
        # туннеля пропускаются, запись сразу идёт в локальную модель
        # (живой журнал 22.10.26: 21.6с лишних попыток до local).
        nonlocal offline
        if not offline and internet_down():
            offline = True
            fallbacks.append("сеть: интернета нет — сразу локальная модель")
            logger.warning("сеть: интернета нет — облачные ступени пропускаю, сразу local")
        return offline

    def _remote_available() -> bool:
        # Мост считается живой ступенью, когда есть адрес ИЛИ клиент дал
        # reviver (сторож туннеля на Mac): тогда remote-ступень может поднять
        # туннель сама, даже если адрес снят после последней упавшей пробы.
        return bool(ctx.remote_asr_url) or getattr(ctx, "remote_reviver", None) is not None

    def _try_remote() -> None:
        nonlocal full_raw_text, engine, assembled_text, lost_marks, chunk_quality, degenerate
        nonlocal remote_attempts
        remote_attempts += 1
        remote_tried_inner = transcribe_remote(audio, dur, ctx)
        if remote_tried_inner:
            full_raw_text = assembled_text = remote_tried_inner
            engine, lost_marks, chunk_quality, degenerate = "remote", [], ['remote'], []
        else:
            fallbacks.append(f"remote: {ctx.remote_last_error or 'нет ответа'}")

    if mode == "local":
        # Явный офлайн-режим: ни одного облачного вызова.
        engine, chunk_quality = "local", ["local"]
        if ctx.local_model is not None:
            try:
                full_raw_text = transcribe_local(audio, ctx.local_model) or None
            except Exception as e:
                logger.warning("local: %s", type(e).__name__)
        assembled_text = full_raw_text or ""
    else:
        # Режим "remote": мост первым — для сети без VPN, где обе облачные
        # ступени заведомо мертвы и каждая попытка стоит таймаут.
        if mode == "remote" and _remote_available():
            remote_tried = True
            _try_remote()
            if not full_raw_text:
                _check_offline()

        if not full_raw_text and not offline and p.deepgram_enabled and ctx.deepgram_api_key:
            merge_pool = merge_future = None
            if ctx.groq_api_key and not ctx.cloud_state.is_blocked:
                # Второй проход для слияния — параллельно Deepgram, в его тени.
                # При отказе Deepgram каскад Groq пойдёт как обычно (вызов здесь
                # только на слияние, результат второго прохода тогда не ждём).
                merge_pool = ThreadPoolExecutor(max_workers=3)
                def _second_pass():
                    _resolve_audio()
                    if audio is None:
                        return None, []
                    # Окно 30с, шаг 28с: нахлёст 2с, чтобы слово на стыке
                    # целиком попало в следующий кусок (вопрос Егора 28.10.26);
                    # при сборке слова куска берём только в его неперекрытой
                    # зоне — дублей на шве нет.
                    WIN = int(ctx.sample_rate * 30)
                    STEP = int(ctx.sample_rate * 28)
                    if len(audio) <= WIN:
                        return transcribe_groq(
                            audio, api_key=ctx.groq_api_key, profile=p,
                            sample_rate=ctx.sample_rate, state=ctx.cloud_state,
                            throttle=ctx.throttle, session=ctx.session,
                            allow_retry=False, use_prompt=False, return_words=True)
                    parts = [(i0 / ctx.sample_rate, audio[i0:i0 + WIN])
                             for i0 in range(0, len(audio), STEP)]
                    def _one(off_sec, part):
                        t, ws = transcribe_groq(
                            part, api_key=ctx.groq_api_key, profile=p,
                            sample_rate=ctx.sample_rate, state=ctx.cloud_state,
                            throttle=ctx.throttle, session=ctx.session,
                            allow_retry=False, use_prompt=False, return_words=True)
                        for w in ws or []:
                            w["start"] = (w.get("start") or 0.0) + off_sec
                            w["end"] = (w.get("end") or 0.0) + off_sec
                        return t, ws or []
                    futs = [merge_pool.submit(_one, off, part)
                            for off, part in parts]
                    texts, all_w = [], []
                    for i, f in enumerate(futs):
                        try:
                            t, ws = f.result(timeout=8)
                        except Exception:
                            continue
                        if t:
                            texts.append(t)
                        for w in ws:
                            if i == len(futs) - 1 or w["start"] < i * 28.0 + 28.0:
                                all_w.append(w)
                    all_w.sort(key=lambda w: w["start"])
                    return " ".join(texts) or None, all_w
                merge_future = merge_pool.submit(_second_pass)
            deepgram_upload_attempted = time.time() >= ctx.deepgram_state.blocked_until
            full_raw_text = transcribe_deepgram(
                audio, api_key=ctx.deepgram_api_key, profile=p, sample_rate=ctx.sample_rate,
                state=ctx.deepgram_state, session=ctx.session, upload=deepgram_upload)
            deepgram_upload_attempted = deepgram_upload_attempted and time.time() >= ctx.deepgram_state.blocked_until
            if not full_raw_text:
                # Звук нужен только неуспешному пути: повтор WAV и запасные
                # ступени. На счастливом пути декод не ждём — ответ уходит по
                # времени Deepgram (замер 22.10.26: 3-19с лишних на webm).
                _resolve_audio()
            if deepgram_upload is not None and deepgram_upload_attempted and not full_raw_text \
                    and time.time() >= ctx.deepgram_state.blocked_until:
                # Контейнер не принят/пусто — тот же звук прежним путём (WAV + темп),
                # чтобы новый путь не мог дать хуже старого.
                logger.info("deepgram: исходный файл не дал текста (%s) — повтор WAV",
                            ctx.deepgram_state.last_reason or "?")
                deepgram_upload = None
                full_raw_text = transcribe_deepgram(
                    audio, api_key=ctx.deepgram_api_key, profile=p, sample_rate=ctx.sample_rate,
                    state=ctx.deepgram_state, session=ctx.session)
            if full_raw_text:
                engine = "deepgram"
                assembled_text = full_raw_text
                chunk_quality = ['deepgram']
                # Заглушка («Продолжение следует…» и т.п.) — не ответ, а галлюцинация
                # на не-речевом звуке. Считаем отказом ступени: дальше Groq, у которого
                # своя проверка заглушек по кускам. Реальная фраза «продолжение следует»
                # посреди текста сюда не попадает — проверка только целиком.
                if boilerplate_only(full_raw_text):
                    logger.info("deepgram: только заглушка ASR — ухожу на следующую ступень")
                    fallbacks.append("deepgram: заглушка ASR")
                    full_raw_text = assembled_text = ""
                    # Текст обнулился ПОСЛЕ точки декода выше — без звука
                    # remote/yandex молча пропустятся (audio=None).
                    _resolve_audio()
                elif dur is None and deepgram_upload is not None:
                    # ffprobe не дал длительности — проверку «редкого ответа»
                    # делать нечем; отмечаем, иначе деградация невидима.
                    fallbacks.append("deepgram: dur неизвестна — проверка редкого ответа пропущена")
                elif dur is not None and dur > 8.0:
                    # Deepgram изредка отдаёт «редкий» ответ: целые куски речи
                    # выпадают, остаток сглажен (живой случай 20.10.26: 18 слов
                    # на 22 с). Повтор ТОГО ЖЕ звука вернул все 42 слова дословно —
                    # сбой разовый, не в звуке. Два триггера повтора:
                    #   а) < 1 слово/с при темпе Егора ~1.5-2;
                    #   б) после последнего распознанного слова остаётся >3с записи
                    #      с громкостью речи (энергия считается по самому звуку,
                    #      тишина в хвосте не срабатывает — проверено на корпусе:
                    #      хвосты с паузой тихие, хвосты с потерей громкие).
                    # Повтор берём, только если слов больше и нет петли (has_loop).
                    n_words = len(full_raw_text.split())
                    wps = n_words / dur
                    tail_gap = dur - ctx.deepgram_state.last_word_end
                    tail_speech = False
                    # Без меток слов (last_word_end=0) хвост не проверяем —
                    # иначе весь файл считался бы непокрытым.
                    if ctx.deepgram_state.last_word_end and tail_gap > 3.0:
                        # Звук для энергии хвоста достаём только когда триггер
                        # сработал: на пути deepgram_upload декод ещё не ждали.
                        _resolve_audio()
                        tail = audio[int(ctx.sample_rate * ctx.deepgram_state.last_word_end):] if audio is not None else []
                        if len(tail):
                            tail_speech = float(
                                np.sqrt((tail.astype(np.float64) ** 2).mean())) > 0.01
                    if wps < 1.0 or tail_speech:
                        reason = (f"мало текста ({wps:.2f} слов/с, {n_words} слов)"
                                  if wps < 1.0 else
                                  f"после последнего слова ещё {tail_gap:.1f}с речи")
                        logger.warning("deepgram: %s за %.1fс — переспрашиваю", reason, dur)
                        deepgram_reasked = True
                        # Повтор идёт по декодированному звуку: на пути upload
                        # его ещё могли не ждать (сработал только wps-триггер).
                        _resolve_audio()
                        retry = transcribe_deepgram(
                            audio, api_key=ctx.deepgram_api_key, profile=p,
                            sample_rate=ctx.sample_rate, state=ctx.deepgram_state,
                            session=ctx.session)
                        n_retry = len(retry.split()) if retry else 0
                        if retry and n_retry > n_words and not has_loop(retry):
                            full_raw_text = assembled_text = retry
                            logger.warning("deepgram: повтор дал %d слов вместо %d — беру повтор",
                                           n_retry, n_words)
                        else:
                            logger.warning("deepgram: повтор не лучше (%d слов) — оставляю первый",
                                           n_retry)
                if full_raw_text and merge_future is not None:
                    # Второй проход уже почти наверняка готов — шёл параллельно.
                    try:
                        # Потолок ожидания: вторая модель идёт в тени первой и
                        # обычно готова к ответу; медленная — пропускаем слияние
                        # этой диктовки, чем растягиваем её на секунды (замер
                        # 28.10.26: выбросы до 4.6с на медленном Groq).
                        _gtext, g_words = merge_future.result(timeout=4) or (None, [])
                    except Exception as e:
                        logger.warning("bimodel: второй проход не дождался (%s)",
                                       type(e).__name__)
                        g_words = []
                    ins: list = []
                    dg_words = ctx.deepgram_state.last_words
                    if g_words and dur and dur > 5.0 and dg_words and                             len(dg_words) / dur < 0.5 and                             len(g_words) > len(dg_words):
                        merged = [w["word"] for w in g_words]
                        ins = []
                        logger.warning("bimodel: dg скудный (%d слов за %.1fс) — "
                                       "беру groq целиком (%d слов)",
                                       len(dg_words), dur, len(g_words))
                    else:
                        merged = _merge_word_streams(dg_words, g_words,
                                                     out_inserts=ins)
                    if merged:
                        full_raw_text = assembled_text = " ".join(merged)
                        logger.warning("bimodel: +%d слов от второй модели",
                                       len(merged) - len(dg_words))
                        try:
                            _learn_from_merge(ins)
                        except Exception:
                            pass
            else:
                fallbacks.append(f"deepgram: {ctx.deepgram_state.last_reason or 'нет ответа'}")
                _check_offline()

        if not full_raw_text:
            # Ранний вызов моста: Groq заведомо недоступен (ключа нет или свежий
            # блок — на Mac'е у Егора гео-блок 403 навсегда) → не жечь минуты на
            # локальной модели, спросить каскад на сервере.
            if not offline and _remote_available() and not remote_tried and \
                    (not ctx.groq_api_key or ctx.cloud_state.is_blocked):
                remote_tried = True
                _try_remote()

        if not full_raw_text:
            # Groq мёртв, а мост не ответил — Yandex дешевле и быстрее, чем
            # прогон локальной модели по кускам (облако, доступное без VPN).
            if not offline and ctx.yandex_api_key and ctx.cloud_state.is_blocked:
                ytext = transcribe_yandex(audio, dur, ctx)
                if ytext:
                    full_raw_text = assembled_text = ytext
                    engine, chunk_quality = "yandex", ['yandex']
                else:
                    fallbacks.append(f"yandex: {ctx.yandex_last_error or 'нет ответа'}")

        if not full_raw_text:
            engine = "groq" if ctx.groq_api_key else "local"
            _resolve_audio()
            full_raw_text, assembled_text, lost_marks, chunk_quality = _recognize_with_groq_cascade(
                audio, dur, ctx, allow_cloud=not offline)
            degenerate = list(ctx.cloud_state.last_degenerate or [])
            if 'local' in chunk_quality and 'cloud' not in chunk_quality and 'cloud_retry' not in chunk_quality:
                engine = "local"
                if ctx.groq_api_key and not offline:
                    fallbacks.append(f"groq: {ctx.cloud_state.last_reason or 'заблокирован'} → local")
            if not full_raw_text:
                fallbacks.append("groq/local: пустой результат")

            # Спасательные ступени: каскад кончился пустотой или целиком
            # локальной моделью → мост (там Deepgram и Groq живы), затем Yandex.
            # После local (десятки секунд) сеть могла вернуться — проба свежая.
            if offline and (not full_raw_text or engine == "local"):
                _reset_net_cache()
                offline = internet_down()
            # Здесь remote_tried НЕ проверяем: первая попытка могла не дойти до
            # моста вовсе (флап сети, отказ reviver), а спустя десятки секунд
            # local туннель уже жив — иначе запись остаётся в локальной модели
            # при живом мосте (живой журнал 22.10.26: 22.5с диктовка ушла в
            # local на 34с, туннель поднялся на ~20-й секунде декода, а
            # спасательный блок молчал именно из-за remote_tried). Потолок 2
            # попыток, чтобы не молотить действительно мёртвый мост.
            if not offline and _remote_available() and remote_attempts < 2 and (not full_raw_text or engine == "local"):
                remote_tried = True
                _try_remote()
            if not offline and ctx.yandex_api_key and (not full_raw_text or engine == "local"):
                ytext = transcribe_yandex(audio, dur, ctx)
                if ytext:
                    full_raw_text = assembled_text = ytext
                    engine, lost_marks, chunk_quality, degenerate = "yandex", [], ['yandex'], []
                else:
                    fallbacks.append(f"yandex: {ctx.yandex_last_error or 'нет ответа'}")

    if not full_raw_text:
        return RecognitionResult(text="", engine=engine, assembled_text="", lost_marks=lost_marks,
                                 chunk_quality=chunk_quality, degenerate=degenerate,
                                 meta={"fallbacks": fallbacks})

    full_raw_text, n_fix = fix_known_terms(full_raw_text)

    return RecognitionResult(
        text=full_raw_text, engine=engine, assembled_text=assembled_text,
        lost_marks=lost_marks, chunk_quality=chunk_quality, degenerate=degenerate,
        terms_fixed=n_fix,
        meta={
            "deepgram_seconds": ctx.deepgram_state.last_seconds,
            "api_text": ctx.cloud_state.last_api_text,
            "fallbacks": fallbacks,
            "deepgram_reasked": deepgram_reasked,
        },
    )
