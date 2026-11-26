# Prueba de estrés con k6 (SCRUM-26)

Mide el flujo de compra del cajero en el POS con 15, 20 y 25 cajeros simultáneos, **mientras padres y docentes usan la app**, y busca el límite del sistema con 50, 100 y 150 cajeros.
Es una herramienta externa: la app, Docker y el CI **no la usan**, y si borras esta carpeta el sistema sigue igual.

**Criterio de aceptación:** el 95 % de las compras se completan en menos de 10 s (y se reporta cuántas lo hacen en menos de 5 s) con 15–25 usuarios, con resultados documentados.

## Qué hace cada rol

| Rol | Qué hace en la prueba |
|---|---|
| **Cajero** (los 15/20/25) | Identifica al cliente por QR (`POST /api/pos/identify/qr/`) y cobra (`POST /api/pos/sales/`). Esa pareja es una **compra**. Un estudiante paga con su billetera; un docente, a crédito (20 % de las compras). |
| **Padre** | Ve su perfil, sus hijos, el saldo y movimientos de la billetera, el menú, sus preórdenes y las compras de su hijo. En 1 de cada 5 visitas **crea una preorden**. |
| **Docente** | Ve su perfil y su consumo a crédito, y muestra su QR. También compra en el POS (arriba). |

El QR del cliente (`GET /api/users/qr/token/`) se pide aparte y no cuenta para el tiempo de la compra.
Quedan fuera las **recargas con tarjeta**, porque llaman a Payphone y Kushki reales.

**Usuarios simultáneos por nivel** (con las proporciones por defecto): cajeros × 1 + padres × 1 + docentes × 0,5.
Por ejemplo, el nivel "25" son 25 cajeros + 25 padres + 13 docentes = **63 usuarios virtuales**. Con `LOAD_PARENT_RATIO=0` y `LOAD_TEACHER_RATIO=0` queda solo la línea base de cajeros.

## Contenido

| Archivo | Para qué sirve |
|---|---|
| `pos_compra.js` | Escenario k6 (perfiles `carga` y `estres`; cajeros, padres y docentes). |
| `seed_carga.py` | Crea los datos de prueba en la sede "Carga". |
| `limpiar_carga.py` | Borra todo lo que creó la prueba. |
| `resultados/` | Reportes HTML generados (no se suben a Git). |

## 1. Variables en el `.env`

Pega estas líneas al final del `.env` del backend (el mismo que usa Docker). Las otras partes del sistema las ignoran. Usa una contraseña de prueba, no una real.

```
# --- Load test (k6, SCRUM-26): solo las lee pruebas-carga/ ---
LOAD_BASE_URL=http://localhost:8000
LOAD_PASSWORD=pon-aqui-una-clave-de-prueba
LOAD_CASHIERS=25
LOAD_STUDENTS=50
LOAD_STEP_SECONDS=120
```

Opcionales (los valores por defecto sirven; cámbialos solo si lo necesitas):

| Variable | Defecto | Para qué |
|---|---|---|
| `LOAD_PARENTS` | `25` | Cuentas de padres a sembrar (cada una con sus hijos). |
| `LOAD_TEACHERS` | `12` | Cuentas de docentes a sembrar. |
| `LOAD_PARENT_RATIO` | `1` | Padres conectados por cada cajero. `0` = sin padres. |
| `LOAD_TEACHER_RATIO` | `0.5` | Docentes conectados por cada cajero. `0` = sin docentes. |
| `LOAD_TEACHER_SHARE` | `0.2` | Parte de las compras del POS que hace un docente. |
| `LOAD_PREORDER_SHARE` | `0.2` | Parte de las visitas de un padre que terminan en preorden. |
| `LOAD_THINK_SECONDS` | `3` | Pausa media del cajero entre compras. Con `10` simula un cajero real. |
| `LOAD_PARENT_THINK_SECONDS` | `6` | Pausa media del padre entre visitas. |
| `LOAD_TEACHER_THINK_SECONDS` | `10` | Pausa media del docente entre visitas. |
| `LOAD_PROFILE` | `carga` | `carga` = 15/20/25 cajeros; `estres` = 50/100/150. |

Cualquiera se puede cambiar sin editar el `.env`, agregando `-e VARIABLE=valor` al comando de k6.

## 2. Instalar k6

```powershell
winget install k6 --source winget
```

Cierra y abre la terminal, y comprueba con `k6 version`.

## 3. Sembrar los datos de prueba

Con Docker activo, desde la carpeta del backend:

```powershell
Get-Content pruebas-carga/seed_carga.py | docker compose exec -T api python manage.py shell
```

Crea la sede **"Carga (prueba de estres)"** con cajeros `carga_cajero_NN`, docentes `carga_docente_NN`, padres `carga_padre_NN`, estudiantes `carga_estudiante_NN` (repartidos entre los padres, cada uno con billetera), 5 productos con stock alto y **menús publicados** del mes actual y del siguiente para poder preordenar.
Se puede repetir sin problema: reinicia saldos y stock. **Vuelve a ejecutarlo el día de la prueba**, porque los menús cubren el mes en que se sembró.

## 4. Servidor de producción (gunicorn) sin apagar tu API

`runserver` es el servidor de desarrollo. Para medir con el mismo servidor que usa producción (gunicorn, 2 workers), levanta un contenedor temporal **al lado** de tu API, en el puerto 8001. Tu API normal no se toca:

```powershell
docker compose run --rm -d --no-deps -e BOOTSTRAP=0 -p 8001:8000 --name carga-gunicorn api gunicorn delycattessen_backend.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60
```

Después apunta k6 a ese puerto con `-e LOAD_BASE_URL=http://localhost:8001` (o cambiando esa línea del `.env`). Al terminar, apágalo:

```powershell
docker stop carga-gunicorn
```

## 5. Corridas a ejecutar

El panel en vivo queda en <http://localhost:5665> mientras corre, y el reporte se guarda al terminar. Cada corrida de perfil `carga` dura unos 8 minutos.

| # | Corrida | Qué demuestra |
|---|---|---|
| 1–3 | **Exigente** (cajeros con pausa de 3 s), 3 veces | Repetibilidad del resultado |
| 4 | **Realista** (cajeros con pausa de 10 s) | Un cajero real |
| 5 | **Estrés** (50, 100 y 150 cajeros) | Dónde se rompe el sistema |
| 6 | **Solo cajeros** (opcional) | Cuánto pesan padres y docentes |

Exigente (repite 3 veces cambiando el nombre del reporte: `-1`, `-2`, `-3`):

```powershell
k6 run -e LOAD_BASE_URL=http://localhost:8001 --out web-dashboard=export=pruebas-carga/resultados/reporte-exigente-1.html pruebas-carga/pos_compra.js
```

Realista:

```powershell
k6 run -e LOAD_BASE_URL=http://localhost:8001 -e LOAD_THINK_SECONDS=10 --out web-dashboard=export=pruebas-carga/resultados/reporte-realista.html pruebas-carga/pos_compra.js
```

Solo cajeros (línea base):

```powershell
k6 run -e LOAD_BASE_URL=http://localhost:8001 -e LOAD_PARENT_RATIO=0 -e LOAD_TEACHER_RATIO=0 --out web-dashboard=export=pruebas-carga/resultados/reporte-solo-cajeros.html pruebas-carga/pos_compra.js
```

Estrés. Con 150 cajeros + 150 padres + 75 docentes son unos 375 usuarios virtuales: es mucho para un solo equipo, así que aquí conviene bajar padres y docentes. Antes pon `LOAD_STUDENTS=150`, `LOAD_CASHIERS=150` y `LOAD_PARENTS=75` en el `.env` y vuelve a sembrar (paso 3):

```powershell
k6 run -e LOAD_BASE_URL=http://localhost:8001 -e LOAD_PROFILE=estres -e LOAD_PARENT_RATIO=0.5 -e LOAD_TEACHER_RATIO=0.25 --out web-dashboard=export=pruebas-carga/resultados/reporte-estres-limite.html pruebas-carga/pos_compra.js
```

## 6. Cómo leer el resultado

El bloque `THRESHOLDS` del resumen trae el total y **cada nivel por separado** (`{users:15}`, `{users:20}`, `{users:25}`), y además la salud de padres y docentes:

```
compra_duration
✓ 'p(95)<10000' p(95)=2.56s           <- total
  {users:15}  ✓ p(95)=867ms
  {users:20}  ✓ p(95)=1.49s
  {users:25}  ✓ p(95)=2.55s
http_req_duration{role:parent}
✓ 'p(95)<5000' p(95)=1.39s            <- pantallas del padre
```

- **Criterio de aceptación (cajeros):** en el perfil `carga`, `compra_duration` y `compra_completed` deben quedar con `✓`.
- **Padres y docentes:** los umbrales `role:parent` y `role:teacher` (p95 menor a 5 s y menos de 1 % de errores) los fijó el equipo; no vienen del criterio de Jira. Declara esa diferencia en el documento.
- **Estrés:** el primer nivel con `✗` es **el límite**: ahí el p95 supera 10 s o más del 1 % de las compras fallan. Si todos pasan, el límite está por encima de 150 cajeros.
- `compra_under_5s` dice cuántas compras tardaron menos de 5 s.

## 7. Capturas para el documento (sección 7.3)

| # | Qué capturar | Cuándo |
|---|---|---|
| 1 | Terminal con `docker compose ps` (contenedores arriba, incluido `carga-gunicorn`) y una línea con CPU y RAM del equipo | Antes de empezar |
| 2 | Panel en vivo, pestaña *Overview* completa (con el gráfico de VUs) | En plena rampa |
| 3 | Resumen final en terminal: bloque `THRESHOLDS` con los niveles `{users:N}` y los roles | Al terminar |
| 4 | Reporte HTML: gráfico de `compra_duration` y tabla por etiqueta (`identify`, `sale`, `parent_*`, `teacher_*`) | Abrir el `.html` |
| 5 | Resumen de la corrida de estrés con el nivel donde cambia el resultado | Corrida 5 |

## 8. Cómo entra en 7.3

- **Estrategia de pruebas:** prueba de carga con k6 (15–25 cajeros con padres y docentes en paralelo) y de estrés (50–150), con gunicorn en Docker local.
- **Trazabilidad:** `RNF rendimiento → PERF-01 (carga) y PERF-02 (estrés) → Aprobado/Fallido → Figuras X–Y`.
- **PERF-01:** precondiciones (Docker activo, datos sembrados el mismo día, variables `LOAD_*`), pasos (comando de la corrida), entrada (15/20/25 cajeros + padres y docentes, 120 s por nivel), esperado (p95 de la compra menor a 10 s y menos de 1 % de errores), obtenido (valores por nivel).
- **PERF-02:** igual, con 50/100/150 cajeros; el resultado es el límite encontrado.
- **Resultados por iteración:** una fila por corrida (1, 2, 3, realista, estrés, solo cajeros) y por nivel, con usuarios, compras, p50/p95/p99, % de error y veredicto.
- **Limitaciones a declarar:** se midió en una máquina local, no en producción; k6 y el servidor comparten el mismo equipo; no se probaron recargas con tarjeta (pasarelas externas).

## 9. Limpiar los datos al terminar

Borra la sede "Carga", sus usuarios (cajeros, docentes, padres, estudiantes), saldos, productos, menús, preórdenes y ventas. Se niega a correr si la sede tiene algo que no sea de la prueba, y si algo falla no borra nada:

```powershell
Get-Content pruebas-carga/limpiar_carga.py | docker compose exec -T api python manage.py shell
```

## Problemas frecuentes

- `LOAD_PASSWORD is not set`: falta pegar las líneas del paso 1.
- `Login failed ... Did you run the seed`: falta el paso 3, la contraseña del `.env` cambió después de sembrar, o `LOAD_PARENTS`/`LOAD_TEACHERS`/`LOAD_CASHIERS` piden más cuentas de las sembradas (vuelve a ejecutar el paso 3).
- Errores 400 en `sale` o `pre-order` a mitad de la corrida: se agotó el saldo o el stock, o los menús ya no cubren la fecha; vuelve a sembrar.
- `port is already allocated` al levantar gunicorn: el contenedor `carga-gunicorn` ya existe; ejecuta `docker stop carga-gunicorn`.
- `service "api" is not running`: tu stack de Docker está apagado; levántalo con `docker compose --profile full up -d`.
- El panel no abre: revisa que el puerto 5665 esté libre.
