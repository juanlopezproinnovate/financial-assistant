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
from app.graph.nodes.producto_guiado import agregar_producto_guiado


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
                   t.cantidad, t.producto_id::text,
                   to_char(t.created_at AT TIME ZONE COALESCE(n.zona_horaria,'America/Lima'), 'DD/MM') AS fecha_corta
            FROM transacciones t
            JOIN negocios n ON n.id = t.negocio_id
            WHERE t.negocio_id = $1::uuid
            ORDER BY t.created_at DESC LIMIT $2
            """,
            negocio_id, limite,
        )
    return [
        {
            **dict(r),
            "id": str(r["id"]),
            "monto": float(r["monto"] or 0),
            "cantidad": int(r["cantidad"] or 1),
            "producto_id": r["producto_id"],
        }
        for r in rows
    ]


# REEMPLAZAR la función completa
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
        # ── Totales generales ──
        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(monto) FILTER (WHERE tipo='venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo='gasto'), 0) AS total_gastos,
                COALESCE(SUM(cantidad) FILTER (WHERE tipo='venta'), 0) AS total_unidades,
                COUNT(*) FILTER (WHERE tipo='venta') AS num_ventas
            FROM transacciones
            WHERE negocio_id = $1 AND {where}
            """,
            negocio_id,
        )

        # ── Producto más vendido (por monto) ──
        top_row = await conn.fetchrow(
            f"""
            SELECT descripcion, SUM(monto) AS total_monto
            FROM transacciones
            WHERE negocio_id = $1 AND tipo = 'venta' AND {where}
              AND descripcion IS NOT NULL
            GROUP BY descripcion
            ORDER BY total_monto DESC
            LIMIT 1
            """,
            negocio_id,
        )

    tv = float(row["total_ventas"])
    tg = float(row["total_gastos"])
    return {
        "periodo":            periodo,
        "total_ventas":       tv,
        "total_gastos":       tg,
        "num_transacciones":  int(row["num_ventas"]),
        "total_unidades":     int(row["total_unidades"]),
        "ganancia_neta":      tv - tg,
        "producto_top":       top_row["descripcion"] if top_row else None,
        "producto_top_monto": float(top_row["total_monto"]) if top_row else 0.0,
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
    categoria_id: str = None,
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
                     fecha, hora, origen_registro, cantidad, producto_id, categoria_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'whatsapp',$8,$9,$10)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto),
                moneda, fecha_obj, hora_obj, cantidad, producto_id, categoria_id,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO transacciones
                    (negocio_id, tipo, descripcion, monto, moneda,
                     fecha, origen_registro, cantidad, producto_id, categoria_id)
                VALUES ($1,$2,$3,$4,$5,CURRENT_DATE,'whatsapp',$6,$7,$8)
                RETURNING id::text
                """,
                negocio_id, tipo, descripcion, float(monto),
                moneda, cantidad, producto_id, categoria_id,
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
        total_venta = float(item.get("total")) if item.get("total") is not None else None
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

        if (total_venta is None or total_venta == 0) and estado_stock == "exacto":
            precio_catalogo = resultado_stock.get("precio_venta")
            if precio_catalogo:
                precio_unitario = float(precio_catalogo)
                total_venta     = precio_unitario * cantidad
            else:
                # ← CAMBIO: no interrumpir, agregar a pendientes
                pendientes.append({
                    **item,
                    "estado_stock": "sin_precio",
                    "candidatos": [],
                    "nombre_producto": nombre_producto,
                    "cantidad": cantidad,
                    "precio_unitario": None,
                    "total_venta": 0.0,
                    "moneda": moneda,
                    "simbolo": simbolo,
                    "f_fecha": f_fecha,
                    "f_hora": f_hora,
                })
                continue
        elif total_venta is None or total_venta == 0:
            total_venta = 0.0

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
            f"¿A cuál producto te refieres? 🤔\n\n"
            f"{lista}\n\nDime el número o avísame si *no está* en la lista."
        )
        sub_estado = "ESPERANDO_SELECCION_STOCK"

    elif primer_pendiente["estado_stock"] == "sin_precio":  # ← NUEVO
        respuesta = (
            respuesta_parcial +
            f"¿A cuánto vendiste *{primer_pendiente['nombre_producto']}*? 💰\n"
            f"_(No tengo precio registrado para ese producto)_"
        )
        sub_estado = "ESPERANDO_PRECIO_VENTA"

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

    # Obtener categorías de gasto disponibles para este negocio
    pool = await get_pool()
    async with pool.acquire() as conn:
        cats = await conn.fetch("SELECT id, nombre FROM categorias WHERE negocio_id = $1 AND tipo = 'gasto'", negocio_id)
        categorias_gasto = [{"id": str(r["id"]), "nombre": r["nombre"]} for r in cats]

    registrados   = []
    total_general = 0.0
    moneda_gral   = "PEN"

    for item in items:
        concepto  = item.get("concepto", "gasto")
        monto     = float(item.get("monto", 0))
        moneda    = item.get("moneda", "PEN")
        simbolo   = SIMBOLOS.get(moneda, "S/")
        
        # Clasificar el gasto en una de las categorías existentes usando IA
        categoria_id = await gemini_service.clasificar_gasto(concepto, categorias_gasto)
        
        # Buscar el nombre de la categoría para mostrarlo en el resumen
        categoria_nombre = "Otros"
        if categoria_id:
            cat_match = next((c["nombre"] for c in categorias_gasto if str(c["id"]) == categoria_id), None)
            if cat_match:
                categoria_nombre = cat_match.capitalize()

        moneda_gral = moneda

        await _guardar_transaccion(
            negocio_id, "gasto", concepto, monto, moneda,
            item.get("fecha"), item.get("hora"),
            categoria_id=categoria_id
        )
        total_general += monto
        registrados.append({
            "concepto": concepto,
            "monto": monto,
            "simbolo": simbolo,
            "categoria": categoria_nombre,
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
    - Muestra los 7 más recientes + avisa el total si hay más
    - Si el usuario filtró (ej: "mis blusas") → filtra por nombre/talla/categoría
    - Stock ≤ 5 unidades → emoji de advertencia ⚠️
    - Siempre incluye el link al dashboard web al final
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
                LIMIT 7
                """,
                negocio_id,
                f"%{filtro.lower()}%",
            )
        else:
            rows = await conn.fetch(
                base_select + "ORDER BY p.created_at DESC LIMIT 7",
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
    elif total <= 7:
        encabezado = f"📦 Tu catálogo ({total} producto{'s' if total != 1 else ''}):\n\n"
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
 
        # Alerta visual si el stock está bajo
        stock_emoji = "⚠️" if 0 < stock <= 5 else ("❌" if stock == 0 else "📦")
 
        lineas.append(
            f"{i}. *{nombre}*{talla}\n"
            f"   💰 {precio} · {stock_emoji} Stock: {stock} uds"
        )
 
    # ── Pie de página con el link al dashboard web ──
    pie = "\n\n🌐 Puedes ver tu inventario completo en tu web:\n👉 http://bit.ly/4dIQAVB"
 
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
    negocio_id = state["negocio_id"]
    
    # Extraer el primer item (o datos_nlp por compatibilidad)
    items = state.get("items") or []
    if items:
        datos = items[0]
    else:
        datos = state.get("datos_nlp", {})

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
            "precio_venta":             precio_venta,
            "precio_costo":             precio_costo,
        }
        return {
            **state,
            "respuesta": resultado["mensaje"],
            "sub_estado": "ESPERANDO_SELECCION_STOCK",
            "datos_pendientes": _persist_sub_estado(datos_pendientes, "ESPERANDO_SELECCION_STOCK"),
        }

    # sin_match
    separado      = await gemini_service.extraer_nombre_y_talla(nombre_producto)
    nombre_limpio = separado.get("nombre") or nombre_producto.strip().title()
    talla         = separado.get("talla")
    formulario    = {
        "nombre": nombre_limpio, 
        "talla": talla,
        "cantidad": cantidad, 
        "precio_venta": precio_venta, 
        "precio_compra": precio_costo,
    }
    datos_pendientes = {
        "operacion_stock": f"inventario_{tipo_inv}",
        "cantidad_stock": cantidad,
        "nombre_producto_original": nombre_producto,
        "formulario_producto": formulario,
        "es_desde_venta": False
    }
    
    from app.graph.nodes.producto_guiado import agregar_producto_guiado
    resultado = await agregar_producto_guiado(negocio_id, "", datos_pendientes)
    
    return {
        **state,
        "respuesta": f"Este producto no está en tu catálogo 🔍\nVamos a agregarlo 📦\n\n{resultado['respuesta']}",
        "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
        "datos_pendientes": _persist_sub_estado({**resultado.get("datos", {}), "sub_estado": "AGREGAR_PRODUCTO_GUIADO"}, "AGREGAR_PRODUCTO_GUIADO"),
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
        # ── NUEVO: mostrar cantidad ──
        cant_txt = f" x{t['cantidad']}" if t.get("cantidad") and t["cantidad"] > 1 else ""
        lineas.append(f"{i}. {t.get('fecha_corta','')} | {t['descripcion']}{cant_txt} | {s} {t['monto']:.2f}")
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

    if es_nuevo:
        if operacion == "venta":
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
        else:
            separado = await gemini_service.extraer_nombre_y_talla(nombre_orig)
            nombre_limpio = separado.get("nombre") or nombre_orig.strip().title()
            talla = separado.get("talla")
            formulario = {
                "nombre": nombre_limpio, 
                "talla": talla,
                "cantidad": cantidad, 
                "precio_venta": datos.get("precio_venta"), 
                "precio_compra": datos.get("precio_costo"),
            }
            nuevos_datos = {
                **datos,
                "formulario_producto": formulario,
                "es_desde_venta": False
            }
            from app.graph.nodes.producto_guiado import agregar_producto_guiado
            resultado = await agregar_producto_guiado(negocio_id, "", nuevos_datos)
            return {
                **state,
                "respuesta": f"Entendido, vamos a agregarlo al catálogo 📦\n\n{resultado['respuesta']}",
                "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
                "datos_pendientes": {**resultado.get("datos", {}), "sub_estado": "AGREGAR_PRODUCTO_GUIADO"},
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
            cant_txt = f" x{tx['cantidad']}" if tx.get("cantidad") and tx["cantidad"] > 1 else " x1"
            respuesta = (
                f"Elegiste: {tx['descripcion']}{cant_txt} ({simbolo} {tx['monto']:.2f})\n\n"
                f"¿Qué deseas cambiar?\n"
                f"_(ej. 'cambia el monto a 50', 'cambia la cantidad a 3', o 'cancelar')_"
            )
            return {
                **state,
                "respuesta": respuesta,
                "sub_estado": "ESPERANDO_EDICION_TRANSACCION",
                "datos_pendientes": {
                    **datos,
                    "transaccion_a_editar": tx,
                    "sub_estado": "ESPERANDO_EDICION_TRANSACCION"
                },
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
    if not cambios:
        return {
            **state,
            "respuesta": "No entendí qué cambiar 🤔 Intenta de otra forma o escribe 'cancelar'.",
            "sub_estado": state["sub_estado"], "datos_pendientes": datos,
        }

    cantidad_anterior = int(tx.get("cantidad") or 1)
    cantidad_nueva    = int(cambios.get("cantidad")) if cambios.get("cantidad") is not None else None
    producto_id       = tx.get("producto_id")

    # ── Actualizar transacción en BD ──
    sets   = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(cambios))
    pool   = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE transacciones SET {sets} WHERE id = $1::uuid",
            tx["id"], *list(cambios.values()),
        )

    # ── Compensar stock si cambió la cantidad y hay producto vinculado ──
    msg_stock = ""
    if cantidad_nueva is not None and producto_id and tx["tipo"] == "venta":
        diferencia = cantidad_nueva - cantidad_anterior
        if diferencia > 0:
            # Vendió más → descontar más stock
            await stock_service.descontar_stock_bd(
                producto_id=producto_id,
                negocio_id=negocio_id,
                cantidad=diferencia,
                transaccion_id=tx["id"],
            )
            msg_stock = f"\n📦 Stock ajustado: se descontaron {diferencia} unidad(es) adicional(es)."
        elif diferencia < 0:
            # Vendió menos → reponer stock
            await stock_service.reponer_stock_bd(
                producto_id=producto_id,
                negocio_id=negocio_id,
                cantidad=abs(diferencia),
                motivo="correccion_edicion",
            )
            msg_stock = f"\n📦 Stock ajustado: se repusieron {abs(diferencia)} unidad(es)."

    cambios_str = ", ".join(f"{k}: {v}" for k, v in cambios.items())
    return {
        **state,
        "respuesta": f"✅ Actualizado.\nCambios: {cambios_str}{msg_stock}",
        "sub_estado": "", "datos_pendientes": {},
    }


async def _handle_decision_producto_nuevo(state, datos, negocio_id, mensaje):
    nombre_prod     = datos.get("nombre_producto_original", "")
    cantidad        = datos.get("cantidad_stock", 1)
    precio_unitario = datos.get("precio_unitario_stock")

    decision = await gemini_service.interpretar_decision_producto_nuevo(mensaje)
    accion   = decision["accion"]

    operacion = datos.get("operacion_stock", "venta")
    es_desde_venta = (operacion == "venta")

    if accion == "AGREGAR":
        separado      = await gemini_service.extraer_nombre_y_talla(nombre_prod)
        nombre_limpio = separado.get("nombre") or nombre_prod.strip().title()
        talla         = separado.get("talla")
        formulario    = {
            "nombre": nombre_limpio, 
            "talla": talla,
            "cantidad": cantidad if not es_desde_venta else None, # si es inventario, prellenar cantidad
            "precio_venta": precio_unitario, 
            "precio_compra": None,
        }
        nuevos_datos = {
            **datos, 
            "formulario_producto": formulario, 
            "es_desde_venta": es_desde_venta
        }
        
        # Pasar un mensaje vacío para evitar que el número de opción ("1") sea interpretado como stock
        resultado = await agregar_producto_guiado(negocio_id, "", nuevos_datos)
        
        return {
            **state,
            "respuesta": "Genial, vamos a agregarlo al catálogo primero. 📦\n\n" + resultado["respuesta"],
            "sub_estado": "AGREGAR_PRODUCTO_GUIADO",
            "datos_pendientes": {**resultado["datos"], "sub_estado": "AGREGAR_PRODUCTO_GUIADO"},
        }

    if accion == "CANCELAR" or not es_desde_venta:
        return {
            **state, 
            "respuesta": "Operación cancelada. ¿En qué más te ayudo? 😊", 
            "sub_estado": "", 
            "datos_pendientes": {}
        }

    # CONTINUAR y es desde venta → registrar venta sin producto_id
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
    resultado = await agregar_producto_guiado(negocio_id, mensaje, datos)
    
    if resultado["finalizado"] and datos.get("es_desde_venta"):
        producto_id_nuevo = resultado.get("producto_id_nuevo")
        cantidad_vendida  = datos.get("cantidad_stock", 1)
        nombre_prod       = datos.get("nombre_producto_original", "")
        simbolo           = datos.get("venta_moneda", "S/")
        nombre_propio     = datos.get("venta_nombre_propio", "Comerciante")
        
        if producto_id_nuevo:
            tx_id = await _guardar_transaccion(
                negocio_id, "venta", nombre_prod,
                datos.get("venta_monto", 0), datos.get("venta_moneda_codigo", "PEN"),
                datos.get("venta_fecha"), datos.get("venta_hora"),
                cantidad_vendida, producto_id_nuevo,
            )
            descuento = await stock_service.ejecutar_descuento_venta(
                negocio_id=negocio_id, producto_id=producto_id_nuevo,
                nombre_producto=nombre_prod, cantidad=cantidad_vendida, transaccion_id=tx_id,
            )
            respuesta = (
                resultado["respuesta"] + 
                f"\n\n✅ Y listo, la venta también quedó registrada, {nombre_propio}\n"
                f"📝 {nombre_prod} x{cantidad_vendida} → {simbolo} {datos.get('venta_monto', 0):.2f}\n"
                + descuento["mensaje_stock"]
            )
        else:
            # Hubo error o se canceló el flow guiado y retornó finalizado sin producto_id
            respuesta = resultado["respuesta"]

        return {
            **state,
            "respuesta": respuesta,
            "sub_estado": "",
            "datos_pendientes": {},
        }

    return {
        **state,
        "respuesta": resultado["respuesta"],
        "sub_estado": "" if resultado["finalizado"] else "AGREGAR_PRODUCTO_GUIADO",
        "datos_pendientes": {**resultado["datos"], "sub_estado": "AGREGAR_PRODUCTO_GUIADO"} if not resultado["finalizado"] else {},
    }