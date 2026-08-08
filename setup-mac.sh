#!/bin/bash
# Разовая настройка WhisperKey на Маке: подключает папку к репозиторию,
# переносит ключ и ставит ярлык. Дальше программа обновляется сама при запуске.
# Запуск: curl -fsSL https://raw.githubusercontent.com/Seikatsuma/whisperkey/main/setup-mac.sh | bash

set -u
NEW="$HOME/Desktop/WhisperKey"

# 1. Ищем нынешнюю папку по самому файлу программы — путь знать не нужно.
OLD=""
for d in "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME"; do
  [ -d "$d" ] || continue
  F=$(find "$d" -maxdepth 3 -name whisperkey.py -not -path "*/WhisperKey/*" 2>/dev/null | head -1)
  if [ -n "$F" ]; then OLD=$(dirname "$F"); break; fi
done
if [ -n "$OLD" ]; then echo "Нашёл нынешнюю папку: $OLD"; else echo "Старой папки не нашёл — продолжаю без переноса ключа."; fi

# 2. Ставим новую папку из GitHub. Если она уже есть — просто обновляем.
if [ -d "$NEW/.git" ]; then
  echo "Папка $NEW уже подключена — обновляю."
  git -C "$NEW" pull --ff-only --quiet && echo "Обновлено."
else
  [ -e "$NEW" ] && mv "$NEW" "$NEW-старая-$(date +%H%M)"
  git clone --quiet https://github.com/Seikatsuma/whisperkey.git "$NEW" || { echo "ОШИБКА: не удалось скачать."; exit 1; }
  echo "Скачано в $NEW"
fi

# 3. Переносим ключ. Без него программа не заработает.
if [ -n "$OLD" ] && [ -f "$OLD/.env" ] && [ ! -f "$NEW/.env" ]; then
  cp "$OLD/.env" "$NEW/.env" && echo "Ключ перенесён."
elif [ -f "$NEW/.env" ]; then
  echo "Ключ уже на месте."
else
  echo "ВНИМАНИЕ: файл .env не найден — скажи мне, помогу."
fi

# 4. Права на запуск и ярлык на рабочий стол.
chmod +x "$NEW"/*.command 2>/dev/null
ln -sfn "$NEW/Запустить WhisperKey.command" "$HOME/Desktop/Запустить WhisperKey.command" 2>/dev/null

echo
echo "ГОТОВО. Запускай ярлык «Запустить WhisperKey» с рабочего стола."
echo "Дальше он обновляется сам при каждом запуске — переносить ничего не нужно."
