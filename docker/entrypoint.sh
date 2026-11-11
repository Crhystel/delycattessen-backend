#!/bin/sh
# Punto de entrada de la imagen. Solo el contenedor de la API prepara el entorno
# (BOOTSTRAP=1): aplica migraciones y descarga los modelos faciales la primera vez.
# El worker de Celery arranca directo.
set -e

if [ "${BOOTSTRAP:-0}" = "1" ]; then
    echo "[entrypoint] Aplicando migraciones..."
    python manage.py migrate --noinput

    if [ ! -f models/face_detection_yunet_2023mar.onnx ] || [ ! -f models/face_recognition_sface_2021dec.onnx ]; then
        echo "[entrypoint] Descargando modelos de reconocimiento facial (solo la primera vez)..."
        python manage.py download_biometric_models \
            || echo "[entrypoint] AVISO: no se pudieron descargar los modelos; el reconocimiento facial no funcionará."
    fi
fi

exec "$@"
