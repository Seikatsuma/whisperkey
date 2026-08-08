#!/bin/bash
# Разовая настройка WhisperKey на Маке: подключает папку к репозиторию,
# переносит ключ и ставит ярлык. Дальше программа обновляется сама при запуске.
# Запуск: curl -fsSL https://raw.githubusercontent.com/Seikatsuma/whisperkey/main/setup-mac.sh | bash
set -u
NEW="$HOME/Desktop/WhisperKey"

echo "── Ищу нынешнюю папку ──"
# Глубина 6: папка может лежать внутри нескольких вложенных, в том числе
# с эмодзи в названии — так у Егора и оказалось (Desktop/Проэкты/Рабочие проэкты/…),
# а прежние 3 уровня её не доставали, и ключ не переносился.
OLD=""
for d in "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME"; do
  [ -d "$d" ] || continue
  F=$(find "$d" -maxdepth 6 -name whisperkey.py -not -path "$NEW/*" 2>/dev/null | head -1)
  if [ -n "$F" ]; then OLD=$(dirname "$F"); break; fi
done
[ -n "$OLD" ] && echo "  нашёл: $OLD" || echo "  не нашёл — продолжаю без переноса ключа"

echo "── Ставлю подключённую папку ──"
if [ -d "$NEW/.git" ] && git -C "$NEW" rev-parse HEAD >/dev/null 2>&1; then
  git -C "$NEW" pull --ff-only --quiet && echo "  обновлено до $(git -C "$NEW" rev-parse --short HEAD)"
else
  # Сюда попадаем и когда папки нет, и когда прошлая попытка оставила её битой.
  [ -e "$NEW" ] && { mv "$NEW" "$NEW-битая-$(date +%H%M%S)"; echo "  прошлая попытка была неполной, отложил её в сторону"; }
  if ! git clone --quiet https://github.com/Seikatsuma/whisperkey.git "$NEW"; then
    echo "  ОШИБКА: не удалось скачать. Проверь интернет и попробуй ещё раз."; exit 1
  fi
  echo "  скачано: $(git -C "$NEW" rev-parse --short HEAD)"
fi

# Проверяем, что скачалось именно то, а не пустая папка.
if [ ! -f "$NEW/whisperkey.py" ]; then
  echo "  ОШИБКА: в папке нет whisperkey.py. Напиши мне, разберёмся."; exit 1
fi

echo "── Переношу ключ ──"
if [ -f "$NEW/.env" ]; then
  echo "  уже на месте"
elif [ -n "$OLD" ] && [ -f "$OLD/.env" ]; then
  cp "$OLD/.env" "$NEW/.env" && echo "  перенесён из старой папки"
else
  echo "  ВНИМАНИЕ: .env не найден — без него программа не заработает. Напиши мне."
fi

chmod +x "$NEW"/*.command 2>/dev/null
ln -sfn "$NEW/Запустить WhisperKey.command" "$HOME/Desktop/Запустить WhisperKey.command" 2>/dev/null
# Путь, запомненный старой копией, сбрасываем: иначе ярлык уведёт обратно в неё.
echo "$NEW" > "$HOME/.whisperkey_path"

echo
if pgrep -f "[Pp]ython.*whisperkey\.py" >/dev/null 2>&1; then
  echo "⚠️  СТАРАЯ ВЕРСИЯ СЕЙЧАС ЗАПУЩЕНА."
  echo "   Закрой её окно Терминала перед запуском новой — программа не пускает"
  echo "   две копии одновременно, и новая просто не стартует."
  echo
fi
echo "ГОТОВО. Запускай ярлык «Запустить WhisperKey» с рабочего стола."
echo "Дальше он обновляется сам при каждом запуске."
