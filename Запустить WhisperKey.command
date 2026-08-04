#!/bin/bash
# Запуск WhisperKey. Файл работает и из папки проекта, и с рабочего стола.

cd "$(dirname "$0")" || exit 1

CONFIG="$HOME/.whisperkey_path"

# Ярлык лежит отдельно от проекта — берём путь, записанный при прошлом запуске.
# Раньше здесь был зашит абсолютный путь чужого пользователя, и у всех остальных
# ярлык на рабочем столе просто не работал.
if [ ! -f "whisperkey.py" ] && [ -f "$CONFIG" ]; then
  SAVED="$(cat "$CONFIG")"
  if [ -f "$SAVED/whisperkey.py" ]; then
    cd "$SAVED" || exit 1
  fi
fi

# Не нашли — пробуем типовые места.
if [ ! -f "whisperkey.py" ]; then
  for guess in \
    "$HOME/Desktop/Whisper на MAC" \
    "$HOME/Desktop/whisperkey" \
    "$HOME/whisperkey/Whisper на MAC" \
    "$HOME/Documents/whisperkey"; do
    if [ -f "$guess/whisperkey.py" ]; then
      cd "$guess" || exit 1
      break
    fi
  done
fi

# Всё ещё нет — спрашиваем у человека вместо молчаливого выхода.
if [ ! -f "whisperkey.py" ]; then
  echo "Не нашёл папку с whisperkey.py."
  read -r -p "Перетащи сюда папку проекта и нажми Enter: " MANUAL
  MANUAL="${MANUAL%\"}"; MANUAL="${MANUAL#\"}"   # снять кавычки от перетаскивания
  MANUAL="${MANUAL%/}"
  if [ -f "$MANUAL/whisperkey.py" ]; then
    cd "$MANUAL" || exit 1
  else
    echo "Там нет whisperkey.py. Проверь папку и запусти снова."
    read -r -p "Enter для выхода..."
    exit 1
  fi
fi

# Запомнили — в следующий раз найдётся сразу.
pwd > "$CONFIG"

export KMP_DUPLICATE_LIB_OK=TRUE
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

echo "WhisperKey — запуск из: $(pwd)"

# venv проекта, если он есть: без него не найдутся установленные зависимости.
if [ -f "venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
  echo "Окружение: venv проекта"
fi

PY=""
for candidate in python3 /usr/local/bin/python3.10 /opt/homebrew/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Ошибка: python3 не найден. Установи Python 3.10 или новее."
  read -r -p "Enter для выхода..."
  exit 1
fi

echo "Python: $($PY --version)"
exec "$PY" whisperkey.py
