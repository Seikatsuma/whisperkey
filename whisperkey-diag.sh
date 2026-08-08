#!/bin/bash
# Диагностика установки WhisperKey: что где лежит и куда смотрит ярлык.
# Запуск: curl -fsSL https://raw.githubusercontent.com/Seikatsuma/whisperkey/main/whisperkey-diag.sh | bash
echo "═══ ЧТО ГДЕ ЛЕЖИТ ═══"
for d in "$HOME/Desktop/WhisperKey" "$HOME/Desktop/Whisper на MAC" "$HOME/whisperkey"; do
  if [ -d "$d" ]; then
    printf '%-34s ' "$(basename "$d")"
    [ -d "$d/.git" ] && printf 'подключена к GitHub, версия %s' "$(git -C "$d" rev-parse --short HEAD 2>/dev/null)" || printf 'НЕ подключена (обновляться не будет)'
    [ -f "$d/.env" ] && printf ' | ключ есть' || printf ' | КЛЮЧА НЕТ'
    echo
  fi
done
echo
echo "═══ ЯРЛЫК НА РАБОЧЕМ СТОЛЕ ═══"
L="$HOME/Desktop/Запустить WhisperKey.command"
if [ -L "$L" ]; then echo "ссылка -> $(readlink "$L")"
elif [ -f "$L" ]; then echo "обычный файл (копия, не ссылка) — обновляться не будет"
else echo "на рабочем столе ярлыка нет"; fi
echo
echo "═══ КУДА СМОТРИТ ПРОГРАММА ═══"
[ -f "$HOME/.whisperkey_path" ] && echo "запомненный путь: $(cat "$HOME/.whisperkey_path")" || echo "путь ещё не запомнен"
echo
echo "═══ ЧТО ЗАПУЩЕНО ПРЯМО СЕЙЧАС ═══"
ps aux | grep "[w]hisperkey.py" | awk '{for(i=11;i<=NF;i++) printf "%s ", $i; print ""}' | head -3
[ -z "$(ps aux | grep '[w]hisperkey.py')" ] && echo "(не запущено)"
