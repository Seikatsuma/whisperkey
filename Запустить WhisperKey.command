#!/bin/bash
cd "/Users/alexnbox/Desktop/Курсор/Whisper на MAC" || exit 1
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
