"""
app/graph/nodes/negocio.py

Nodos de lógica de negocio del grafo:
  - venta_node
  - gasto_node
  - inventario_node
  - reporte_node
  - editar_node
  - eliminar_node
  - respuesta_directa_node
  - sub_estado_activo_node  ← maneja los turnos intermedios

Cada nodo recibe el QuriState completo y retorna el estado actualizado
con "respuesta" y el nuevo "sub_estado" (vacío si el flujo terminó).
"""

import logging
import datetime
from zoneinfo import ZoneInfo

from app.graph.state import QuriState
from app.services.gemini_service import gemini_service
from app.services.stock_service import stock_service
from app.services.onboarding_service import onboarding_service
from app.database import get_pool

logger = logging.getLogger(__name__)

SIMBOLOS = {"PEN": "S/", "USD": "$", "BOB": "Bs.", "CLP": "CLP"}


# ══════════════════════════════════════════════════════════
#  HELPERS COMUNES
# ══════════════════════════════════════════════════════════

def _ahora_local(negocio: dict) -> datetime.datetime:
    zona_str = negocio.get("zona_horaria") or "America/Lima"
    try:
        tz = ZoneInfo(zona_str)
    except Exception:
        tz = ZoneInfo("America/Lima")
    return datetime.datetime.now(tz)


async def _guardar_transaccion(
    negocio_id: str,
    tipo: str,
    descripcion: str,
    monto: float,
    moneda: str = "PEN",
    fecha: str = None,
    hora: str = None,
    cantidad: int = 1,
    producto_id: str = None,
) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if fecha and hora:
            try:
                fecha_obj = datetime.date.fromisoformat(fecha)
                hora_obj  = datetime.time.fromisoformat(hora)
            except ValueError:
                fecha_obj = datetime.date.today()
                hora_obj  = datetime.time(12, 0)
            row = await conn.fetchrow(
                """
                INSERT INTO transacciones
                    (negocio_id, tipo, descripcion, monto, moneda,
                     fecha, hora, origen_registro, cantidad, producto_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'whatsapp',$8,$9)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto),
                moneda, fecha_obj, hora_obj, cantidad, producto_id,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO transacciones
                    (negocio_id, tipo, descripcion, monto, moneda,
                     fecha, origen_registro, cantidad, producto_id)
                VALUES ($1,$2,$3,$4,$5,CURRENT_DATE,'whatsapp',$6,$7)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto),
                moneda, cantidad, producto_id,
            )
    tx_id = row["id"] if row else None
    logger.info(f"💾 {tipo.upper()}: {descripcion} | {moneda} {monto} | id={tx_id}")
    return tx_id


async def _obtener_ultimas_transacciones(negocio_id: str, limite: int = 5) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id, t.tipo, t.descripcion, t.monto, t.moneda,
                   to_char(t.created_at AT TIME ZONE COALESCE(n.zona_horaria,'America/Lima'), 'DD/MM') AS fecha_corta
            FROM transacciones t
            JOIN negocios n ON n.id = t.negocio_id
            WHERE t.negocio_id = $1::uuid
            ORDER BY t.created_at DESC LIMIT $2
            """,
            negocio_id, limite,
        )
    return [
        {**dict(r), "id": str(r["id"]), "monto": float(r["monto"] or 0)}
        for r in rows
    ]


async def _obtener_reporte(negocio_id: str, periodo: str) -> dict:
    filtros = {
        "hoy":    "fecha = CURRENT_DATE",
        "ayer":   "fecha = CURRENT_DATE - 1",
        "semana": "fecha >= CURRENT_DATE - 7",
        "mes":    "fecha >= CURRENT_DATE - 30",
    }
    where = filtros.get(periodo, filtros["hoy"])
    pool  = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(monto) FILTER (WHERE tipo='venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo='gasto'), 0) AS total_gastos,
                COUNT(*)           FILTER (WHERE tipo='venta')       AS num_ventas
            FROM transacciones
            WHERE negocio_id = $1 AND {where}
            """,
            negocio_id,
        )
    tv = float(row["total_ventas"])
    tg = float(row["total_gastos"])
    return {
        "periodo":           periodo,
        "total_ventas":      tv,
        "total_gastos":      tg,
        "num_transacciones": int(row["num_ventas"]),
        "ganancia_neta":     tv - tg,
    }


def _persist_sub_estado(datos_pendientes: dict, sub_estado: str) -> dict:
    """Agrega sub_estado al dict que se persistirá en datos_temporales."""
    return {**datos_pendientes, "sub_estado": sub_estado}


# ══════════════════════════════════════════════════════════
#  NODO: VENTA
# ══════════════════════════════════════════════════════════

async def venta_node(state: QuriState) -> QuriState:
    datos        = state.get("datos_nlp", {})
    negocio      = state["negocio"]
    negocio_id   = state["negocio_id"]
    ahora        = _ahora_local(negocio)

    if not datos.get("total"):
        return {**state, "respuesta": "¿Cuánto fue el total de la venta? 💰"}

    nombre_producto = datos.get("producto", "producto")
    cantidad        = int(datos.get("cantidad", 1))
    precio_unitario = datos.get("precio_unitario")
    total_venta     = float(datos.get("total", 0))
    moneda          = datos.get("moneda", "PEN")
    simbolo         = SIMBOLOS.get(moneda, "S/")
    f_fecha         = datos.get("fecha") or ahora.strftime("%Y-%m-%d")
    f_hora          = datos.get("hora")  or ahora.strftime("%H:%M:%S")
    nombre_propio   = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"

    datos_pendientes = {
        "nombre_producto_original": nombre_producto,
        "cantidad_stock":           cantidad,
        "precio_unitario_stock":    precio_unitario,
        "venta_monto":              total_venta,
        "venta_moneda":             simbolo,
        "venta_fecha":              f_fecha,
        "venta_hora":               f_hora,
        "venta_nombre_propio":      nombre_propio,
        "venta_moneda_codigo":      moneda,
    }

    resultado_stock = await stock_service.procesar_venta(
        negocio_id      = negocio_id,
        nombre_producto = nombre_producto,
        cantidad        = cantidad,
        precio_unitario = precio_unitario,
    )
    estado_stock = resultado_stock["estado"]

    def _msg_conf(nombre_final: str) -> str:
        return (
            f"✅ Venta registrada, {nombre_propio}\n\n"
            f"📅 {f_fecha} {f_hora}\n"
            f"📝 Producto: {nombre_final}\n"
            f"📦 Cantidad: {cantidad}\n"
            f"💰 Total: {simbolo} {total_venta:.2f}"
        )

    # ── Match exacto: registrar y cerrar ──
    if estado_stock == "exacto":
        producto_id  = resultado_stock["producto_id"]
        prod_nombre  = resultado_stock["producto_nombre"]
        prod_talla   = resultado_stock["producto_talla"]
        nombre_final = prod_nombre + (f" Talla {prod_talla}" if prod_talla else "")

        tx_id = await _guardar_transaccion(
            negocio_id, "venta", nombre_final, total_venta,
            moneda, datos.get("fecha"), datos.get("hora"), cantidad, producto_id,
        )
        descuento = await stock_service.ejecutar_descuento_venta(
            negocio_id=negocio_id, producto_id=producto_id,
            nombre_producto=nombre_producto, cantidad=cantidad, transaccion_id=tx_id,
        )
        respuesta = _msg_conf(nombre_final) + "\n\n" + descuento["mensaje_stock"]
        return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}

    # ── Candidatos múltiples: pedir selección ──
    if estado_stock == "parcial":
        candidatos = resultado_stock["candidatos"]
        lista = "\n".join(
            f"{i+1}. {p['nombre']}" + (f", Talla {p['talla']}" if p.get("talla") else "")
            + f" (stock: {p.get('cantidad_actual','?')})"
            for i, p in enumerate(candidatos)
        )
        datos_pendientes["candidatos_stock"] = candidatos
        datos_pendientes["operacion_stock"]  = "venta"
        respuesta = f"¿Cuál de estos vendiste? 🤔\n\n{lista}\n\nEscribe el número, o dime si no está en la lista."
        return {
            **state,
            "respuesta": respuesta,
            "sub_estado": "ESPERANDO_SELECCION_STOCK",
            "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_SELECCION_STOCK"),
        }

    # ── Sin match: preguntar si agregar ──
    respuesta = (
        f"Este producto no está en tu catálogo 🔍\n\n"
        f"¿Qué quieres hacer?\n"
        f"1️⃣ *Agregar* al catálogo\n"
        f"2️⃣ *Seguir* sin añadir al stock\n\n"
        f"_(La venta se registrará cuando elijas)_"
    )
    return {
        **state,
        "respuesta": respuesta,
        "sub_estado": "ESPERANDO_DECISION_PRODUCTO_NUEVO",
        "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_DECISION_PRODUCTO_NUEVO"),
    }


# ══════════════════════════════════════════════════════════
#  NODO: GASTO
# ══════════════════════════════════════════════════════════

async def gasto_node(state: QuriState) -> QuriState:
    datos      = state.get("datos_nlp", {})
    negocio    = state["negocio"]
    negocio_id = state["negocio_id"]
    ahora      = _ahora_local(negocio)

    if not datos.get("monto"):
        return {**state, "respuesta": "¿Cuánto fue el gasto? 💸"}

    moneda        = datos.get("moneda", "PEN")
    simbolo       = SIMBOLOS.get(moneda, "S/")
    f_fecha       = datos.get("fecha") or ahora.strftime("%Y-%m-%d")
    f_hora        = datos.get("hora")  or ahora.strftime("%H:%M:%S")
    nombre_propio = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"

    await _guardar_transaccion(
        negocio_id, "gasto",
        datos.get("concepto", "gasto"),
        datos.get("monto", 0), moneda,
        datos.get("fecha"), datos.get("hora"),
    )

    respuesta = (
        f"✅ Gasto registrado, {nombre_propio}\n\n"
        f"📅 {f_fecha} {f_hora}\n"
        f"🏷️ {str(datos.get('categoria', 'Otros')).capitalize()}\n"
        f"📝 {datos.get('concepto', '')}\n"
        f"💰 {simbolo} {datos.get('monto', 0):.2f}"
    )
    return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}


# ══════════════════════════════════════════════════════════
#  NODO: INVENTARIO
# ══════════════════════════════════════════════════════════

async def inventario_node(state: QuriState) -> QuriState:
    datos        = state.get("datos_nlp", {})
    negocio_id   = state["negocio_id"]

    if not datos.get("producto"):
        return {**state, "respuesta": "¿Qué producto quieres registrar en inventario? 📦"}

    nombre_producto = datos.get("producto", "")
    cantidad        = int(datos.get("cantidad", 0))
    tipo_inv        = datos.get("tipo", "entrada")
    precio_costo    = datos.get("precio_costo")
    precio_venta    = datos.get("precio_venta")

    resultado = await stock_service.procesar_inventario(
        negocio_id=negocio_id,
        nombre_producto=nombre_producto,
        cantidad=cantidad,
        tipo=tipo_inv,
        precio_costo=precio_costo,
        precio_venta=precio_venta,
    )
    estado_inv = resultado["estado"]

    if estado_inv in ("actualizado", "creado"):
        return {**state, "respuesta": resultado["mensaje"], "sub_estado": "", "datos_pendientes": {}}

    if estado_inv == "pendiente_seleccion":
        datos_pendientes = {
            "candidatos_stock":         resultado["candidatos"],
            "operacion_stock":          f"inventario_{tipo_inv}",
            "cantidad_stock":           cantidad,
            "nombre_producto_original": nombre_producto,
        }
        return {
            **state,
            "respuesta": resultado["mensaje"],
            "sub_estado": "ESPERANDO_SELECCION_STOCK",
            "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_SELECCION_STOCK"),
        }

    # sin_match
    if precio_venta is not None:
        separado      = await gemini_service.extraer_nombre_y_talla(nombre_producto)
        nombre_limpio = separado.get("nombre") or nombre_producto.strip().title()
        talla         = separado.get("talla")
        formulario    = {
            "nombre": nombre_limpio, "talla": talla,
            "stock": cantidad, "precio_venta": precio_venta, "precio_compra": precio_costo,
        }
        talla_linea = f"📐 *Talla:* {talla}\n" if talla else "📐 *Talla:* (no especificada)\n"
        respuesta = (
            f"📝 *Nombre:* {nombre_limpio}\n{talla_linea}"
            f"📦 *Stock:* {cantidad}\n"
            f"💰 *Precio de Venta:* {'S/ '+str(precio_venta) if precio_venta else '(no especificado)'}\n\n"
            f"¿Queda así? Escribe *guardar*, sigue editando, o *cancelar*."
        )
        datos_pendientes = {
            "operacion_stock": f"inventario_{tipo_inv}",
            "cantidad_stock": cantidad,
            "nombre_producto_original": nombre_producto,
            "formulario_producto": formulario,
        }
        return {
            **state,
            "respuesta": respuesta,
            "sub_estado": "ESPERANDO_DATOS_PRODUCTO_NUEVO",
            "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_DATOS_PRODUCTO_NUEVO"),
        }

    respuesta = (
        f"Este producto no está en tu catálogo 🔍\n\n"
        f"¿Quieres agregar *{nombre_producto}* como producto nuevo?\n"
        f"Responde *Sí* o *Cancelar*."
    )
    datos_pendientes = {
        "operacion_stock": f"inventario_{tipo_inv}",
        "cantidad_stock": cantidad,
        "nombre_producto_original": nombre_producto,
    }
    return {
        **state,
        "respuesta": respuesta,
        "sub_estado": "ESPERANDO_DECISION_PRODUCTO_NUEVO",
        "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_DECISION_PRODUCTO_NUEVO"),
    }


# ══════════════════════════════════════════════════════════
#  NODO: REPORTE
# ══════════════════════════════════════════════════════════

async def reporte_node(state: QuriState) -> QuriState:
    datos      = state.get("datos_nlp", {})
    negocio_id = state["negocio_id"]
    periodo    = datos.get("periodo", "hoy")

    datos_reporte = await _obtener_reporte(negocio_id, periodo)
    respuesta     = await gemini_service.generar_resumen_reporte(datos_reporte)
    return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}


# ══════════════════════════════════════════════════════════
#  NODO: ELIMINAR TRANSACCIÓN
# ══════════════════════════════════════════════════════════

async def eliminar_node(state: QuriState) -> QuriState:
    negocio_id = state["negocio_id"]
    ultimas    = await _obtener_ultimas_transacciones(negocio_id, 5)

    if not ultimas:
        return {**state, "respuesta": "No tienes transacciones recientes para eliminar.", "sub_estado": ""}

    lineas = ["¿Cuál deseas eliminar?"]
    for i, t in enumerate(ultimas, 1):
        s = SIMBOLOS.get(t["moneda"], t["moneda"])
        lineas.append(f"{i}. {t.get('fecha_corta','')} | {t['descripcion']} | {s} {t['monto']:.2f}")
    lineas.append("\nEscribe el número o 'cancelar'.")

    datos_pendientes = {"ultimas_transacciones": ultimas}
    return {
        **state,
        "respuesta": "\n".join(lineas),
        "sub_estado": "ESPERANDO_SELECCION_ELIMINAR",
        "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_SELECCION_ELIMINAR"),
    }


# ══════════════════════════════════════════════════════════
#  NODO: EDITAR TRANSACCIÓN
# ══════════════════════════════════════════════════════════

async def editar_node(state: QuriState) -> QuriState:
    negocio_id = state["negocio_id"]
    ultimas    = await _obtener_ultimas_transacciones(negocio_id, 5)

    if not ultimas:
        return {**state, "respuesta": "No tienes transacciones recientes para editar.", "sub_estado": ""}

    lineas = ["¿Cuál deseas editar?"]
    for i, t in enumerate(ultimas, 1):
        s = SIMBOLOS.get(t["moneda"], t["moneda"])
        lineas.append(f"{i}. {t.get('fecha_corta','')} | {t['descripcion']} | {s} {t['monto']:.2f}")
    lineas.append("\nEscribe el número o 'cancelar'.")

    datos_pendientes = {"ultimas_transacciones": ultimas}
    return {
        **state,
        "respuesta": "\n".join(lineas),
        "sub_estado": "ESPERANDO_SELECCION_EDITAR",
        "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_SELECCION_EDITAR"),
    }


# ══════════════════════════════════════════════════════════
#  NODO: RESPUESTA DIRECTA (INCOMPLETO / SALUDO / DESCONOCIDO)
# ══════════════════════════════════════════════════════════

async def respuesta_directa_node(state: QuriState) -> QuriState:
    """
    Para intents que ya tienen su respuesta lista desde el router
    (INCOMPLETO, SALUDO, AYUDA, DESCONOCIDO).
    Si es INCOMPLETO guarda en historial para que el siguiente mensaje
    tenga contexto.
    """
    respuesta = state.get("respuesta") or "¿En qué te ayudo? 😊"
    intent    = state.get("intent", "DESCONOCIDO")

    # INCOMPLETO: el historial se guardará en el nodo final (persistencia)
    return {
        **state,
        "respuesta": respuesta,
        "sub_estado": "INCOMPLETO" if intent == "INCOMPLETO" else "",
    }


# ══════════════════════════════════════════════════════════
#  NODO: SUB_ESTADO ACTIVO
#  Maneja los turnos intermedios (selección de producto, edición, etc.)
#  Es básicamente el flujo B1-B4 del webhook.py original, pero en un nodo
# ══════════════════════════════════════════════════════════

async def sub_estado_activo_node(state: QuriState) -> QuriState:
    """
    Despacha al handler correcto según el sub_estado guardado.
    """
    sub_estado       = state.get("sub_estado", "")
    datos_pendientes = state.get("datos_pendientes", {})
    mensaje          = state["mensaje"]
    negocio_id       = state["negocio_id"]
    msg_lower        = mensaje.strip().lower()

    # ── Cancelar universal ──
    if msg_lower == "cancelar":
        return {
            **state,
            "respuesta": "Operación cancelada. ¿En qué más te ayudo? 😊",
            "sub_estado": "",
            "datos_pendientes": {},
        }

    # ── Despachar según sub_estado ──
    if sub_estado == "ESPERANDO_SELECCION_STOCK":
        return await _handle_seleccion_stock(state, datos_pendientes, negocio_id, mensaje, msg_lower)

    if sub_estado == "ESPERANDO_SELECCION_ELIMINAR":
        return await _handle_seleccion_eliminar(state, datos_pendientes, negocio_id, mensaje)

    if sub_estado == "ESPERANDO_SELECCION_EDITAR":
        return await _handle_seleccion_editar(state, datos_pendientes, negocio_id, mensaje)

    if sub_estado == "ESPERANDO_EDICION_TRANSACCION":
        return await _handle_edicion_transaccion(state, datos_pendientes, negocio_id, mensaje)

    if sub_estado == "ESPERANDO_DECISION_PRODUCTO_NUEVO":
        return await _handle_decision_producto_nuevo(state, datos_pendientes, negocio_id, mensaje)

    if sub_estado == "ESPERANDO_DATOS_PRODUCTO_NUEVO":
        return await _handle_datos_producto_nuevo(state, datos_pendientes, negocio_id, mensaje)

    # Sub-estado desconocido
    return {**state, "respuesta": "¿En qué te ayudo? 😊", "sub_estado": "", "datos_pendientes": {}}


# ──────────────────────────────────────────────────────────
#  Handlers de sub_estado (extraídos del webhook.py original)
# ──────────────────────────────────────────────────────────

async def _handle_seleccion_stock(state, datos, negocio_id, mensaje, msg_lower):
    candidatos  = datos.get("candidatos_stock", [])
    operacion   = datos.get("operacion_stock", "venta")
    cantidad    = datos.get("cantidad_stock", 1)
    nombre_orig = datos.get("nombre_producto_original", "")

    es_nuevo = any(w in msg_lower for w in [
        "0", "ninguno", "ninguna", "no está", "no esta", "no hay",
        "agrega", "nuevo", "no pertenece", "ningun", "no aparece"
    ])

    if es_nuevo and operacion == "venta":
        respuesta = (
            f"Entendido 🔍\n\n¿Qué quieres hacer?\n"
            f"1️⃣ *Agregar* al catálogo\n"
            f"2️⃣ *Seguir* sin añadir al stock\n\n"
            f"_(La venta se registrará cuando elijas)_"
        )
        return {
            **state, "respuesta": respuesta,
            "sub_estado": "ESPERANDO_DECISION_PRODUCTO_NUEVO",
            "datos_pendientes": {**datos, "sub_estado": "ESPERANDO_DECISION_PRODUCTO_NUEVO"},
        }

    try:
        seleccion = int(mensaje.strip())
        if 1 <= seleccion <= len(candidatos):
            producto_id_sel = candidatos[seleccion - 1]["id"]

            if operacion == "venta":
                prod         = await stock_service.get_producto(producto_id_sel)
                prod_nombre  = prod["nombre"] if prod else nombre_orig
                prod_talla   = prod.get("talla") if prod else None
                nombre_final = prod_nombre + (f" Talla {prod_talla}" if prod_talla else "")

                tx_id = await _guardar_transaccion(
                    negocio_id, "venta", nombre_final,
                    datos.get("venta_monto", 0),
                    datos.get("venta_moneda_codigo", "PEN"),
                    datos.get("venta_fecha"), datos.get("venta_hora"),
                    cantidad, producto_id_sel,
                )
                descuento = await stock_service.ejecutar_descuento_venta(
                    negocio_id=negocio_id, producto_id=producto_id_sel,
                    nombre_producto=nombre_orig, cantidad=cantidad, transaccion_id=tx_id,
                )
                simbolo       = datos.get("venta_moneda", "S/")
                nombre_propio = datos.get("venta_nombre_propio", "Comerciante")
                respuesta = (
                    f"✅ Venta registrada, {nombre_propio}\n\n"
                    f"📅 {datos.get('venta_fecha','')} {datos.get('venta_hora','')}\n"
                    f"📝 Producto: {nombre_final}\n📦 Cantidad: {cantidad}\n"
                    f"💰 Total: {simbolo} {datos.get('venta_monto', 0):.2f}\n\n"
                    + descuento["mensaje_stock"]
                )
                return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}
            else:
                resultado = await stock_service.confirmar_seleccion_parcial(
                    negocio_id=negocio_id, producto_id=producto_id_sel,
                    nombre_original=nombre_orig, cantidad=cantidad,
                    transaccion_id=None, operacion=operacion,
                )
                return {**state, "respuesta": resultado["mensaje"], "sub_estado": "", "datos_pendientes": {}}

        return {
            **state,
            "respuesta": f"Escribe un número entre 1 y {len(candidatos)}, *0* si no está, o *cancelar*.",
            "sub_estado": state["sub_estado"],
            "datos_pendientes": datos,
        }
    except ValueError:
        return {
            **state,
            "respuesta": "Escribe el número del producto, *0* si no está aquí, o *cancelar*.",
            "sub_estado": state["sub_estado"],
            "datos_pendientes": datos,
        }


async def _handle_seleccion_eliminar(state, datos, negocio_id, mensaje):
    ultimas = datos.get("ultimas_transacciones", [])
    try:
        seleccion = int(mensaje.strip())
        if 1 <= seleccion <= len(ultimas):
            tx = ultimas[seleccion - 1]
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM transacciones WHERE id = $1::uuid", tx["id"])
            simbolo  = SIMBOLOS.get(tx["moneda"], tx["moneda"])
            respuesta = f"✅ Eliminado: {tx['descripcion']} ({simbolo} {tx['monto']:.2f})"
            return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}
        return {
            **state,
            "respuesta": f"Número entre 1 y {len(ultimas)}, o 'cancelar'.",
            "sub_estado": state["sub_estado"], "datos_pendientes": datos,
        }
    except ValueError:
        return {
            **state,
            "respuesta": "Escribe un número o 'cancelar'.",
            "sub_estado": state["sub_estado"], "datos_pendientes": datos,
        }


async def _handle_seleccion_editar(state, datos, negocio_id, mensaje):
    ultimas = datos.get("ultimas_transacciones", [])
    try:
        seleccion = int(mensaje.strip())
        if 1 <= seleccion <= len(ultimas):
            tx = ultimas[seleccion - 1]
            simbolo  = SIMBOLOS.get(tx["moneda"], tx["moneda"])
            respuesta = (
                f"Elegiste: {tx['descripcion']} ({simbolo} {tx['monto']:.2f}).\n"
                f"¿Qué deseas cambiar? (ej. 'cambia el monto a 50' o 'cancelar')"
            )
            return {
                **state,
                "respuesta": respuesta,
                "sub_estado": "ESPERANDO_EDICION_TRANSACCION",
                "datos_pendientes": {**datos, "transaccion_a_editar": tx, "sub_estado": "ESPERANDO_EDICION_TRANSACCION"},
            }
        return {
            **state,
            "respuesta": f"Número entre 1 y {len(ultimas)}, o 'cancelar'.",
            "sub_estado": state["sub_estado"], "datos_pendientes": datos,
        }
    except ValueError:
        return {
            **state,
            "respuesta": "Escribe un número o 'cancelar'.",
            "sub_estado": state["sub_estado"], "datos_pendientes": datos,
        }


async def _handle_edicion_transaccion(state, datos, negocio_id, mensaje):
    tx = datos.get("transaccion_a_editar")
    if not tx:
        return {**state, "respuesta": "Ocurrió un error. Operación cancelada.", "sub_estado": "", "datos_pendientes": {}}

    cambios = await gemini_service.interpretar_edicion(tx, mensaje)
    if cambios:
        sets   = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(cambios))
        pool   = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE transacciones SET {sets} WHERE id = $1::uuid",
                tx["id"], *list(cambios.values()),
            )
        cambios_str = ", ".join(f"{k}: {v}" for k, v in cambios.items())
        return {
            **state,
            "respuesta": f"✅ Actualizado.\nCambios: {cambios_str}",
            "sub_estado": "", "datos_pendientes": {},
        }
    return {
        **state,
        "respuesta": "No entendí qué cambiar 🤔 Intenta de otra forma o escribe 'cancelar'.",
        "sub_estado": state["sub_estado"], "datos_pendientes": datos,
    }


async def _handle_decision_producto_nuevo(state, datos, negocio_id, mensaje):
    nombre_prod     = datos.get("nombre_producto_original", "")
    cantidad        = datos.get("cantidad_stock", 1)
    precio_unitario = datos.get("precio_unitario_stock")

    decision = await gemini_service.interpretar_decision_producto_nuevo(mensaje)
    accion   = decision["accion"]

    if accion == "AGREGAR":
        separado      = await gemini_service.extraer_nombre_y_talla(nombre_prod)
        nombre_limpio = separado.get("nombre") or nombre_prod.strip().title()
        talla         = separado.get("talla")
        formulario    = {
            "nombre": nombre_limpio, "talla": talla,
            "stock": None, "precio_venta": precio_unitario, "precio_compra": None,
        }
        talla_linea   = f"📐 *Talla:* {talla}\n" if talla else "📐 *Talla:* (no especificada)\n"
        precio_fmt    = f"S/ {precio_unitario:.2f}" if precio_unitario else "(no especificado)"
        respuesta = (
            f"Cuéntame más 📦\n\n📝 *Nombre:* {nombre_limpio}\n{talla_linea}"
            f"📦 *Stock:* (¿cuántas unidades tienes?)\n💰 *Precio de Venta:* {precio_fmt}\n"
            f"💵 *Precio de Compra:* (opcional)\n\n"
            f"Edita lo que quieras o escribe *guardar* para confirmar."
        )
        return {
            **state,
            "respuesta": respuesta,
            "sub_estado": "ESPERANDO_DATOS_PRODUCTO_NUEVO",
            "datos_pendientes": {**datos, "formulario_producto": formulario, "sub_estado": "ESPERANDO_DATOS_PRODUCTO_NUEVO"},
        }

    # CONTINUAR o CANCELAR → registrar venta sin producto_id
    simbolo       = datos.get("venta_moneda", "S/")
    nombre_propio = datos.get("venta_nombre_propio", "Comerciante")
    tx_id = await _guardar_transaccion(
        negocio_id, "venta", nombre_prod,
        datos.get("venta_monto", 0), datos.get("venta_moneda_codigo", "PEN"),
        datos.get("venta_fecha"), datos.get("venta_hora"), cantidad,
    )
    respuesta = (
        f"✅ Venta registrada, {nombre_propio}\n\n"
        f"📅 {datos.get('venta_fecha','')} {datos.get('venta_hora','')}\n"
        f"📝 Producto: {nombre_prod}\n📦 Cantidad: {cantidad}\n"
        f"💰 Total: {simbolo} {datos.get('venta_monto', 0):.2f}\n\n_(Sin descuento de stock)_"
    )
    return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}


async def _handle_datos_producto_nuevo(state, datos, negocio_id, mensaje):
    formulario  = datos.get("formulario_producto", {})
    nombre_prod = datos.get("nombre_producto_original", "")
    cantidad    = datos.get("cantidad_stock", 1)

    interpretacion = await gemini_service.interpretar_formulario_producto_venta(mensaje, formulario)
    accion  = interpretacion["accion"]
    cambios = interpretacion.get("cambios", {})

    if accion == "EDITAR":
        for campo, valor in cambios.items():
            if valor is not None:
                formulario[campo] = valor
        pv = formulario.get("precio_venta")
        pc = formulario.get("precio_compra")
        st = formulario.get("stock")
        talla_linea = f"📐 *Talla:* {formulario.get('talla')}\n" if formulario.get("talla") else "📐 *Talla:* (no especificada)\n"
        respuesta = (
            f"📝 *Nombre:* {formulario.get('nombre','?')}\n{talla_linea}"
            f"📦 *Stock:* {st if st is not None else '(no especificado)'}\n"
            f"💰 *Precio de Venta:* {f'S/ {pv:.2f}' if pv else '(no especificado)'}\n"
            f"💵 *Precio de Compra:* {f'S/ {pc:.2f}' if pc else '(opcional)'}\n\n"
            f"¿Queda así? Escribe *guardar*, sigue editando, o *cancelar*."
        )
        return {
            **state, "respuesta": respuesta,
            "sub_estado": "ESPERANDO_DATOS_PRODUCTO_NUEVO",
            "datos_pendientes": {**datos, "formulario_producto": formulario, "sub_estado": "ESPERANDO_DATOS_PRODUCTO_NUEVO"},
        }

    if accion == "GUARDAR":
        if not formulario.get("nombre"):
            return {**state, "respuesta": "¿Cómo se llama el producto?", "sub_estado": state["sub_estado"], "datos_pendientes": datos}
        if formulario.get("stock") is None:
            return {**state, "respuesta": "¿Cuántas unidades tienes en stock?", "sub_estado": state["sub_estado"], "datos_pendientes": datos}

        producto_id_nuevo = await stock_service.crear_producto(
            negocio_id=negocio_id,
            nombre=formulario["nombre"],
            talla=formulario.get("talla"),
            precio_venta=formulario.get("precio_venta"),
            precio_costo=formulario.get("precio_compra"),
            cantidad_inicial=formulario["stock"],
        )
        nombre_final  = formulario["nombre"] + (f" Talla {formulario['talla']}" if formulario.get("talla") else "")
        simbolo       = datos.get("venta_moneda", "S/")
        nombre_propio = datos.get("venta_nombre_propio", "Comerciante")

        tx_id = await _guardar_transaccion(
            negocio_id, "venta", nombre_final,
            datos.get("venta_monto", 0), datos.get("venta_moneda_codigo", "PEN"),
            datos.get("venta_fecha"), datos.get("venta_hora"),
            cantidad, producto_id_nuevo,
        )
        descuento = await stock_service.ejecutar_descuento_venta(
            negocio_id=negocio_id, producto_id=producto_id_nuevo,
            nombre_producto=nombre_prod, cantidad=cantidad, transaccion_id=tx_id,
        )
        respuesta = (
            f"✅ Venta registrada, {nombre_propio}\n\n"
            f"📝 Producto: {nombre_final}\n📦 Cantidad: {cantidad}\n"
            f"💰 Total: {simbolo} {datos.get('venta_monto', 0):.2f}\n\n"
            f"✅ \"{nombre_final}\" agregado a tu catálogo 📦\n"
            + descuento["mensaje_stock"]
        )
        return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}

    # CANCELAR
    simbolo       = datos.get("venta_moneda", "S/")
    nombre_propio = datos.get("venta_nombre_propio", "Comerciante")
    await _guardar_transaccion(
        negocio_id, "venta", nombre_prod,
        datos.get("venta_monto", 0), datos.get("venta_moneda_codigo", "PEN"),
        datos.get("venta_fecha"), datos.get("venta_hora"), cantidad,
    )
    respuesta = (
        f"✅ Venta registrada, {nombre_propio} _(sin stock)_\n\n"
        f"📝 {nombre_prod} | {simbolo} {datos.get('venta_monto', 0):.2f}"
    )
    return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}