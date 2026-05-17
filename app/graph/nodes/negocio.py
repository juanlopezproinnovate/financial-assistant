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
            nombre_final = prod_nombre + (f" [Talla {prod_talla}]" if prod_talla else "")

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

async def catalogo_node(state: QuriState) -> QuriState:
    """
    Consulta los productos registrados del negocio en Supabase.
 
    Comportamiento:
    - Si tiene 10 o menos → muestra todos con stock y precio
    - Si tiene más de 10  → muestra los 10 más recientes + avisa el total
    - Si el usuario filtró (ej: "mis blusas") → filtra por nombre/talla/categoría
    - Stock ≤ 5 unidades → emoji de advertencia ⚠️
    """
    negocio_id = state["negocio_id"]
    datos      = state.get("datos_nlp", {}) or {}
    filtro     = datos.get("filtro")  # str o None
 
    pool = await get_pool()
    async with pool.acquire() as conn:
 
        # ── Total de productos activos ──────────────────────
        total_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total FROM productos WHERE negocio_id = $1 AND activo = true",
            negocio_id,
        )
        total = int(total_row["total"])
 
        if total == 0:
            return {
                **state,
                "respuesta": (
                    "Aún no tienes productos registrados en tu catálogo 📦\n\n"
                    "Puedes agregar uno diciéndome, por ejemplo:\n"
                    "_'Llegaron 20 polos talla M a S/15'_"
                ),
                "sub_estado": "",
                "datos_pendientes": {},
            }
 
        # ── Query con o sin filtro ──────────────────────────
        base_select = """
            SELECT
                p.nombre,
                p.talla,
                p.precio_venta_pen,
                p.precio_costo,
                COALESCE(s.cantidad_actual, 0) AS stock,
                c.nombre AS categoria,
                p.created_at
            FROM productos p
            LEFT JOIN stock s      ON s.producto_id = p.id
            LEFT JOIN categorias c ON c.id = p.categoria_id
            WHERE p.negocio_id = $1 AND p.activo = true
        """
 
        if filtro:
            rows = await conn.fetch(
                base_select + """
                  AND (
                    LOWER(p.nombre)  ILIKE $2 OR
                    LOWER(p.talla)   ILIKE $2 OR
                    LOWER(c.nombre)  ILIKE $2
                  )
                ORDER BY p.created_at DESC
                LIMIT 10
                """,
                negocio_id,
                f"%{filtro.lower()}%",
            )
        else:
            rows = await conn.fetch(
                base_select + "ORDER BY p.created_at DESC LIMIT 10",
                negocio_id,
            )
 
    productos = [dict(r) for r in rows]
    mostrados = len(productos)
 
    # ── Sin resultados para el filtro ───────────────────────
    if filtro and not productos:
        return {
            **state,
            "respuesta": (
                f"No encontré productos con \"{filtro}\" en tu catálogo 🔍\n"
                "Prueba con otro nombre o categoría."
            ),
            "sub_estado": "",
            "datos_pendientes": {},
        }
 
    # ── Encabezado ──────────────────────────────────────────
    if filtro:
        encabezado = f"📦 Productos con \"{filtro}\" ({mostrados} encontrado{'s' if mostrados != 1 else ''}):\n\n"
    elif total <= 10:
        encabezado = f"📦 Tu catálogo completo ({total} producto{'s' if total != 1 else ''}):\n\n"
    else:
        encabezado = (
            f"📦 Tienes *{total} productos* registrados en total.\n"
            f"Aquí los últimos {mostrados}:\n\n"
        )
 
    # ── Líneas por producto ─────────────────────────────────
    lineas = []
    for i, p in enumerate(productos, 1):
        nombre    = p["nombre"] or "Sin nombre"
        talla     = f" · Talla {p['talla']}" if p.get("talla") else ""
        precio    = f"S/ {float(p['precio_venta_pen']):.2f}" if p.get("precio_venta_pen") else "sin precio"
        stock     = int(p["stock"])
        categoria = f" [{p['categoria']}]" if p.get("categoria") else ""
 
        # Alerta visual si el stock está bajo
        stock_emoji = "⚠️" if 0 < stock <= 5 else ("❌" if stock == 0 else "📦")
 
        lineas.append(
            f"{i}. *{nombre}*{talla}{categoria}\n"
            f"   💰 {precio} · {stock_emoji} Stock: {stock} uds"
        )
 
    # ── Pie de página si hay más de 10 ─────────────────────
    pie = ""
    if total > 10 and not filtro:
        pie = (
            f"\n\n_Mostrando los últimos {mostrados} de {total} productos._\n"
            "Para buscar uno específico dime, por ejemplo:\n"
            "_'muéstrame mis blusas'_ o _'qué tengo en talla M'_"
        )
 
    respuesta = encabezado + "\n".join(lineas) + pie
 
    return {
        **state,
        "respuesta": respuesta,
        "sub_estado": "",
        "datos_pendientes": {},
    }


# ══════════════════════════════════════════════════════════
#  NODO: INVENTARIO
# ══════════════════════════════════════════════════════════

async def inventario_node(state: QuriState) -> QuriState:
    datos        = state.get("datos_nlp", {})
    negocio_id   = state["negocio_id"]

    if not datos.get("producto"):
        formulario = {
            "nombre": None,
            "talla": None,
            "precio_venta": None,
            "precio_compra": None,
            "cantidad": None,
        }
        datos_pendientes = {
            "formulario_producto": formulario,
            "inv_sub_paso": "nombre",
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
        }
        return {
            **state,
            "respuesta": (
                "¡Claro! Vamos a registrar el producto 📦\n\n"
                "Necesito estos datos:\n"
                "📝 *Nombre* — ej: Polo básico, Jean slim\n"
                "📐 *Talla* — ej: S, M, L, XL, 28, 30\n"
                "💰 *Precio de venta* — ej: S/ 35\n"
                "📦 *Stock* — cuántas unidades tienes\n"
                "💵 *Precio de compra* — opcional\n\n"
                "Puedes enviarlo todo junto o paso a paso 👇\n"
                "_(Ej: Jean slim talla 28, precio 45 soles, stock 20)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": datos_pendientes,
        }

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
    logger.info(f"[SubEstado] sub_estado={sub_estado}")
    logger.info(f"[SubEstado] datos_pendientes={datos_pendientes}")
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

    if sub_estado == "AGREGAR_PRODUCTO_GUIADO":
        return await _handle_agregar_producto_guiado(state, datos_pendientes, negocio_id, mensaje)


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
                nombre_final = prod_nombre + (f" [Talla {prod_talla}]" if prod_talla else "")

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


async def _handle_agregar_producto_guiado(state, datos, negocio_id, mensaje):
    """
    Flujo guiado para agregar un producto nuevo desde el chat activo
    (fuera del onboarding). Recolecta campos uno a uno usando el mismo
    extractor LLM que usa el onboarding, y persiste el sub_estado en
    cada turno vía datos_pendientes (que graph.py guarda en la BD).
    """

    logger.info(f"[AgregarProducto] datos recibidos: {datos}")
    logger.info(f"[AgregarProducto] formulario: {datos.get('formulario_producto')}")
    
    formulario = datos.get("formulario_producto", {})
    msg_lower  = mensaje.strip().lower()

    # ── Cancelar explícito ──
    if msg_lower in ("cancelar", "salir"):
        return {
            **state,
            "respuesta": "Cancelado. ¿En qué más te ayudo? 😊",
            "sub_estado": "",
            "datos_pendientes": {},
        }

    if datos.get("inv_guardado"):
        return {
            **state,
            "respuesta": "¿En qué más te ayudo? 😊",
            "sub_estado": "",
            "datos_pendientes": {},
        }

    # ── Extraer lo que venga en el mensaje con el LLM ──
    campos = await gemini_service.extraer_producto_inventario(mensaje, formulario)

    if campos.get("nombre"):
        formulario["nombre"] = campos["nombre"]
    if campos.get("talla"):
        formulario["talla"] = campos["talla"]
    if campos.get("precio_venta") is not None:
        formulario["precio_venta"] = campos["precio_venta"]
    if campos.get("precio_compra") is not None:
        formulario["precio_compra"] = campos["precio_compra"]
    if campos.get("cantidad") is not None:
        formulario["cantidad"] = campos["cantidad"]

    # Omisión explícita de precio_compra
    if msg_lower in ("no", "omitir", "saltar", "-", "n/a", "nada", "0"):
        if (formulario.get("nombre") and formulario.get("talla")
                and formulario.get("precio_venta") is not None
                and formulario.get("cantidad") is not None):
            formulario["precio_compra"] = None  # confirmado como omitido

    def _actualizar_datos():
        return {
            **datos,
            "formulario_producto": formulario,
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
        }

    # ── Pedir campos faltantes uno a uno ──
    if not formulario.get("nombre"):
        return {
            **state,
            "respuesta": (
                "¿Cómo se llama el producto?\n"
                "_(Ej: Polo básico, Jean slim, Blusa floral)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": _actualizar_datos(),
        }

    if not formulario.get("talla"):
        return {
            **state,
            "respuesta": (
                f"*{formulario['nombre']}* ✅\n\n"
                "📐 ¿Qué talla tiene?\n"
                "_(Ej: S, M, L, XL, 28, 30, Talla única)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": _actualizar_datos(),
        }

    if formulario.get("precio_venta") is None:
        return {
            **state,
            "respuesta": (
                f"*{formulario['nombre']}* talla *{formulario['talla']}* ✅\n\n"
                "💰 ¿Cuál es el precio de venta en Soles?\n"
                "_(Ej: 35, 49.90, 120)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": _actualizar_datos(),
        }

    if formulario.get("cantidad") is None:
        return {
            **state,
            "respuesta": (
                f"*{formulario['nombre']}* · S/ {formulario['precio_venta']:.2f} ✅\n\n"
                "📦 ¿Cuántas unidades tienes en stock?\n"
                "_(Ej: 10, 25 — escribe 0 si aún no tienes)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": _actualizar_datos(),
        }

    # precio_compra: None = no preguntado aún, valor real o False = ya respondido
    if "precio_compra" not in formulario:
        formulario["precio_compra"] = "pendiente"  # marca para el próximo turno
        return {
            **state,
            "respuesta": (
                f"*{formulario['nombre']}* · {formulario['cantidad']} uds ✅\n\n"
                "💵 ¿Cuál es el precio de compra (costo) en Soles?\n"
                "_(Opcional — escribe 'no' para omitirlo)_"
            ),
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": _actualizar_datos(),
        }

    # ── Todos los campos listos → guardar ──
    precio_compra_final = formulario.get("precio_compra")
    if precio_compra_final == "pendiente":
        precio_compra_final = None  # no respondió, omitimos

    ya_existe = await _producto_ya_existe(
        negocio_id,
        formulario["nombre"],
        formulario.get("talla"),
    )
    if ya_existe:
        talla_txt = f" Talla {formulario['talla']}" if formulario.get("talla") else ""
        return {
            **state,
            "respuesta": (
                f"⚠️ *{formulario['nombre']}{talla_txt}* ya está en tu catálogo.\n\n"
                f"¿Quieres registrar otro producto diferente o en qué más te ayudo? 😊"
            ),
            "sub_estado": "",
            "datos_pendientes": {},
        }

    try:
        await stock_service.crear_producto(
            negocio_id       = negocio_id,
            nombre           = formulario["nombre"],
            talla            = formulario.get("talla"),
            precio_venta     = formulario["precio_venta"],
            precio_costo     = precio_compra_final,
            cantidad_inicial = formulario["cantidad"],
        )
    except Exception as e:
        logger.error(f"[agregar_producto_guiado] Error al guardar: {e}")
        return {
            **state,
            "respuesta": "⚠️ Hubo un problema al guardar. Intenta de nuevo o escribe 'cancelar'.",
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": datos,
        }

    nombre_final = formulario["nombre"] + (
        f" Talla {formulario['talla']}" if formulario.get("talla") else ""
    )
    respuesta = (
        f"✅ *{nombre_final}* agregado a tu catálogo 📦\n\n"
        f"📐 Talla: {formulario.get('talla', '—')}\n"
        f"💰 Precio venta: S/ {formulario['precio_venta']:.2f}\n"
        f"📦 Stock inicial: {formulario['cantidad']} unidades\n"
    )
    if precio_compra_final:
        respuesta += f"💵 Precio compra: S/ {precio_compra_final:.2f}\n"
    respuesta += "\n¿Quieres agregar otro producto o en qué más te ayudo? 😊"

    return {
        **state,
        "respuesta": respuesta,
        "sub_estado": "",
        "datos_pendientes": {},
    }

async def _producto_ya_existe(negocio_id: str, nombre: str, talla: str) -> bool:
    """Verifica si ya existe un producto con el mismo nombre y talla."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM productos
            WHERE negocio_id = $1
              AND LOWER(nombre) = LOWER($2)
              AND LOWER(COALESCE(talla, '')) = LOWER(COALESCE($3, ''))
              AND activo = true
            LIMIT 1
            """,
            negocio_id,
            nombre.strip(),
            talla or "",
        )
    return row is not None

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