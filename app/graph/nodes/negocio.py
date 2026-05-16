"""
app/graph/nodes/negocio.py  — parche para múltiples items

Solo se muestran los nodos que cambian: venta_node y gasto_node.
El resto del archivo (inventario_node, reporte_node, etc.) queda igual.

CAMBIOS:
- venta_node: itera sobre state["items"] en lugar de state["datos_nlp"]
- gasto_node: itera sobre state["items"] en lugar de state["datos_nlp"]
- Respuesta consolidada al final mostrando todos los items registrados
"""

import logging
import datetime
from zoneinfo import ZoneInfo

from app.graph.state import QuriState
from app.services.gemini_service import gemini_service
from app.services.stock_service import stock_service
from app.database import get_pool

logger = logging.getLogger(__name__)

SIMBOLOS = {"PEN": "S/", "USD": "$", "BOB": "Bs.", "CLP": "CLP"}


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


# ══════════════════════════════════════════════════════════
#  NODO: VENTA (múltiples items)
# ══════════════════════════════════════════════════════════

async def venta_node(state: QuriState) -> QuriState:
    """
    Procesa 1 a 5 productos vendidos en un mismo mensaje.
    Para cada item:
      - Busca match en stock
      - Si match exacto → registra transacción + descuenta stock
      - Si candidatos múltiples → guarda en sub_estado para resolver después
      - Si sin match → pregunta si agregar al catálogo

    Si hay múltiples items y alguno necesita resolución manual,
    registra los que puede y guarda los pendientes.
    """
    negocio    = state["negocio"]
    negocio_id = state["negocio_id"]
    ahora      = _ahora_local(negocio)
    nombre_propio = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"

    # Leer items del NLP (1 a 5)
    items = state.get("items") or []

    # Compatibilidad: si llegó datos_nlp en lugar de items (un solo item)
    if not items and state.get("datos_nlp", {}).get("total"):
        items = [state["datos_nlp"]]

    if not items:
        return {**state, "respuesta": "¿Qué vendiste y a cuánto? 💰"}

    # Limitar a 5
    if len(items) > 5:
        items = items[:5]
        aviso_limite = "\n\n⚠️ Solo registré los primeros 5 productos. Envía el resto en otro mensaje."
    else:
        aviso_limite = ""

    confirmados   = []   # items registrados exitosamente
    pendientes    = []   # items que necesitan selección manual
    total_general = 0.0
    moneda_gral   = "PEN"

    for item in items:
        nombre_producto = item.get("producto", "producto")
        cantidad        = int(item.get("cantidad", 1))
        precio_unitario = item.get("precio_unitario")
        total_venta     = float(item.get("total", 0))
        moneda          = item.get("moneda", "PEN")
        simbolo         = SIMBOLOS.get(moneda, "S/")
        f_fecha         = item.get("fecha") or ahora.strftime("%Y-%m-%d")
        f_hora          = item.get("hora")  or ahora.strftime("%H:%M:%S")
        moneda_gral     = moneda

        resultado_stock = await stock_service.procesar_venta(
            negocio_id=negocio_id,
            nombre_producto=nombre_producto,
            cantidad=cantidad,
            precio_unitario=precio_unitario,
        )
        estado_stock = resultado_stock["estado"]

        if estado_stock == "exacto":
            producto_id  = resultado_stock["producto_id"]
            prod_nombre  = resultado_stock["producto_nombre"]
            prod_talla   = resultado_stock["producto_talla"]
            nombre_final = prod_nombre + (f" Talla {prod_talla}" if prod_talla else "")

            tx_id = await _guardar_transaccion(
                negocio_id, "venta", nombre_final, total_venta,
                moneda, item.get("fecha"), item.get("hora"), cantidad, producto_id,
            )
            descuento = await stock_service.ejecutar_descuento_venta(
                negocio_id=negocio_id, producto_id=producto_id,
                nombre_producto=nombre_producto, cantidad=cantidad, transaccion_id=tx_id,
            )
            total_general += total_venta
            confirmados.append({
                "nombre": nombre_final,
                "cantidad": cantidad,
                "total": total_venta,
                "simbolo": simbolo,
                "stock_msg": descuento["mensaje_stock"],
            })

        elif estado_stock in ("parcial", "sin_match"):
            # Guardar como pendiente para resolución
            pendientes.append({
                "item": item,
                "estado_stock": estado_stock,
                "candidatos": resultado_stock.get("candidatos", []),
                "nombre_producto": nombre_producto,
                "cantidad": cantidad,
                "precio_unitario": precio_unitario,
                "total_venta": total_venta,
                "moneda": moneda,
                "simbolo": simbolo,
                "f_fecha": f_fecha,
                "f_hora": f_hora,
            })

    # ── Armar respuesta ──────────────────────────────────

    # Caso 1: todo confirmado, sin pendientes
    if confirmados and not pendientes:
        lineas = [f"✅ Venta registrada, {nombre_propio}\n"]
        lineas.append(f"📅 {ahora.strftime('%Y-%m-%d')} {ahora.strftime('%H:%M')}\n")
        for c in confirmados:
            lineas.append(f"📝 {c['nombre']} x{c['cantidad']} → {c['simbolo']} {c['total']:.2f}")
        if len(confirmados) > 1:
            lineas.append(f"\n💰 Total: {SIMBOLOS.get(moneda_gral,'S/')} {total_general:.2f}")
        for c in confirmados:
            if c.get("stock_msg"):
                lineas.append(f"\n{c['stock_msg']}")
        respuesta = "\n".join(lineas) + aviso_limite
        return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}

    # Caso 2: algunos confirmados + algunos pendientes
    # Registramos los confirmados y guardamos los pendientes en sub_estado
    respuesta_parcial = ""
    if confirmados:
        lineas = [f"✅ Registré estos, {nombre_propio}:\n"]
        for c in confirmados:
            lineas.append(f"📝 {c['nombre']} x{c['cantidad']} → {c['simbolo']} {c['total']:.2f}")
        respuesta_parcial = "\n".join(lineas) + "\n\n"

    # Tomar el primer pendiente para resolverlo ahora
    primer_pendiente = pendientes[0]
    resto_pendientes = pendientes[1:]  # los demás esperan

    datos_pendientes = {
        "nombre_producto_original": primer_pendiente["nombre_producto"],
        "cantidad_stock":           primer_pendiente["cantidad"],
        "precio_unitario_stock":    primer_pendiente["precio_unitario"],
        "venta_monto":              primer_pendiente["total_venta"],
        "venta_moneda":             primer_pendiente["simbolo"],
        "venta_fecha":              primer_pendiente["f_fecha"],
        "venta_hora":               primer_pendiente["f_hora"],
        "venta_nombre_propio":      nombre_propio,
        "venta_moneda_codigo":      primer_pendiente["moneda"],
        "items_pendientes":         resto_pendientes,  # cola de pendientes
    }

    if primer_pendiente["estado_stock"] == "parcial":
        candidatos = primer_pendiente["candidatos"]
        lista = "\n".join(
            f"{i+1}. {p['nombre']}" + (f", Talla {p['talla']}" if p.get("talla") else "")
            + f" (stock: {p.get('cantidad_actual','?')})"
            for i, p in enumerate(candidatos)
        )
        datos_pendientes["candidatos_stock"] = candidatos
        datos_pendientes["operacion_stock"]  = "venta"
        respuesta = (
            respuesta_parcial +
            f"¿Cuál de estos es *{primer_pendiente['nombre_producto']}*? 🤔\n\n"
            f"{lista}\n\nEscribe el número, o *0* si no está en la lista."
        )
        sub_estado = "ESPERANDO_SELECCION_STOCK"

    else:  # sin_match
        respuesta = (
            respuesta_parcial +
            f"*{primer_pendiente['nombre_producto']}* no está en tu catálogo 🔍\n\n"
            f"¿Qué quieres hacer?\n"
            f"1️⃣ *Agregar* al catálogo\n"
            f"2️⃣ *Seguir* sin añadir al stock"
        )
        sub_estado = "ESPERANDO_DECISION_PRODUCTO_NUEVO"

    return {
        **state,
        "respuesta": respuesta + aviso_limite,
        "sub_estado": sub_estado,
        "datos_pendientes": {
            **datos_pendientes,
            "sub_estado": sub_estado,
        },
    }


# ══════════════════════════════════════════════════════════
#  NODO: GASTO (múltiples items)
# ══════════════════════════════════════════════════════════

async def gasto_node(state: QuriState) -> QuriState:
    """
    Registra 1 a 5 gastos en un mismo mensaje.
    Todos se insertan directamente (no hay matching de stock).
    """
    negocio    = state["negocio"]
    negocio_id = state["negocio_id"]
    ahora      = _ahora_local(negocio)
    nombre_propio = negocio.get("nombre_propietario") or negocio.get("nombre_negocio") or "Comerciante"

    items = state.get("items") or []

    # Compatibilidad con un solo item en datos_nlp
    if not items and state.get("datos_nlp", {}).get("monto"):
        items = [state["datos_nlp"]]

    if not items:
        return {**state, "respuesta": "¿Cuánto fue el gasto y en qué? 💸"}

    if len(items) > 5:
        items = items[:5]
        aviso_limite = "\n\n⚠️ Solo registré los primeros 5 gastos. Envía el resto en otro mensaje."
    else:
        aviso_limite = ""

    registrados   = []
    total_general = 0.0
    moneda_gral   = "PEN"

    for item in items:
        concepto  = item.get("concepto", "gasto")
        monto     = float(item.get("monto", 0))
        moneda    = item.get("moneda", "PEN")
        simbolo   = SIMBOLOS.get(moneda, "S/")
        categoria = str(item.get("categoria", "otros")).capitalize()
        moneda_gral = moneda

        await _guardar_transaccion(
            negocio_id, "gasto", concepto, monto, moneda,
            item.get("fecha"), item.get("hora"),
        )
        total_general += monto
        registrados.append({
            "concepto": concepto,
            "monto": monto,
            "simbolo": simbolo,
            "categoria": categoria,
        })

    # Armar respuesta
    f_fecha = ahora.strftime("%Y-%m-%d")
    f_hora  = ahora.strftime("%H:%M")
    lineas  = [f"✅ Gasto(s) registrado(s), {nombre_propio}\n"]
    lineas.append(f"📅 {f_fecha} {f_hora}\n")

    for g in registrados:
        lineas.append(f"🏷️ {g['categoria']} | {g['concepto']} → {g['simbolo']} {g['monto']:.2f}")

    if len(registrados) > 1:
        lineas.append(f"\n💸 Total gastado: {SIMBOLOS.get(moneda_gral,'S/')} {total_general:.2f}")

    respuesta = "\n".join(lineas) + aviso_limite
    return {**state, "respuesta": respuesta, "sub_estado": "", "datos_pendientes": {}}