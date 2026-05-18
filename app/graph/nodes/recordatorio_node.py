"""
app/graph/nodes/recordatorio_node.py
Nodo que extrae fecha/hora y mensaje del recordatorio y lo guarda en BD.
"""
import logging
import datetime
from zoneinfo import ZoneInfo
from app.graph.state import QuriState
from app.database import get_pool

logger = logging.getLogger(__name__)


async def recordatorio_node(state: QuriState) -> QuriState:
    negocio    = state["negocio"]
    negocio_id = state["negocio_id"]
    datos      = state.get("datos_nlp", {}) or {}

    nombre_propio = negocio.get("nombre_propietario") or "Comerciante"
    zona_str      = negocio.get("zona_horaria") or "America/Lima"
    try:
        tz = ZoneInfo(zona_str)
    except Exception:
        tz = ZoneInfo("America/Lima")

    mensaje_recordatorio = datos.get("mensaje_recordatorio", "")
    fecha_hora_str       = datos.get("fecha_hora", "")

    if not mensaje_recordatorio or not fecha_hora_str:
        return {
            **state,
            "respuesta": (
                "No entendí bien el recordatorio 😅\n"
                "Dime así: _'Recuérdame a las 5pm recoger la mercadería'_"
            ),
            "sub_estado": "",
            "datos_pendientes": {},
        }

    # Parsear fecha_hora (viene como string ISO del LLM)
    try:
        fecha_hora_local = datetime.datetime.fromisoformat(fecha_hora_str)
        if fecha_hora_local.tzinfo is None:
            fecha_hora_local = fecha_hora_local.replace(tzinfo=tz)
        fecha_hora_utc = fecha_hora_local.astimezone(datetime.timezone.utc)
    except Exception:
        return {
            **state,
            "respuesta": (
                "No pude entender la hora 😅\n"
                "Dime así: _'Recuérdame a las 5pm recoger la mercadería'_"
            ),
            "sub_estado": "",
            "datos_pendientes": {},
        }

    # Guardar en BD
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO recordatorios
                    (negocio_id, tipo, mensaje, fecha_hora, estado)
                VALUES ($1, 'personalizado', $2, $3, 'pendiente')
                """,
                negocio_id,
                mensaje_recordatorio,
                fecha_hora_utc,
            )
    except Exception as e:
        logger.error(f"[Recordatorio] Error guardando: {e}")
        return {
            **state,
            "respuesta": "Tuve un problema guardando el recordatorio 🙏 Intenta de nuevo.",
            "sub_estado": "",
            "datos_pendientes": {},
        }

    hora_fmt = fecha_hora_local.strftime("%I:%M %p").lstrip("0")
    fecha_fmt = ""
    hoy = datetime.datetime.now(tz).date()
    if fecha_hora_local.date() != hoy:
        fecha_fmt = f" el {fecha_hora_local.strftime('%d/%m')}"

    return {
        **state,
        "respuesta": (
            f"Listo {nombre_propio}, te recuerdo{fecha_fmt} a las {hora_fmt} "
            f"que tienes que {mensaje_recordatorio} 🔔"
        ),
        "sub_estado": "",
        "datos_pendientes": {},
    }