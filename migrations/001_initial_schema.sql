## Table `categorias`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `nombre` | `varchar` |  |
| `tipo` | `varchar` |  |
| `color` | `varchar` |  Nullable |
| `activa` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `categorias_plantilla`

Caché de categorías de inventario agrupadas por tipo de ropa. Generadas por IA la primera vez y reutilizadas para ahorrar tokens.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `tipo_ropa` | `varchar` |  Unique |
| `categorias` | `jsonb` |  |
| `generado_por_ia` | `bool` |  Nullable |
| `veces_usado` | `int4` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `inventario`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `producto` | `varchar` |  |
| `categoria` | `varchar` |  Nullable |
| `cantidad_actual` | `int4` |  Nullable |
| `cantidad_minima` | `int4` |  Nullable |
| `precio_costo` | `numeric` |  Nullable |
| `precio_costo_moneda` | `varchar` |  Nullable |
| `precio_venta_pen` | `numeric` |  Nullable |
| `unidad` | `varchar` |  Nullable |
| `activo` | `bool` |  Nullable |
| `ultima_actualizacion` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `metricas`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `fecha` | `date` |  |
| `mensajes_recibidos` | `int4` |  Nullable |
| `mensajes_enviados` | `int4` |  Nullable |
| `ventas_registradas` | `int4` |  Nullable |
| `gastos_registrados` | `int4` |  Nullable |
| `consultas_inventario` | `int4` |  Nullable |
| `reportes_generados` | `int4` |  Nullable |
| `audios_transcritos` | `int4` |  Nullable |
| `ventas_a_chilenos` | `int4` |  Nullable |
| `ventas_a_bolivianos` | `int4` |  Nullable |
| `ventas_en_clp` | `numeric` |  Nullable |
| `ventas_en_bob` | `numeric` |  Nullable |
| `ventas_en_usd` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `negocios`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `whatsapp_numero` | `varchar` |  Unique |
| `nombre_negocio` | `varchar` |  Nullable |
| `nombre_propietario` | `varchar` |  Nullable |
| `rubro` | `varchar` |  Nullable |
| `ciudad` | `varchar` |  Nullable |
| `estado` | `varchar` |  Nullable |
| `onboarding_completo` | `bool` |  Nullable |
| `idioma_preferido` | `varchar` |  Nullable |
| `zona_horaria` | `varchar` |  Nullable |
| `atiende_turistas_chilenos` | `bool` |  Nullable |
| `atiende_clientes_bolivianos` | `bool` |  Nullable |
| `proveedores_zona_franca` | `bool` |  Nullable |
| `proveedores_bolivia` | `bool` |  Nullable |
| `monedas_aceptadas` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `horario_cierre` | `varchar` |  Nullable |

## Table `productos`

Catálogo de productos por negocio. Separado del stock para respetar responsabilidades.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `categoria_id` | `uuid` |  Nullable |
| `nombre` | `varchar` |  |
| `nombre_variantes` | `_varchar` |  Nullable |
| `precio_costo` | `numeric` |  Nullable |
| `precio_costo_moneda` | `varchar` |  Nullable |
| `precio_venta_pen` | `numeric` |  Nullable |
| `unidad` | `varchar` |  Nullable |
| `activo` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `talla` | `text` |  Nullable |

## Table `recordatorios`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `tipo` | `varchar` |  |
| `mensaje` | `text` |  |
| `frecuencia` | `varchar` |  Nullable |
| `hora_envio` | `time` |  Nullable |
| `dia_semana` | `int4` |  Nullable |
| `dia_mes` | `int4` |  Nullable |
| `fecha_especifica` | `date` |  Nullable |
| `activo` | `bool` |  Nullable |
| `ultimo_envio_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `sesiones`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  Unique |
| `estado_conversacion` | `varchar` |  Nullable |
| `datos_temporales` | `jsonb` |  Nullable |
| `ultimo_mensaje_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `stock`

Estado actual de stock por producto. One-to-one con productos. Solo se actualiza, nunca se borra.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `producto_id` | `uuid` |  Unique |
| `cantidad_actual` | `int4` |  |
| `cantidad_minima` | `int4` |  |
| `ultima_actualizacion` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `stock_movimientos`

Historial inmutable de movimientos de stock. Nunca se elimina. Cada fila es una entrada o salida con snapshot de cantidad.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `producto_id` | `uuid` |  |
| `negocio_id` | `uuid` |  |
| `tipo` | `varchar` |  |
| `cantidad` | `int4` |  |
| `cantidad_antes` | `int4` |  |
| `cantidad_despues` | `int4` |  |
| `motivo` | `varchar` |  Nullable |
| `transaccion_id` | `uuid` |  Nullable |
| `notas` | `text` |  Nullable |
| `fecha` | `date` |  |
| `hora` | `time` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `tipos_cambio`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `fecha` | `date` |  |
| `moneda_origen` | `varchar` |  |
| `moneda_destino` | `varchar` |  |
| `tasa` | `numeric` |  |
| `fuente` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `transacciones`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `negocio_id` | `uuid` |  |
| `tipo` | `varchar` |  |
| `monto` | `numeric` |  |
| `moneda` | `varchar` |  Nullable |
| `tasa_cambio_usada` | `numeric` |  Nullable |
| `monto_pen` | `numeric` |  Nullable |
| `categoria_id` | `uuid` |  Nullable |
| `descripcion` | `text` |  Nullable |
| `origen_cliente` | `varchar` |  Nullable |
| `metodo_pago` | `varchar` |  Nullable |
| `fecha` | `date` |  |
| `hora` | `time` |  Nullable |
| `notas` | `text` |  Nullable |
| `origen_registro` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `cantidad` | `int4` |  Nullable |
| `producto_id` | `uuid` |  Nullable |

