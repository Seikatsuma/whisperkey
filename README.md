# 🎙️ WhisperKey v23.5 "Flawless"

**WhisperKey** — мгновенная голосовая транскрибация текста прямо в активное окно. Облако (Groq Whisper Large v3 + Llama 3.1 70B) + оффлайн fallback.

---

## 🚀 Основные возможности
- **Dual-Stage Pipeline:** Whisper Large v3 → Llama 3.1 70B (грамматика и пунктуация).
- **Zero Hallucinations:** Strict Prompt — запрет на выдумку имён и контекста.
- **Smart Chunking:** Дробление записей 30с+ по паузам.
- **Cross-Platform:** macOS (`whisperkey.py`) и Windows (`whisperkey_win.py`).

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

> **VPN:** В некоторых регионах Groq недоступен без VPN. Без VPN работает **offline-режим** (медленнее, без Llama-корректора).

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
7. **Architecture Guard:** Large-v3 + Llama 3.1, Skip=0, Strict Prompt — не упрощать

---

*Разработано Егором Нищуком (Telegram: [@Seikatsuma](https://t.me/Seikatsuma))*
