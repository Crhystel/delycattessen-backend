# Imagen del backend (API + worker de Celery). La construye el CI y se publica en GHCR.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Usuario sin privilegios: el contenedor no corre como root.
RUN useradd --create-home --uid 1000 app

# Primero solo las dependencias: esta capa se reutiliza mientras requirements.txt no cambie.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Normaliza fin de línea (por si se editó en Windows) y prepara carpetas escribibles:
# models/ (modelos faciales descargados al primer arranque) y media/ (fotos subidas).
RUN sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod +x docker/entrypoint.sh \
    && mkdir -p /app/models /app/media \
    && chown -R app:app /app/models /app/media

USER app

EXPOSE 8000

# "sh" explícito: también funciona cuando el código se monta desde Windows (docker-compose dev).
ENTRYPOINT ["sh", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "delycattessen_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
