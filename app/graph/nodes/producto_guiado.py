"""
app/graph/nodes/producto_guiado.py

Lógica compartida para agregar productos guiado paso a paso.
Usada tanto en el flujo activo (negocio.py) como en el onboarding.
"""
from app.services.gemini_service import gemini_service
from app.services.stock_service import stock_service
from app.database import get_pool
import logging

logger = logging.getLogger(__name__)


async def agregar_producto_guiado(
    negocio_id: str,
    mensaje: str,
    datos: dict,
) -> dict:
    """
    Retorna:
    {
        "respuesta": str,
        "finalizado": bool,
        "datos": dict,
        "agregar_otro": bool,
    }
    """
    formulario = datos.get("formulario_producto", {})
    msg_lower  = mensaje.strip().lower()

    if msg_lower in ("cancelar", "salir"):
        return {
            "respuesta": "Cancelado. ¿En qué más te ayudo? 😊",
            "finalizado": True,
            "datos": {},
            "agregar_otro": False,
        }

    # ── Si ya está en confirmación → interpretar acción ──
    if datos.get("inv_confirmado"):
        return await _handle_confirmacion(negocio_id, mensaje, msg_lower, formulario, datos)

    # ── Extraer campos del mensaje ──
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
            formulario["precio_compra"] = None

    def _datos_actualizados():
        return {**datos, "formulario_producto": formulario, "inv_confirmado": False}

    # ── Pedir campos faltantes uno a uno ──
    if not formulario.get("nombre"):
        return {
            "respuesta": "¿Cómo se llama el producto?\n_(Ej: Polo básico, Jean slim, Blusa floral)_",
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    if not formulario.get("talla"):
        return {
            "respuesta": (
                f"*{formulario['nombre']}* ✅\n\n"
                "📐 ¿Qué talla tiene?\n"
                "_(Ej: S, M, L, XL, 28, 30, Talla única)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    if formulario.get("precio_venta") is None:
        return {
            "respuesta": (
                f"*{formulario['nombre']}* talla *{formulario['talla']}* ✅\n\n"
                "💰 ¿Precio de venta en Soles?\n"
                "_(Ej: 35, 49.90, 120)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    if formulario.get("cantidad") is None:
        return {
            "respuesta": (
                f"*{formulario['nombre']}* · S/ {formulario['precio_venta']:.2f} ✅\n\n"
                "📦 ¿Cuántas unidades tienes?\n"
                "_(Ej: 10, 25 — escribe 0 si aún no tienes)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    if "precio_compra" not in formulario:
        formulario["precio_compra"] = "pendiente"
        return {
            "respuesta": (
                f"*{formulario['nombre']}* · {formulario['cantidad']} uds ✅\n\n"
                "💵 ¿Precio de compra en Soles?\n"
                "_(Opcional — escribe 'no' para omitirlo)_"
            ),
            "finalizado": False,
            "datos": _datos_actualizados(),
            "agregar_otro": False,
        }

    # ── Todos los campos listos → mostrar confirmación ──
    precio_compra_final = formulario.get("precio_compra")
    if precio_compra_final == "pendiente":
        precio_compra_final = None
    formulario["precio_compra"] = precio_compra_final

    talla_txt   = f"[Talla {formulario['talla']}]" if formulario.get("talla") else "sin talla"
    precio_comp = f"S/ {precio_compra_final:.2f}" if precio_compra_final else "(no especificado)"

    return {
        "respuesta": (
            f"📝 *{formulario['nombre']}* {talla_txt}\n"
            f"💰 Precio venta: S/ {formulario['precio_venta']:.2f}\n"
            f"📦 Stock: {formulario['cantidad']} unidades\n"
            f"💵 Precio compra: {precio_comp}\n\n"
            "¿Todo bien? Escribe *guardar*, edita lo que necesites, o *cancelar*."
        ),
        "finalizado": False,
        "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
        "agregar_otro": False,
    }


async def _handle_confirmacion(
    negocio_id: str,
    mensaje: str,
    msg_lower: str,
    formulario: dict,
    datos: dict,
) -> dict:
    """Maneja el turno de confirmación: guardar, editar o cancelar."""

    # ── Cancelar ──
    if msg_lower in ("cancelar", "salir", "no"):
        return {
            "respuesta": "Cancelado. ¿En qué más te ayudo? 😊",
            "finalizado": True,
            "datos": {},
            "agregar_otro": False,
        }

    # ── Guardar ──
    quiere_guardar = any(w in msg_lower for w in [
        "guardar", "sí", "si", "ok", "dale", "listo", "bien", "confirmar", "perfecto"
    ])

    if quiere_guardar:
        return await _guardar_producto(negocio_id, formulario, datos)

    # ── Editar: el LLM extrae los campos a cambiar ──
    campos = await gemini_service.extraer_producto_inventario(mensaje, formulario)
    hubo_cambio = False

    if campos.get("nombre"):
        formulario["nombre"] = campos["nombre"]
        hubo_cambio = True
    if campos.get("talla"):
        formulario["talla"] = campos["talla"]
        hubo_cambio = True
    if campos.get("precio_venta") is not None:
        formulario["precio_venta"] = campos["precio_venta"]
        hubo_cambio = True
    if campos.get("precio_compra") is not None:
        formulario["precio_compra"] = campos["precio_compra"]
        hubo_cambio = True
    if campos.get("cantidad") is not None:
        formulario["cantidad"] = campos["cantidad"]
        hubo_cambio = True

    if hubo_cambio:
        talla_txt   = f"[Talla {formulario['talla']}]" if formulario.get("talla") else "sin talla"
        precio_comp = f"S/ {formulario['precio_compra']:.2f}" if formulario.get("precio_compra") else "(no especificado)"
        return {
            "respuesta": (
                f"✅ Actualizado.\n\n"
                f"📝 *{formulario['nombre']}* {talla_txt}\n"
                f"💰 Precio venta: S/ {formulario['precio_venta']:.2f}\n"
                f"📦 Stock: {formulario['cantidad']} unidades\n"
                f"💵 Precio compra: {precio_comp}\n\n"
                "¿Todo bien? Escribe *guardar*, edita algo más, o *cancelar*."
            ),
            "finalizado": False,
            "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
            "agregar_otro": False,
        }

    # No entendió
    return {
        "respuesta": (
            "No entendí bien 😅\n"
            "Escribe *guardar* para confirmar, dime qué cambiar, o *cancelar*."
        ),
        "finalizado": False,
        "datos": {**datos, "formulario_producto": formulario, "inv_confirmado": True},
        "agregar_otro": False,
    }


async def _guardar_producto(
    negocio_id: str,
    formulario: dict,
    datos: dict,
) -> dict:
    """Verifica duplicado, inserta en BD y retorna respuesta final."""

    # Verificar duplicado
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
            formulario["nombre"].strip(),
            formulario.get("talla") or "",
        )
    if row:
        talla_txt = f" [Talla {formulario['talla']}]" if formulario.get("talla") else ""
        return {
            "respuesta": (
                f"⚠️ *{formulario['nombre']}{talla_txt}* ya está en tu catálogo.\n\n"
                "¿Quieres agregar otro producto? 😊"
            ),
            "finalizado": True,
            "datos": {},
            "agregar_otro": False,
        }

    try:
        await stock_service.crear_producto(
            negocio_id       = negocio_id,
            nombre           = formulario["nombre"],
            talla            = formulario.get("talla"),
            precio_venta     = formulario["precio_venta"],
            precio_costo     = formulario.get("precio_compra"),
            cantidad_inicial = formulario["cantidad"],
        )
    except Exception as e:
        logger.error(f"[producto_guiado] Error al guardar: {e}")
        return {
            "respuesta": "⚠️ Hubo un problema al guardar. Intenta de nuevo o escribe 'cancelar'.",
            "finalizado": False,
            "datos": datos,
            "agregar_otro": False,
        }

    nombre_final = formulario["nombre"] + (
        f" [Talla {formulario['talla']}]" if formulario.get("talla") else ""
    )
    precio_comp  = formulario.get("precio_compra")
    respuesta = (
        f"✅ *{nombre_final}* agregado a tu catálogo 📦\n\n"
        f"💰 Precio venta: S/ {formulario['precio_venta']:.2f}\n"
        f"📦 Stock inicial: {formulario['cantidad']} unidades\n"
    )
    if precio_comp:
        respuesta += f"💵 Precio compra: S/ {precio_comp:.2f}\n"

    return {
        "respuesta": respuesta,
        "finalizado": True,
        "datos": {},
        "agregar_otro": False,
    }