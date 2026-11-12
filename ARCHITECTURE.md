# Arquitectura del backend — D'Elycattessen

API REST del sistema de ventas y control de servicios alimentarios de D'Elycattessen. Atiende a una app web (React) y una app móvil (Flutter), que viven en repositorios aparte.

## 1. Qué hace el sistema

- **Padres** registran a sus hijos (estudiantes), recargan una billetera, preordenan el menú del día, configuran controles parentales y alergias.
- **Estudiantes** se identifican en el punto de venta (POS) con **reconocimiento facial** o un **QR dinámico**; los **docentes** solo con el QR dinámico (no tienen biometría) y compran en efectivo o a crédito.
- **Personal operativo** vende en el POS (efectivo, saldo, crédito docente) y entrega preórdenes.
- **Administradores** gestionan el personal, el catálogo y los menús mensuales de su institución.

Roles (`users.CustomUser.Role`): `ADMIN`, `OPERATIONS_STAFF`, `TEACHER`, `PARENT`, `STUDENT`.

## 2. Stack

| Pieza | Uso |
|---|---|
| Python 3.12, Django 6, Django REST Framework | API |
| PostgreSQL | Base de datos principal |
| JWT (`simplejwt`) | Autenticación; login por correo |
| Celery + RabbitMQ | Tareas asíncronas (correos, cobros con Kushki) |
| Redis (opcional, `REDIS_URL`) | Caché compartida: códigos de reset y nonces de QR |
| Kushki / Payphone | Pasarelas de pago para recargas |
| OpenCV (YuNet + SFace) + AES-256-GCM | Biometría facial; solo se guarda el vector cifrado |
| whitenoise · django-axes | Estáticos del admin con gunicorn · bloqueo del login del admin tras varios intentos fallidos |
| pytest + pytest-django + xdist | Tests |

Toda la configuración sale de variables de entorno (`python-decouple`); la plantilla es `.env.example`.

## 3. Apps y dependencias

```
core      código compartido, sin modelos
  ▲
users ◄── catalog ◄── wallet ◄── pos
```

Una flecha `A ◄── B` significa "B puede importar de A". **No hay imports circulares entre apps**; todo lo que dos apps necesitan va en `core`.

| App | Responsabilidad | Archivos clave |
|---|---|---|
| `core` | Aislamiento por institución, mixins de serializers, zona horaria, biometría facial | `mixins.py`, `serializer_mixins.py`, `timezone.py`, `biometrics.py` |
| `users` | Cuentas, perfiles, instituciones, alergias, control parental, PIN, QR, registro biométrico | `models.py`, `services.py`, `permissions.py`, `views.py` |
| `catalog` | Alérgenos, ingredientes, productos, platillos, menús mensuales y diarios y combos | `models.py`, `services.py`, `views.py` |
| `wallet` | Billetera, transacciones, recargas con pasarelas | `services.py`, `tasks.py`, `views.py` |
| `pos` | Preórdenes, ventas, identificación facial/QR, historiales | `services.py`, `mixins.py`, `views.py` |

### Modelo de datos (resumen)

- **users**: `Institution` (única fuente de verdad de sede) · `CustomUser` (rol, institución, nombre en 4 partes) · `ParentProfile` · `StudentProfile` (padre, institución, foto) · `ParentalControl` (límite diario, días permitidos y aviso de saldo bajo: `low_balance_alert_enabled`/`low_balance_alert_amount`; el listado de hijos devuelve `low_balance_alert_amount`, `null` si el aviso está desactivado) · `UserBiometric` (vector cifrado: `encrypted_embedding`, `nonce`, `tag`).
- **catalog**: `Allergen` · `Ingredient` (alérgenos) · `MenuItem` (producto del POS, con stock) · `MenuDish` (platillo de preorden) · `MonthlyMenu` (borrador/publicado) → `DailyMenu` · `Combo` → `ComboItem` (productos a precio fijo, con fechas de vigencia `ValidityPeriod`).
- **wallet**: `Wallet` (1 a 1 con estudiante, saldo) · `Transaction` (recarga o consumo, pasarela, estado, enlace a `PreOrder` o `Sale`).
- **pos**: `PreOrder` → `PreOrderItem` (el listado de preórdenes y el historial de compras del padre incluyen la foto, `image`, de cada producto) · `Sale` (efectivo, crédito o saldo) → `SaleItem` · `ReportExport` (archivo de reporte en cola) · `CreditStatement` (estado de cuenta mensual enviado a un docente).

## 4. Endpoints

| Prefijo | Contenido |
|---|---|
| `/api/users/` | Registro de padre y estudiante, login, `me` (y `me/profile/` para que padre y docente editen sus datos; cambiar el correo exige la contraseña actual en `current_password`), `change-password/`, hijos (`children/<id>/` para que el padre edite nombres y usuario; `children/<id>/password/` para que cambie la contraseña de su hijo confirmando con la suya, cierra las sesiones del hijo y avisa por correo al padre sin la contraseña; límite `CHILD_ACCOUNT_THROTTLE_RATE`), alergias, control parental, PIN de pago, QR (`qr/token/`), biometría (`biometrics/register/`), reset de contraseña, gestión de personal (`staff/`) |
| `/api/catalog/` | Menú, productos, ingredientes, alérgenos, platillos, menús mensuales (publicar/despublicar), menú del estudiante y del POS y combos (`combos/`, vigentes en `combos/active/`) |
| `/api/wallet/` | `recharge/`, `recharge/gateway/`, transacciones, callbacks de Payphone, webhook de Kushki |
| `/api/pos/` | Preórdenes, ventas, historial, crédito docente, `identify/face/`, `identify/qr/`, analítica de ventas para el administrador (`analytics/summary/`, `analytics/timeseries/`, `analytics/top-products/`, `analytics/top-combos/`) y reporte mensual de crédito docente (`reports/teacher-credit/?month=YYYY-MM`, con su exportación a Excel o PDF por cola en `reports/teacher-credit/exports/?month=YYYY-MM&file_type=xlsx|pdf`) |
| `/api/token/` | Login JWT y refresh |

## 5. Flujos clave

### 5.1 Recarga de saldo
`RechargeService.start()` (en `wallet/services.py`) elige la pasarela con `GatewayRouter` según el monto y `WALLET_RECHARGE_GATEWAY_THRESHOLD`:

- **Monto menor al umbral → Payphone**: `PayphonePreparer.prepare()` crea la transacción pendiente y devuelve una `payment_url` para un WebView. Payphone llama al callback y `confirm()` verifica con Payphone y acredita el saldo (idempotente, con bloqueo de fila).
- **Monto mayor o igual → Kushki**: el frontend tokeniza la tarjeta; `RechargeService` crea la transacción pendiente **antes** de encolar y la tarea Celery `process_recharge_task(transaction_id, …)` llama a `KushkiGateway.process_recharge(transaction=…)` (Template Method: validar → pendiente → cobrar → acreditar → correo). Solo reintenta ante `GatewayUnavailableError`; tras el último intento la marca fallida.
  - **Broker caído**: si `delay()` no puede encolar (`OperationalError`/`OSError`) la pendiente pasa a `FAILED` y la API responde 503 con un mensaje claro; nada se cobra.
  - **Sin workers**: `expire_unclaimed_recharges()` marca `FAILED` las pendientes de Kushki que ningún worker tomó en `RECHARGE_QUEUE_TIMEOUT_SECONDS` (120 s). Corre al iniciar una recarga y al listar movimientos, así la app nunca se queda en "en proceso". El worker primero llama a `claim_pending_recharge()` (marca `Transaction.claimed_at` con un `UPDATE` condicional): si la recarga expiró, falló o ya la tomó otro worker, no cobra. Así un worker que vuelve tarde nunca cobra con un token viejo ni dos veces; antes de reintentar libera la reserva (`release_recharge`).

El saldo siempre se acredita en la base de datos con `F()` (`credit_wallet`, que devuelve el saldo resultante). Cada movimiento exitoso guarda ese saldo en `Transaction.balance_after` (recargas al acreditar y consumos en `debit_wallet`), y `GET /api/wallet/<id>/transactions/` lo devuelve para que las apps muestren "saldo después del movimiento"; es `null` en pendientes y fallidos. La migración `wallet 0005` rellenó el histórico recorriendo los movimientos exitosos de cada billetera desde su saldo actual hacia atrás (se detiene, dejando `null`, si el saldo saldría negativo). El webhook de Kushki se protege con `KUSHKI_WEBHOOK_SECRET` (cabecera `X-Webhook-Secret`).

### 5.2 Venta en el POS
`process_sale()` elige un `SaleProcessor` por método de pago (`CASH`, `CREDIT`, `WALLET`), todos con el mismo Template Method: bloquear productos → validar cliente → registrar venta → cobrar → descontar stock. Para agregar un método de pago se crea una subclase y se registra en `PROCESSORS`. El cobro con saldo usa `debit_wallet()`, que bloquea la billetera y registra la `Transaction`.

### 5.3 Combos
El producto tiene un solo precio (`MenuItem.price`): no existen descuentos ni promociones, y `SaleProcessor` cobra ese precio. La única oferta son los combos.

Un `Combo` agrupa productos a un precio fijo (p. ej. sánduche + cola a $3.50). `ComboService` da los combos vigentes; en la venta, `SaleProcessor` recibe además `combo_lines`: bloquea y descuenta el stock de cada producto del combo, revisa sus alérgenos y registra el combo como una sola línea `SaleCombo` (guarda el nombre, así el recibo sobrevive si se borra el combo). Dentro del combo vale el precio del combo. **Stock del combo:** el combo no tiene stock propio, se deriva de sus productos. `ComboService.available_units` es el mínimo de `stock // cantidad` de sus ítems (0 si un producto está inactivo) y se expone como `available_quantity`/`is_available` (y `stock`/`is_active` por ítem) en `combos/` y `combos/active/`; cada cliente decide si oculta o atenúa el combo agotado. Al crear un combo, `ComboService.validate_stock` rechaza productos sin stock para una unidad; al editar solo revisa los productos nuevos o con mayor cantidad. La venta sigue siendo la validación definitiva (bloqueo de filas con `select_for_update`).

**Aviso por correo al crear un combo:** `ComboService.notify_created` (llamado por `ComboListCreateView.perform_create`, solo al crear, no al editar ni borrar) encola `catalog.tasks.send_combo_created_email` al confirmar la transacción, igual que el aviso del menú mensual. Envía un correo por padre activo con hijos en la sede del combo (nombre, productos con sus cantidades, vigencia y **la imagen del combo incrustada** como `Content-ID`, con correo `multipart/related`; sin precios ni ahorro, y si el archivo no se puede leer, sale sin imagen). Comparte con el aviso del menú los helpers de destinatarios, remitente y envío por lotes de `catalog/tasks.py`.

### 5.4 Preorden
`PreOrderCreator` (en `pos/services.py`) valida fecha de entrega (posterior a hoy), menú publicado de la institución, disponibilidad de platillos, control parental y alérgenos, y debita la billetera, todo en una transacción. `deliver_preorder()` la marca entregada solo el día correspondiente.

### 5.5 Identificación en el POS
- **Facial**: se extrae el vector de la foto (la imagen no se guarda), se compara por similitud coseno contra los vectores cifrados de la institución (`BIOMETRIC_MATCH_THRESHOLD`, y un margen de ambigüedad `BIOMETRIC_AMBIGUITY_MARGIN`).
- **QR dinámico**: JWT de corta vida (`QR_TOKEN_TTL_SECONDS`) con un nonce de un solo uso (`consume_qr_nonce`).
- Un mismo rostro no puede registrarse para dos personas de la misma institución (`FaceAlreadyRegistered`, 409).

### 5.6 Recuperación de contraseña
Solicitar código → generar (`secrets`) → enviar por correo (Celery) → confirmar. La respuesta es idéntica para correos que no existen, hay un máximo de intentos por código y un límite de llamadas por correo (`CanRequestPasswordReset`). `BasePasswordResetView` es el Template Method; el cambio de contraseña autenticado reutiliza su estructura. Tanto el cambio como la recuperación ponen en lista negra los refresh tokens del usuario (`revoke_user_tokens`); `change-password/` devuelve tokens nuevos para el dispositivo que hizo el cambio (`rotate_user_tokens`) y limita los intentos por usuario (scope `change_password`, `CHANGE_PASSWORD_THROTTLE_RATE`). Cambiar el correo desde `me/profile/` también exige la contraseña actual (se valida en `ProfileUpdateSerializer`) y esos intentos comparten el mismo límite.

### 5.7 Analítica de ventas (dashboard del administrador)
`SalesAnalytics` (`pos/services.py`) calcula las métricas bajo demanda sobre la base de datos, sin caché: el panel las vuelve a pedir cada pocos segundos. `BaseAnalyticsView` es el Template Method (validar rango → resolver sede → construir la métrica) y solo lo lee el rol `ADMIN`. La sede es la propia, `?institution=<id>` o `?institution=all` (consolidado). Parámetros: `date_from` y `date_to` (inclusivos, por defecto el mes actual; máximo `ANALYTICS_MAX_RANGE_DAYS`) y, en `timeseries`, `granularity` (`day`, `week`, `month`). El detalle de un día/semana/mes se obtiene pidiendo los mismos endpoints con el rango de ese bucket (cada fila de `timeseries` trae su `date_from`/`date_to`).

Reglas de las métricas:
- **Ingreso** = todas las `Sale` (efectivo, saldo, crédito) más las `PreOrder` que no estén `CANCELED`, contadas al crearse. Los canales son `CASH`, `WALLET`, `CREDIT` y `PREORDER`.
- No se suman las `Transaction` de consumo (duplicarían la venta) ni las recargas (dinero que entra al monedero, no consumo).
- La sede sale de `Sale.institution` y de `PreOrder.student.institution`, nunca de `customer.institution`.
- Los días se cortan en hora local (`core.timezone.LOCAL_TIMEZONE`), no en UTC: una venta de las 23:30 queda en su día.
- La comparativa usa el periodo inmediatamente anterior de igual duración; el cambio es `null` si ese periodo vale 0.
- Top de productos: `SaleItem` y `PreOrderItem` unidos por nombre, con ingreso `cantidad × price_at_purchase`; dos rankings (`by_revenue`, `by_units`) de `ANALYTICS_TOP_PRODUCTS_LIMIT` filas.
- Top de combos: aparte de los productos, desde `SaleCombo` (cada combo es una línea a su precio, agrupado por el nombre guardado en la venta), con los mismos dos rankings y los totales del periodo (`totals.units`, `totals.revenue`).
- Reporte de crédito docente (HU-08.2): `teacher_credit_report()` toma las ventas `CREDIT` del periodo que da `credit_period(month)` (hora local, nunca UTC): el mes en curso va del día 1 hasta el momento de generarlo (si hoy es 14, del 1 al 14) y un mes pasado (`month=YYYY-MM`) sale completo; un mes futuro o mal escrito es 400. Una fila por docente y sede con cada venta (productos y combos) y los totales. Incluye docentes dados de baja: el consumo existió. La web lo exporta para Recursos Humanos en Excel o PDF: `POST reports/teacher-credit/exports/` (`month`, `file_type`; se llama `file_type` porque `?format=` lo reserva DRF) crea un `ReportExport` (`PENDING`) y encola `build_teacher_credit_export_task` (202); la web consulta `exports/<id>/` hasta `DONE` y baja el archivo de `exports/<id>/download/` (solo el administrador que lo pidió, `IsReportExportOwner`). Nada se genera dentro de la petición. `TeacherCreditReportBuilder` reúne lo común (periodo, una consulta agregada por docente y las ventas en streaming con `iterator()` + `prefetch_related`) y cada formato es una subclase registrada en `EXPORT_FORMATS` (un formato nuevo es una entrada más): `TeacherCreditExcelBuilder` (`xlsxwriter` con `constant_memory`) escribe las hojas Resumen (lectura), Nómina (tabla plana para importar al ERP: encabezado en la fila 1, sin celdas combinadas ni totales; código, apellidos, nombres, correo, sede, concepto, desde, hasta, N.º consumos, valor a descontar) y Detalle (una fila por consumo con comprobante, productos con precio unitario, ítems y monto); `TeacherCreditPdfBuilder` (`reportlab`, A4 horizontal, con la marca, resumen, detalle por docente con subtotales, firmas y "Página X de Y") importa `reportlab` solo al construir, así solo el worker de Celery carga la librería, y arma una tabla pequeña por docente con celdas de texto plano. Con 21 000 consumos tarda unos 6 s (PDF) y 2 s (Excel) y el PDF usa ~50 MB de pico. No se guarda la cédula: RR. HH. cruza por nombre y correo (único) y el código interno del docente. Un pedido sin terminar del mismo usuario, sede, mes y tipo se reutiliza, los atascados pasan a `FAILED` tras `REPORT_EXPORT_TIMEOUT_SECONDS` y los archivos se borran tras `REPORT_EXPORT_TTL_DAYS`.
- Estado de cuenta a crédito por correo: el administrador lo envía desde el reporte con `POST reports/teacher-credit/statements/?month=YYYY-MM` (`{"resend": false}`) y **solo para meses cerrados** (`closed_credit_month`): el mes en curso o uno futuro da 400, porque su monto aún cambia; el mes cierra a la medianoche de hora local. `send_credit_statements()` toma la misma consulta agregada del reporte (`teacher_credit_summary`) y, por cada docente con correo, crea un `CreditStatement` (único por docente, sede y mes: `QUEUED` → `SENT` o `FAILED`) y encola una tarea `send_credit_statement_email` por docente (solo viaja el id; reintenta ante fallos de entrega y al agotarlos queda `FAILED`). Un doble clic no reenvía (`already_sent`), un `FAILED` se reintenta solo y `resend` fuerza el reenvío; los docentes sin correo (p. ej. dados de baja) se omiten. `deliver_credit_statement()` arma el correo (`users/templates/emails/credit_statement.html`: total en el recuadro amarillo y detalle de cada consumo) con las mismas ventas del reporte, así lo que recibe el docente coincide con lo enviado a RR. HH. El reporte (`GET`) marca por fila `statement_status` y `statement_sent_at`.

## 6. Patrones y decisiones

- **Mixin**: `OwnInstitutionMixin` (+ `InstitutionScopeMixin`, `ViewerInstitutionMixin`) filtra todo queryset por la institución del usuario; `LineSummaryMixin` comparte el resumen de compras en serializers.
- **Template Method**: `PaymentGateway`, `SaleProcessor`, `BasePasswordResetView`, `BaseAllergyView`, `BaseAnalyticsView`.
- **Permission Class**: `RolePermission` como base (`IsAdministrator`, `IsParentUser`, `IsTeacherUser`, …), `SameInstitutionPermission`, `IsWalletOwnerParent`, `CanManageStaffAccount`. La autorización no vive dentro de las vistas.
- **Services**: la lógica de negocio vive en `services.py` de cada app; las vistas solo validan la entrada, llaman al servicio y arman la respuesta.
- **Aislamiento multi-institución**: un usuario solo ve y escribe datos de su institución; el administrador puede ampliar con `?institution=<id>` o `?institution=all` (solo lectura consolidada).
- **Soft delete** de cuentas (`soft_delete_account`): se conserva el historial de ventas y se borran datos personales y biometría.
- **Datos sensibles** (foto, biometría, alergias, contraseña): solo los maneja el dueño de la cuenta; el administrador nunca los toca (`CanRegisterBiometric`). El administrador gestiona al personal operativo por completo y, de los docentes, solo activa/desactiva (y corrige o elimina los pendientes de activación) según `CanManageStaffAccount`.
- **Contraseña temporal del personal**: al crear un docente o personal operativo (`POST /api/users/staff/`), `create_staff_account` genera la contraseña (8 caracteres con letras y números, sin símbolos ni caracteres que se confunden como `I l i L o O 0 1`, para leerla y copiarla fácil) y `notify_staff_credentials` la envía al correo del nuevo usuario por Celery; la API **no la devuelve** (solo `email_sent`, para avisar al admin si el correo no se pudo encolar). El usuario debe cambiarla en su primer inicio de sesión (`must_change_password`).
- **Correos HTML**: viven en `users/templates/emails/`. Los correos nuevos extienden `base.html` (encabezado, barra de acento y pie de marca) y solo definen `{% block content %}`; para destacar un dato que el usuario debe copiar (usuario, contraseña) usan `{% include "emails/_credential_box.html" with label=... value=... %}`; los anteriores (`password_reset`, `recharge_confirmation`, `monthly_menu_published`) y `combo_created` (que imita al del menú) aún llevan el marco copiado.
- **Deuda conocida**: los campos de nombre usan `first_name`, `second_name`, `last_name`, `second_last_name` (heredados de `AbstractUser`); el estándar del proyecto es `first_name`, `middle_name`, `first_last_name`, `second_last_name`. Renombrarlos requiere migración y revisar API y clientes.

## 7. Configuración

Variables en `.env.example`, agrupadas en seguridad, entorno, base de datos, servicios (RabbitMQ, Redis), correo y pagos. Obligatorias sin valor por defecto: `SECRET_KEY`, `DB_*`, `RABBITMQ_URL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`.

## 8. Tareas asíncronas (Celery)

- `wallet.tasks.process_recharge_task`, `send_recharge_confirmation_email`
- `users.tasks.send_password_reset_email`, `send_staff_credentials_email` (contraseña temporal de un docente o personal operativo recién creado)
- `catalog.tasks` (`send_monthly_menu_published_email`: aviso de menú mensual publicado; `send_combo_created_email`: aviso de combo nuevo con su imagen)
- `pos.tasks.build_teacher_credit_export_task` (Excel o PDF del reporte de crédito docente), `send_credit_statement_email` (estado de cuenta mensual de un docente)

Worker local: `run_celery.bat`. Redis y RabbitMQ locales: `docker compose up -d`.

- **Correos con `acks_late=True` y `reject_on_worker_lost=True`**: si el worker muere a mitad del envío el mensaje vuelve a la cola en vez de perderse (un correo repetido no hace daño). El cobro de recargas no los usa: lo protege la reserva `claimed_at`.
- **Aviso de workers caídos**: `python manage.py check_celery_workers` usa `core.health.check_workers()` y, si no hay ningún worker consumiendo la cola, envía un correo **directo por SMTP** (no por Celery) a los superusuarios activos con correo. Se programa fuera de Django (cron o Programador de tareas de Windows, p. ej. cada 5 minutos).

## 8.1 Superadmin (admin de Django)

`/admin/` es el panel técnico del superusuario (`createsuperuser` crea `role=ADMIN`; entra con el **usuario**, no con el correo). No sustituye al panel web: el día a día del negocio pasa por la API.

- **Sitio propio** (`core/admin_site.py`, activado con `delycattessen_backend/apps.py`): dos secciones, *Personas y cuentas* y *Seguridad y sistema*, con nombres en español (`verbose_name` de los modelos). Oculta tablas internas (grupos, resultados de Celery, listas de tokens) y deshabilita la acción masiva "eliminar seleccionados". Wallet, pos y catalog no se registran en el admin.
- **Cuentas** (`users/admin.py`, `admin_forms.py`, `admin_services.py`): el alta solo crea administrador, docente u operativo por `create_staff_account`; el rol se puede cambiar solo entre esos tres (`role_change_error`: nunca padres, estudiantes, superadministradores ni la propia cuenta, ni un docente con crédito) y cierra las sesiones. Acciones: activar, desactivar, dar de baja (`soft_delete_account`, misma política que `CanManageStaffAccount`) y restablecer la contraseña temporal de **cualquier rol** (`reset_account_password`: al correo de la cuenta; la de un estudiante, al de su padre; tope de 25 por acción; tarea `send_password_reset_by_admin_email`).
- **Sedes** (`Institution`): dirección, teléfono, `is_active` y suspensión con motivo. Suspender oculta la sede al registrar estudiantes y personal nuevos (`GET /users/institutions/` solo devuelve las activas a quien no es ADMIN, con `is_active`); no corta sesiones ni ventas. Las estadísticas por sede (`users/admin_stats.py`) son una consulta agrupada por métrica; wallet y pos se resuelven con `apps.get_model` porque están por encima de users.
- **Bitácora** (`AuditLog`, `users/audit.py`): quién hizo qué (API, panel o sistema), con copia de los nombres y sin secretos (`detail` descarta claves que parezcan contraseñas o tokens). El receptor de `celery.signals.task_failure` guarda tareas fallidas sin sus argumentos. `UPDATE_LAST_LOGIN` registra el último acceso de la API. El historial de accesos al panel es `axes.AccessLog`, de solo lectura.
- **Alertas** (`core/alerts.py`): cada app registra sus proveedores en su `alerts.py` (`core` no importa apps); el índice (`admin/delycattessen_index.html`) los muestra con caché de 60 s. **Estado del sistema** (`core/health.py`, vista `admin:system_status`): base de datos, caché, broker, consumidores de la cola (el control remoto de Celery está desactivado), migraciones, `media/` y, bajo demanda, SMTP.
- **Aceptación de términos** (`TermsAcceptance`, migración `users 0015`): evidencia legal de cada aceptación, de solo lectura en el panel. Se crea una fila (usuario, hijo si aplica, documento, versión `TERMS_VERSION`, IP y fecha) al registrar al padre (`PARENT_REGISTRATION`), al registrar un hijo con su foto (`STUDENT_REGISTRATION`) y al cambiar la foto de un hijo (`CHILD_PHOTO`); nunca se edita, aceptar de nuevo agrega otra. Los tres endpoints exigen `accept_terms=true` (`AcceptTermsField`; la foto valida `TermsConsentSerializer`) y la guardan con `record_terms_acceptance`. Al cambiar el texto de los términos se sube `TERMS_VERSION` en el `.env`.
- **Integridad en la base** (migraciones `users 0012`-`0013`, `wallet 0004`, `pos 0012`, `catalog 0013`): `on_delete=PROTECT` en perfiles, billetera, transacciones y preórdenes; `CheckConstraint` de saldo, montos, cantidades, stock/precio y vigencias; correo y nombre de sede únicos sin distinguir mayúsculas; referencia de pasarela única; índices de analítica. Es protección del ORM: no frena un `DELETE` escrito a mano en SQL.

## 9. Tests y CI

- Tests junto al código de cada app: `users/tests/`, `catalog/tests/`, `pos/tests/`, `wallet/tests/` (más `conftest.py` en la raíz).
- `pytest` corre en paralelo (`-n auto`) con un hasher de contraseñas rápido; toda la suite tarda unos 20 segundos.
- CI (`.github/workflows/ci.yml`): `manage.py check` y `pytest` con cobertura sobre PostgreSQL 16.
