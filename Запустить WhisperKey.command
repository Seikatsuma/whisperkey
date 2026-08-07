#!/bin/bash
# Запуск WhisperKey. Файл работает и из папки проекта, и с рабочего стола.
# Если его запустили без окна Терминала (Automator, Shortcuts, Raycast, launchd,
# горячая клавиша) — сам открывает Терминал и перезапускается уже в нём.

# ─── Перезапуск в Терминале, если запущены без него ──────────────────────────
# [ -t 1 ] — есть ли на выходе терминал. При двойном клике по .command он есть,
# при запуске из Automator и подобных — нет, и тогда программа работала бы
# вслепую: ни логов, ни возможности ответить на вопрос про папку.
# Переменная-флаг защищает от бесконечного перезапуска самого себя.
if [ ! -t 1 ] && [ -z "${WHISPERKEY_IN_TERMINAL:-}" ]; then
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  # do script принимает строку AppleScript: обратные слэши и кавычки надо
  # удвоить, иначе путь с пробелами и кириллицей развалится на первом пробеле.
  SELF_ESC="$(printf '%s' "$SELF" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  osascript >/dev/null 2>&1 <<EOF
tell application "Terminal"
    activate
    do script "WHISPERKEY_IN_TERMINAL=1 \\"$SELF_ESC\\""
end tell
EOF
  exit 0
fi

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

# ─── Самообновление ──────────────────────────────────────────────────────────
# Обновление приезжает при каждом запуске, чтобы не переносить папку руками.
# Правила, без которых это опасно:
#   --ff-only  — если в папке есть свои правки, обновление просто не применится
#                вместо того, чтобы их затереть;
#   BatchMode  — ssh не станет спрашивать пароль и ждать ответа вечно;
#   любая неудача — запускаемся на том, что уже лежит: диктовка важнее свежести.
# Отключить: запусти с WHISPERKEY_NO_UPDATE=1 или создай файл .no-update рядом.
if [ -z "${WHISPERKEY_NO_UPDATE:-}" ] && [ ! -f ".no-update" ] && [ -d ".git" ]; then
  if command -v git >/dev/null 2>&1; then
    BEFORE="$(git rev-parse --short HEAD 2>/dev/null)"
    echo "Проверяю обновление..."
    if GIT_TERMINAL_PROMPT=0 \
       GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new" \
       git pull --ff-only --quiet 2>/tmp/whisperkey_update.log; then
      AFTER="$(git rev-parse --short HEAD 2>/dev/null)"
      if [ "$BEFORE" != "$AFTER" ]; then
        echo "Обновлено: $BEFORE → $AFTER"
        git log --oneline "$BEFORE..$AFTER" 2>/dev/null | sed 's/^/   • /'
      else
        echo "Уже последняя версия."
      fi
    else
      # Частые причины: нет сети, свои правки в папке, ключ SSH недоступен.
      echo "Обновиться не вышло — работаю на текущей версии."
      sed 's/^/   /' /tmp/whisperkey_update.log 2>/dev/null | head -3
    fi
  fi
elif [ ! -d ".git" ]; then
  echo "Папка не подключена к репозиторию — обновляться неоткуда."
  echo "Один раз выполни в Терминале, и дальше всё будет само:"
  echo "   git clone git@github.com:Seikatsuma/whisperkey.git ~/Desktop/WhisperKey"
  echo "   и перенеси в новую папку файл .env со своим ключом."
fi

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
