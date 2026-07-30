# D'Elycattessen - Backend

Backend del sistema de gestión de ventas y control de servicios alimentarios de D'Elycattessen, desarrollado en Django bajo arquitectura MVC. Este repositorio contiene únicamente el backend (API REST). Los repositorios de la interfaz web (React) y la aplicación móvil (Flutter) son independientes.

## Stack técnico

- Python 3.12
- Django + Django REST Framework
- PostgreSQL (base de datos principal)
- Redis (caché)
- RabbitMQ (cola de mensajería asíncrona)
- Autenticación con JWT (djangorestframework-simplejwt)
- Docker (para Redis y RabbitMQ en desarrollo local)

## Estructura de apps

El backend está organizado en 6 apps, cada una correspondiente a un módulo del sistema:

- `usuarios` - Gestión de acceso, registro y perfil de usuario
- `catalogo` - Catálogo de productos e inventario
- `billetera` - Billetera digital y recargas autónomas
- `perfiles` - Control parental y gestión de alérgenos
- `punto_venta` - Identificación biométrica, QR y procesamiento de cobros
- `reportes` - Analítica automatizada de ventas

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

### 4. Configurar variables de entorno

Crear un archivo `.env` en la raíz del proyecto, con este contenido (ajustar valores según tu entorno local):

```
DB_NAME=delycattessen_db
DB_USER=postgres
DB_PASSWORD=tu_clave_local
DB_HOST=localhost
DB_PORT=5432
SECRET_KEY=tu_clave_secreta_de_django
```

Generar una `SECRET_KEY` con:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

El archivo `.env` no se sube al repositorio. Cada persona del equipo debe crear el suyo localmente.

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

Deben aparecer dos contenedores: uno de Redis (puerto 6379) y uno de RabbitMQ (puertos 5672 y 15672, este último para el panel de administración web de RabbitMQ).

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