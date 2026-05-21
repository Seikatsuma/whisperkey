# WhisperKey 🎙️

Глобальный голосовой ввод для macOS, Windows и Linux. Зажми **Right Option**, скажи фразу и отпусти — текст вставится сам.

## Особенности
- **100% Offline**: Работает без интернета.
- **Turbo Speed**: Оптимизировано для слабых процессоров (Intel i5 и выше).
- **International**: Идеально понимает русский и английский одновременно.
- **Direct Insert**: Вставляет текст напрямую, не портит ваш буфер обмена.

---

## Установка (macOS)

1. **Установите зависимости**:
   ```bash
   brew install python@3.11 portaudio
   ```
2. **Клонируйте и установите пакеты**:
   ```bash
   git clone https://github.com/ekaterinabobrovnikova/whisperkey.git
   cd whisperkey
   pip install -r requirements.txt
   ```
3. **Выдайте права**:
   Системные настройки → Защита и безопасность → Конфиденциальность:
   - **Микрофон**: Добавить Terminal
   - **Универсальный доступ**: Добавить Terminal

4. **Запуск**:
   ```bash
   python3 whisperkey.py
   ```

---

## Установка (Windows)

1. Установите [Python 3.11+](https://www.python.org/).
2. Скачайте [FFmpeg](https://ffmpeg.org/download.html) и добавьте его в PATH.
3. Установите зависимости:
   ```cmd
   pip install -r requirements.txt
   ```
4. Запустите:
   ```cmd
   python whisperkey.py
   ```

---

## Установка (Linux)

1. Установите системные зависимости:
   ```bash
   sudo apt install python3-pip portaudio19-dev ffmpeg
   ```
2. Установите пакеты:
   ```bash
   pip install -r requirements.txt
   ```
3. Запустите:
   ```bash
   python3 whisperkey.py
   ```

---

## Управление
- **Удерживать Right Option**: Запись.
- **Отпустить**: Транскрибация и вставка.
- **Ctrl+C**: Выход.
