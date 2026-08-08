#!/bin/bash
# Диагностика установки WhisperKey: что где лежит и куда смотрит ярлык.
# Запуск: curl -fsSL https://raw.githubusercontent.com/Seikatsuma/whisperkey/main/whisperkey-diag.sh | bash
echo "═══ ЧТО ГДЕ ЛЕЖИТ ═══"
# Пути с пробелами и эмодзи в названиях — норма (у Егора «Проэкты 📈»),
# поэтому список собирается через файл, а не через подстановку в for.
LIST=$(mktemp)
echo "$HOME/Desktop/WhisperKey" >> "$LIST"
for r in "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME"; do
  [ -d "$r" ] || continue
  find "$r" -maxdepth 6 -name whisperkey.py 2>/dev/null | while IFS= read -r f; do
    dirname "$f" >> "$LIST"
  done
done
sort -u "$LIST" -o "$LIST"
while IFS= read -r d; do
  [ -d "$d" ] || continue
  printf '%s\n' "$d"
  printf '    '
  if [ -d "$d/.git" ] && git -C "$d" rev-parse --short HEAD >/dev/null 2>&1; then
    printf 'подключена к GitHub, версия %s' "$(git -C "$d" rev-parse --short HEAD 2>/dev/null)"
  elif [ -d "$d/.git" ]; then
    printf 'клон НЕПОЛНЫЙ — обновляться не будет'
  else
    printf 'НЕ подключена — обновляться не будет'
  fi
  [ -f "$d/.env" ] && printf ' | ключ есть' || printf ' | КЛЮЧА НЕТ'
  echo
done < "$LIST"
rm -f "$LIST"

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
ps aux | grep "[Pp]ython.*[w]hisperkey.py" | awk '{for(i=11;i<=NF;i++) printf "%s ", $i; print ""}' | head -3
[ -z "$(ps aux | grep '[Pp]ython.*[w]hisperkey.py')" ] && echo "(не запущено)"
