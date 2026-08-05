# 🎙️ WhisperKey v24 — дословная диктовка

**WhisperKey** — голосовой ввод текста прямо в активное окно. Зажал правый Option,
сказал, отпустил — текст вставился там, где стоит курсор.

Главное свойство: **вставляется ровно то, что сказано.** Речь не переформулируется,
не «улучшается» и не сокращается. Из оформления — только то, что даёт само
распознавание: заглавные и знаки препинания.

---

## 🚀 Как устроено
- **Один запрос в облако** — Groq `whisper-large-v3`. Запись не режется на куски:
  замер показал, что нарезка ухудшает результат (79.8% против 72.3% полноты).
- **Ничего не теряется молча** — если часть записи не распозналась, в тексте
  остаётся метка `[не распознано MM:SS]`, а не гладкая дыра.
- **Предзапись 500 мс** — первое слово не срезается, пока поднимается микрофон.
- **Оффлайн-запас** — при недоступности облака работает локальная модель,
  и об этом приходит уведомление.
- **Cross-Platform:** macOS (`whisperkey.py`). Windows-версия
  (`whisperkey_win.py`) пока содержит старую логику.

Почему нет нейросетевого «корректора»: он стоял на модели, выведенной Groq
из обслуживания, и год молча возвращал сырой текст — именно этот сырой текст
и был тем качеством, которое всех устраивало. Рабочая LLM переписывает
4-6% слов, что для дословной диктовки дефект. Подробности — в `agent.md`.

---

## 🍎 macOS — установка

### 1. Python 3.10+
```bash
brew install portaudio   # только Mac
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. API ключ
```bash
cp .env.example .env
# Вставьте GROQ_API_KEY в .env
```

### 3. Права
**Системные настройки → Конфиденциальность → Универсальный доступ** — разрешите Terminal или Cursor.

### 4. Запуск
Дважды кликните `Запустить WhisperKey.command` или:
```bash
python whisperkey.py
```

---

## 🪟 Windows — установка (пошагово)

### Шаг 1. Python
1. Скачайте Python с [python.org](https://www.python.org/downloads/).
2. При установке **обязательно** поставьте галочку **"Add Python to PATH"**.

### Шаг 2. Скачать проект
```cmd
git clone https://github.com/Seikatsuma/whisperkey.git
cd whisperkey
```

Или откройте папку в **Cursor / Claude Code** и попросите: *«Установи WhisperKey для Windows по README»*.

### Шаг 3. Зависимости
```cmd
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Шаг 4. API ключ
```cmd
copy .env.example .env
```
Откройте `.env` блокнотом и вставьте ключ с [Groq Console](https://console.groq.com/keys):
```env
GROQ_API_KEY=gsk_ваш_ключ
```

> **VPN:** В некоторых регионах Groq недоступен без VPN. Без VPN работает **offline-режим** — локальная модель, качество заметно ниже. Программа предупредит уведомлением, когда переключится.

### Шаг 5. Запуск
**Важно:** запускайте из папки проекта!

```cmd
run_whisperkey.bat
```

При первом успешном запуске на Рабочем столе появится ярлык `WhisperKey.bat` — он уже знает путь к проекту.

### Шаг 6. Если не работает
| Проблема | Решение |
|----------|---------|
| `Python not found` | Переустановите Python с галочкой PATH |
| `ModuleNotFoundError` | `venv\Scripts\activate` → `pip install -r requirements.txt` |
| Запись не начинается | Зажимайте **правый Alt** (AltGr). Нет клавиши — напишите автору |
| Текст не вставляется | ПКМ по `run_whisperkey.bat` → **Запуск от имени администратора** |
| Очень медленно | Создайте `.env` с ключом + включите VPN |
| Долго «висит» при старте | Первый offline-запуск качает ~500 МБ модели — подождите |

**Не закрывайте чёрное окно** — пока оно открыто, WhisperKey работает.

---

## ⌨️ Управление (Mac и Windows)

1. **Зажмите ПРАВЫЙ ALT (Option / AltGr)**
2. **Говорите**
3. **Отпустите** — текст вставится там, где стоит курсор

---

## 💎 Инструкция для AI-ассистентов (Cursor / Claude Code)

### macOS
1. `brew install portaudio` → `pip install -r requirements.txt`
2. `cp .env.example .env` → ключ Groq
3. Права **Accessibility** для Terminal/Cursor
4. Запуск: `python whisperkey.py` или `Запустить WhisperKey.command`

### Windows
1. Проверь Python в PATH: `python --version`
2. `python -m venv venv` → `venv\Scripts\activate` → `pip install -r requirements.txt`
3. **`copy .env.example .env`** (не `cp`!)
4. Запуск **только** через `run_whisperkey.bat` из папки проекта
5. Если вставка не работает — предложи **Запуск от имени администратора**
6. Предупреди про VPN для Groq и про правый Alt / AltGr
7. **Architecture Guard:** цель — дословность. Не добавляй LLM-полировку, не режь
   запись на куски, не клади в промпт темы и имена. Причины и замеры — в `agent.md`,
   прочитай его перед любой правкой.

---

*Разработано Егором Нищуком (Telegram: [@Seikatsuma](https://t.me/Seikatsuma))*
