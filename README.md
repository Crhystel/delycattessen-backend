# D'Elycattessen - Backend

Backend del sistema de gestión de ventas y control de servicios alimentarios de D'Elycattessen, desarrollado en Django bajo arquitectura MVC. Este repositorio contiene únicamente el backend (API REST). Los repositorios de la interfaz web (React) y la aplicación móvil (Flutter) son independientes.

## Stack técnico

- Python 3.12
- Django + Django REST Framework
- PostgreSQL (base de datos principal)
- Redis (caché compartida, opcional: se activa con `REDIS_URL`)
- RabbitMQ (cola de mensajería asíncrona)
- Autenticación con JWT (djangorestframework-simplejwt)
- Docker (para Redis y RabbitMQ en desarrollo local)

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

## Pipeline CI/CD

El repositorio tiene un pipeline configurado en `.github/workflows/ci.yml`, que se ejecuta automáticamente en cada push o pull request hacia las ramas `main` y `develop`. El pipeline instala las dependencias del proyecto y ejecuta las verificaciones de Django (`python manage.py check`).

Las variables de entorno necesarias para el pipeline están configuradas como Secrets del repositorio en GitHub (Settings > Secrets and variables > Actions), no en el código ni en archivos versionados.

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
