# Arquitectura del backend — D'Elycattessen

API REST del sistema de ventas y control de servicios alimentarios de D'Elycattessen. Atiende a una app web (React) y una app móvil (Flutter), que viven en repositorios aparte.

## 1. Qué hace el sistema

- **Padres** registran a sus hijos (estudiantes), recargan una billetera, preordenan el menú del día, configuran controles parentales y alergias.
- **Estudiantes y docentes** se identifican en el punto de venta (POS) con **reconocimiento facial** o un **QR dinámico**.
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
| `catalog` | Alérgenos, ingredientes, productos, platillos, menús mensuales y diarios, promociones | `models.py`, `services.py`, `views.py` |
| `wallet` | Billetera, transacciones, recargas con pasarelas | `services.py`, `tasks.py`, `views.py` |
| `pos` | Preórdenes, ventas, identificación facial/QR, historiales | `services.py`, `mixins.py`, `views.py` |

### Modelo de datos (resumen)

- **users**: `Institution` (única fuente de verdad de sede) · `CustomUser` (rol, institución, nombre en 4 partes) · `ParentProfile` · `StudentProfile` (padre, institución, foto) · `ParentalControl` · `UserBiometric` (vector cifrado: `encrypted_embedding`, `nonce`, `tag`).
- **catalog**: `Allergen` · `Ingredient` (alérgenos) · `MenuItem` (producto del POS, con stock) · `MenuDish` (platillo de preorden) · `MonthlyMenu` (borrador/publicado) → `DailyMenu` · `Promotion` (descuento % sobre productos, con fechas de vigencia).
- **wallet**: `Wallet` (1 a 1 con estudiante, saldo) · `Transaction` (recarga o consumo, pasarela, estado, enlace a `PreOrder` o `Sale`).
- **pos**: `PreOrder` → `PreOrderItem` · `Sale` (efectivo, crédito o saldo) → `SaleItem`.

## 4. Endpoints

| Prefijo | Contenido |
|---|---|
| `/api/users/` | Registro de padre y estudiante, login, `me`, hijos, alergias, control parental, PIN de pago, QR (`qr/token/`), biometría (`biometrics/register/`), reset de contraseña, gestión de personal (`staff/`) |
| `/api/catalog/` | Menú, productos, ingredientes, alérgenos, platillos, menús mensuales (publicar/despublicar), menú del estudiante y del POS, promociones (`promotions/`, vigentes para la app en `promotions/active/`) |
| `/api/wallet/` | `recharge/`, `recharge/gateway/`, transacciones, callbacks de Payphone, webhook de Kushki |
| `/api/pos/` | Preórdenes, ventas, historial, crédito docente, `identify/face/`, `identify/qr/` |
| `/api/token/` | Login JWT y refresh |

## 5. Flujos clave

### 5.1 Recarga de saldo
`RechargeService.start()` (en `wallet/services.py`) elige la pasarela con `GatewayRouter` según el monto y `WALLET_RECHARGE_GATEWAY_THRESHOLD`:

- **Monto menor al umbral → Payphone**: `PayphonePreparer.prepare()` crea la transacción pendiente y devuelve una `payment_url` para un WebView. Payphone llama al callback y `confirm()` verifica con Payphone y acredita el saldo (idempotente, con bloqueo de fila).
- **Monto mayor o igual → Kushki**: el frontend tokeniza la tarjeta; la tarea Celery `process_recharge_task` llama a `KushkiGateway.process_recharge()` (Template Method: validar → crear pendiente → cobrar → acreditar → correo). Solo reintenta ante `GatewayUnavailableError`.

El saldo siempre se acredita en la base de datos con `F()` (`credit_wallet`). El webhook de Kushki se protege con `KUSHKI_WEBHOOK_SECRET` (cabecera `X-Webhook-Secret`).

### 5.2 Venta en el POS
`process_sale()` elige un `SaleProcessor` por método de pago (`CASH`, `CREDIT`, `WALLET`), todos con el mismo Template Method: bloquear productos → validar cliente → registrar venta → cobrar → descontar stock. Para agregar un método de pago se crea una subclase y se registra en `PROCESSORS`. El cobro con saldo usa `debit_wallet()`, que bloquea la billetera y registra la `Transaction`.

### 5.3 Promociones
`PromotionService` (en `catalog/services.py`) decide el descuento vigente de cada producto según la fecha local (`core.timezone.local_today`). El precio del producto nunca se modifica: el catálogo devuelve `discount_percentage` y `final_price`, y `SaleProcessor` cobra ese precio, así que al vencer la promoción se vuelve solo al original. Un producto no puede estar en dos promociones con fechas que se cruzan (`validate_products_available`). Las promociones aplican solo a productos (`MenuItem`), nunca a platillos del menú mensual.

### 5.4 Preorden
`PreOrderCreator` (en `pos/services.py`) valida fecha de entrega (posterior a hoy), menú publicado de la institución, disponibilidad de platillos, control parental y alérgenos, y debita la billetera, todo en una transacción. `deliver_preorder()` la marca entregada solo el día correspondiente.

### 5.5 Identificación en el POS
- **Facial**: se extrae el vector de la foto (la imagen no se guarda), se compara por similitud coseno contra los vectores cifrados de la institución (`BIOMETRIC_MATCH_THRESHOLD`, y un margen de ambigüedad `BIOMETRIC_AMBIGUITY_MARGIN`).
- **QR dinámico**: JWT de corta vida (`QR_TOKEN_TTL_SECONDS`) con un nonce de un solo uso (`consume_qr_nonce`).
- Un mismo rostro no puede registrarse para dos personas de la misma institución (`FaceAlreadyRegistered`, 409).

### 5.6 Recuperación de contraseña
Solicitar código → generar (`secrets`) → enviar por correo (Celery) → confirmar. La respuesta es idéntica para correos que no existen, hay un máximo de intentos por código y un límite de llamadas por correo (`CanRequestPasswordReset`). `BasePasswordResetView` es el Template Method; el cambio de contraseña autenticado reutiliza su estructura.

## 6. Patrones y decisiones

- **Mixin**: `OwnInstitutionMixin` (+ `InstitutionScopeMixin`, `ViewerInstitutionMixin`) filtra todo queryset por la institución del usuario; `LineSummaryMixin` comparte el resumen de compras en serializers.
- **Template Method**: `PaymentGateway`, `SaleProcessor`, `BasePasswordResetView`, `BaseAllergyView`.
- **Permission Class**: `RolePermission` como base (`IsAdministrator`, `IsParentUser`, `IsTeacherUser`, …), `SameInstitutionPermission`, `IsWalletOwnerParent`, `CanManageStaffAccount`. La autorización no vive dentro de las vistas.
- **Services**: la lógica de negocio vive en `services.py` de cada app; las vistas solo validan la entrada, llaman al servicio y arman la respuesta.
- **Aislamiento multi-institución**: un usuario solo ve y escribe datos de su institución; el administrador puede ampliar con `?institution=<id>` o `?institution=all` (solo lectura consolidada).
- **Soft delete** de cuentas (`soft_delete_account`): se conserva el historial de ventas y se borran datos personales y biometría.
- **Deuda conocida**: los campos de nombre usan `first_name`, `second_name`, `last_name`, `second_last_name` (heredados de `AbstractUser`); el estándar del proyecto es `first_name`, `middle_name`, `first_last_name`, `second_last_name`. Renombrarlos requiere migración y revisar API y clientes.

## 7. Configuración

Variables en `.env.example`, agrupadas en seguridad, entorno, base de datos, servicios (RabbitMQ, Redis), correo y pagos. Obligatorias sin valor por defecto: `SECRET_KEY`, `DB_*`, `RABBITMQ_URL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`.

## 8. Tareas asíncronas (Celery)

- `wallet.tasks.process_recharge_task`, `send_recharge_confirmation_email`
- `users.tasks.send_password_reset_email`
- `catalog.tasks` (aviso de menú mensual publicado)

Worker local: `run_celery.bat`. Redis y RabbitMQ locales: `docker compose up -d`.

## 9. Tests y CI

- Tests junto al código de cada app: `users/tests/`, `catalog/tests/`, `pos/tests/`, `wallet/tests/` (más `conftest.py` en la raíz).
- `pytest` corre en paralelo (`-n auto`) con un hasher de contraseñas rápido; toda la suite tarda unos 20 segundos.
- CI (`.github/workflows/ci.yml`): `manage.py check` y `pytest` con cobertura sobre PostgreSQL 16.
