#!/bin/bash
# Переходим в папку, где лежит сам скрипт
cd "$(dirname "$0")" || exit 1

# Если мы не в той папке (например, скрипт скопирован на рабочий стол), 
# пробуем перейти по абсолютному пути
if [ ! -f "whisperkey.py" ]; then
  cd "/Users/alexnbox/Desktop/Проэкты 📈/Рабочие проэкты 🏛️/Whisper на MAC" || exit 1
fi
export KMP_DUPLICATE_LIB_OK=TRUE
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
echo "WhisperKey — запуск..."
PY=""
for candidate in /usr/local/bin/python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Ошибка: python3 не найден. Установи Python 3.10+."
  read -r -p "Enter для выхода..."
  exit 1
fi
echo "Python: $($PY --version)"
exec "$PY" whisperkey.py
