# D'Elycattessen - Backend

Backend del sistema de gestión de ventas y control de servicios alimentarios de D'Elycattessen, desarrollado en Django con Django REST Framework (la lógica de negocio vive en `services.py` de cada app). Este repositorio contiene únicamente el backend (API REST). Los repositorios de la interfaz web (React) y la aplicación móvil (Flutter) son independientes.

## Stack técnico

- Python 3.12
- Django + Django REST Framework
- PostgreSQL (base de datos principal)
- Redis (caché compartida, opcional: se activa con `REDIS_URL`)
- RabbitMQ (cola de mensajería asíncrona)
- Autenticación con JWT (djangorestframework-simplejwt)
- Docker (para Redis y RabbitMQ en desarrollo local)

La arquitectura completa (apps, dependencias, flujos y decisiones) está en [ARCHITECTURE.md](ARCHITECTURE.md).

## Estructura de apps

El backend está organizado en 5 apps, cada una correspondiente a un módulo del sistema:

- `users` - Gestión de acceso, registro y perfil de usuario
- `catalog` - Catálogo de productos e inventario
- `wallet` - Billetera digital y recargas autónomas
- `pos` - Identificación biométrica, QR y procesamiento de cobros
- `core` - Código compartido entre apps (mixins, utilidades de zona horaria)

## Requisitos previos

- Python 3.12 o superior
- PostgreSQL instalado y corriendo localmente
- Docker Desktop instalado y corriendo
- Git

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/<usuario>/delycattessen-backend.git
cd delycattessen-backend
```

### 2. Crear y activar el entorno virtual

```bash
python -m venv venv
```

Windows:

```bash
venv\Scripts\activate
```

Mac/Linux:

```bash
source venv/bin/activate
```

### 3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3.1 Descargar modelos de reconocimiento facial

Los modelos de detección (YuNet) y reconocimiento facial (SFace) deben descargarse antes de iniciar el sistema:

```bash
python manage.py download_biometric_models
```

### 4. Configurar variables de entorno

Copiar `.env.example` a `.env` y completar los valores reales (el archivo `.env` nunca se sube a git). Para desarrollo local agregar además:

```
DEBUG=True
ALLOWED_HOSTS=*
CORS_ALLOW_ALL_ORIGINS=True
```

Sin esas líneas la app arranca en modo seguro (`DEBUG=False`): no sirve las imágenes de `/media/` y solo acepta `localhost` y `127.0.0.1`. En producción usar los valores de `.env.example` (hosts y orígenes reales, `HTTPS_ONLY=True`).

`SECRET_KEY` es obligatoria: sin ella el proyecto no arranca. Generar una (y otra para `BIOMETRIC_ENCRYPTION_KEY`) con:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### 5. Crear la base de datos en PostgreSQL

Conectarse a PostgreSQL:

```bash
psql -U postgres
```

Crear la base de datos:

```sql
CREATE DATABASE delycattessen_db;
```

### 6. Levantar Redis y RabbitMQ con Docker

Con Docker Desktop abierto y corriendo:

```bash
docker-compose up -d
```

Verificar que ambos contenedores estén activos:

```bash
docker ps
```

Deben aparecer dos contenedores: uno de Redis (puerto 6379) y uno de RabbitMQ (puertos 5672 y 15672, este último para el panel de administración web en `http://localhost:15672`). Los puertos solo escuchan en `localhost` y cada contenedor tiene un límite de memoria (Redis 128 MB, RabbitMQ 512 MB).

Para usar Redis como caché compartida (códigos de recuperación de contraseña y QR entre varios procesos) agregar al `.env`: `REDIS_URL=redis://localhost:6379/1`. Sin esa variable Django usa su caché local en memoria.

### 7. Aplicar las migraciones

```bash
python manage.py migrate
```

### 8. Crear un superusuario (opcional, para acceder al panel admin de Django)

```bash
python manage.py createsuperuser
```

### 9. Levantar el servidor

```bash
python manage.py runserver
```

El backend queda disponible en `http://127.0.0.1:8000/`. El panel admin de Django está en `http://127.0.0.1:8000/admin/`.

## Detener los servicios de Docker

```bash
docker-compose down
```

## Levantar todo el backend con un solo comando (Docker)

Alternativa a los pasos 5 a 9: en vez de abrir Postgres, `runserver` y Celery por separado, se levanta todo en contenedores. Requiere Docker Desktop y el `.env` del paso 4 (sus `DB_NAME`, `DB_USER` y `DB_PASSWORD` son las credenciales de la base del contenedor).

```bash
docker compose --profile full up -d --build
```

Esto inicia Postgres, la API (`0.0.0.0:8000`, con recarga automática al editar código), el worker de Celery, Redis y RabbitMQ. La API aplica las migraciones y descarga los modelos faciales sola la primera vez. Un único proceso atiende a todos los clientes:

| Cliente | URL del backend |
|---|---|
| Web (navegador) | `http://localhost:8000` |
| Emulador Android | `http://10.0.2.2:8000` |
| Celular en la misma red Wi-Fi | `http://<IP-de-tu-PC>:8000` (permitir el puerto 8000 en el firewall) |

Sin el perfil `full`, `docker compose up -d` sigue levantando solo Redis y RabbitMQ, y la API y Celery se corren a mano como antes.

Consumo aproximado: 1,5 a 2 GB de RAM entre todos los contenedores (cada uno tiene un tope de memoria). En equipos con poca RAM, limita Docker Desktop creando `%UserProfile%\.wslconfig`:

```
[wsl2]
memory=3GB
processors=2
```

y reinicia WSL con `wsl --shutdown`. Si el equipo no da, usa el flujo sin Docker de los pasos 5 a 9.

Comandos útiles: `docker compose logs -f api worker` (ver logs) y `docker compose --profile full down` (detener; los datos de Postgres se conservan en un volumen).

## Pipeline CI/CD

El pipeline vive en `.github/workflows/ci.yml` y se ve por etapas en la pestaña Actions de GitHub:

```
1 · Lint y checks ─┐
2 · Tests ─────────┼─► 4 · Imagen Docker ─► 5 · Deploy a staging (rama develop)
3 · Seguridad ─────┘                      └► 6 · Deploy a producción (rama main, con aprobación)
```

- **Se dispara** en cada push o pull request hacia `main` y `develop` (no corre si solo cambian archivos `.md`; un push nuevo cancela la corrida anterior).
- **1 · Lint y checks:** sintaxis, `manage.py check` y que no falten migraciones.
- **2 · Tests:** `pytest` con cobertura contra un Postgres de servicio; sube `coverage.xml` y muestra el porcentaje en el resumen.
- **3 · Seguridad:** `pip-audit` informa las vulnerabilidades conocidas de `requirements.txt` en el resumen del job (no bloquea el pipeline).
- **4 · Imagen Docker:** construye la imagen del `Dockerfile`. En un pull request solo la valida; en push a `develop` o `main` la publica en GHCR (`ghcr.io/crhystel/delycattessen-backend`, tags `develop`, `latest` y el SHA).
- **5 y 6 · Deploy:** usan los entornos `staging` y `production` de GitHub. En `production` conviene activar *Required reviewers* para exigir aprobación manual.

El CI **no usa Secrets reales**: las variables de entorno de los tests son valores falsos escritos en el workflow.

### Staging / semiproducción en una PC

Cualquier PC con Docker puede correr la imagen que publicó el CI, sin instalar Python ni Postgres:

```bash
cp .env.staging.example .env.staging        # completa los valores (nunca se sube a git)
docker compose -f docker-compose.staging.yml --env-file .env.staging pull
docker compose -f docker-compose.staging.yml --env-file .env.staging up -d
```

`BACKEND_TAG=develop` (por defecto) usa la versión de `develop`; `latest` usa la de `main`. Para exponerlo con HTTPS sin pagar servidor: `cloudflared tunnel --url http://localhost:8000` y poner el dominio resultante en `ALLOWED_HOSTS` (y `HTTPS_ONLY=True`) de `.env.staging`. Usa solo claves de **sandbox** de Kushki y Payphone.

### Despliegue automático por SSH (opcional)

Si el staging está en un servidor, define en el entorno `staging` de GitHub (Settings > Environments) la variable `DEPLOY_HOST` y los secretos `DEPLOY_USER`, `DEPLOY_PATH` y `DEPLOY_SSH_KEY`. El job 5 entonces ejecuta `pull` y `up -d` por SSH. Sin esas variables el paso se omite y el despliegue es manual.

## Notas

- No modificar `settings.py` para incluir valores por defecto de credenciales o configuración sensible. Todo debe leerse desde variables de entorno.
- El archivo `requirements.txt` debe regenerarse después de instalar cualquier librería nueva, con `pip freeze > requirements.txt`, y subirse junto con el cambio correspondiente.

## Rotar claves

`SECRET_KEY` firma los JWT; la clave de los vectores faciales es independiente (`BIOMETRIC_ENCRYPTION_KEY`). Para cambiar la clave biométrica sin perder los rostros registrados:

```bash
# 1. Poner la clave nueva en BIOMETRIC_ENCRYPTION_KEY (.env) y exportar la anterior solo en la terminal
export OLD_BIOMETRIC_SEED="clave_anterior"
# 2. Verificar sin escribir y luego aplicar
python manage.py rotate_biometric_key --dry-run
python manage.py rotate_biometric_key
```

Hacer un respaldo de la base de datos antes. Cambiar `SECRET_KEY` cierra todas las sesiones (los usuarios deben iniciar sesión de nuevo); en CI actualizar el secret `SECRET_KEY` de GitHub.
