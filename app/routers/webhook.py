"""
Router del webhook de YCloud — Día 2: NLP + Onboarding

Estructura real del payload YCloud v2:
{
  "type": "whatsapp.inbound_message.received",
  "whatsappInboundMessage": {
    "from": "51912345678",
    "type": "text",
    "text": { "body": "Hola" }
  }
}

YCloud reintenta hasta 7 veces si no recibe 200: 10s,30s,5m,30m,1h,2h,2h
"""
import logging
from fastapi import APIRouter, Request, Header, HTTPException, status
from app.config import settings
from app.services import ycloud
from app.services.gemini_service import gemini_service
from app.services.onboarding_service import onboarding_service
from app.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ──────────────────────────────────────────────
#  SEGURIDAD
# ──────────────────────────────────────────────

def _verify_token(token: str | None) -> None:
    if not settings.YCLOUD_WEBHOOK_TOKEN:
        return
    if token != settings.YCLOUD_WEBHOOK_TOKEN:
        logger.warning(f"Token inválido recibido: {token!r}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


# ──────────────────────────────────────────────
#  ENDPOINT PRINCIPAL
# ──────────────────────────────────────────────

@router.post("")
async def receive_event(
    request: Request,
    x_ycloud_webhook_token: str | None = Header(default=None),
):
    """Endpoint principal — YCloud siempre espera HTTP 200."""
    _verify_token(x_ycloud_webhook_token)

    body = await request.json()
    event_type: str = body.get("type", "")
    logger.info(f"📥 Evento YCloud: {event_type}")

    if event_type == "whatsapp.inbound_message.received":
        await _handle_inbound(body)

    elif event_type == "whatsapp.message.updated":
        wa_msg = body.get("whatsappMessage", {})
        logger.info(f"📊 Estado | id={wa_msg.get('id')} | status={wa_msg.get('status')}")

    else:
        logger.debug(f"Evento ignorado: {event_type}")

    return {"status": "ok"}


# ──────────────────────────────────────────────
#  HANDLER DE MENSAJES ENTRANTES
# ──────────────────────────────────────────────

async def _handle_inbound(body: dict) -> None:
    msg: dict = body.get("whatsappInboundMessage", {})
    from_number: str = msg.get("from", "")
    msg_type: str = msg.get("type", "")

    logger.info(f"📨 Mensaje de {from_number} | tipo={msg_type}")

    if not from_number:
        logger.warning("Webhook sin 'from' — ignorado")
        return

    if msg_type == "text":
        text: str = msg.get("text", {}).get("body", "").strip()
        logger.info(f"   Texto: {text!r}")
        await _process_text(from_number, text)

    elif msg_type == "audio":
        audio_url: str = msg.get("audio", {}).get("url", "")
        logger.info(f"   Audio URL: {audio_url}")
        # TODO Día 3: transcribir con Groq Whisper
        await ycloud.send_text(from_number, "🎙️ Recibí tu audio, pronto lo proceso.")

    elif msg_type == "image":
        await ycloud.send_text(from_number, "🖼️ Recibí tu imagen.")

    else:
        logger.info(f"   Tipo no manejado aún: {msg_type}")


# ──────────────────────────────────────────────
#  LÓGICA PRINCIPAL — NLP + ONBOARDING
# ──────────────────────────────────────────────

async def _process_text(from_number: str, text: str) -> None:
    """
    Flujo principal:
    1. ¿Usuario nuevo o en onboarding? → flujo onboarding
    2. ¿Usuario activo? → NLP con Gemini → guardar en BD → responder
    """
    try:
        negocio = await onboarding_service.get_negocio(from_number)
        sesion  = await onboarding_service.get_sesion(from_number)

        en_onboarding = (
            not negocio
            or not negocio.get("onboarding_completo")
            or (sesion and sesion.get("estado", "").startswith("onboarding"))
        )

        # ── FLUJO A: Onboarding ──
        if en_onboarding:
            respuesta = await onboarding_service.procesar(from_number, text)
            await ycloud.send_text(from_number, respuesta)
            return

        # ── FLUJO B: NLP activo ──
        contexto = {
            "nombre":    negocio.get("nombre", ""),
            "tipo_ropa": negocio.get("tipo_ropa", ""),
        }
        result = await gemini_service.procesar_mensaje(
            mensaje=text,
            contexto_negocio=contexto,
        )

        intent    = result.get("intent", "DESCONOCIDO")
        datos     = result.get("datos", {})
        respuesta = result.get("respuesta", "¿En qué te ayudo? 😊")

        logger.info(f"[NLP] intent={intent} | datos={datos}")

        # ── Persistir según intent ──
        if intent == "VENTA" and datos.get("total"):
            await _guardar_transaccion(
                telefono  = from_number,
                tipo      = "venta",
                concepto  = datos.get("producto", "venta"),
                monto     = datos.get("total", 0),
                cantidad  = datos.get("cantidad", 1),
                moneda    = datos.get("moneda", "PEN"),
            )

        elif intent == "GASTO" and datos.get("monto"):
            await _guardar_transaccion(
                telefono  = from_number,
                tipo      = "gasto",
                concepto  = datos.get("concepto", "gasto"),
                monto     = datos.get("monto", 0),
                moneda    = datos.get("moneda", "PEN"),
                categoria = datos.get("categoria", "otros"),
            )

        elif intent == "REPORTE":
            datos_reporte = await _obtener_reporte(from_number, datos.get("periodo", "hoy"))
            respuesta = await gemini_service.generar_resumen_reporte(datos_reporte)

        await ycloud.send_text(from_number, respuesta)

    except Exception as e:
        logger.error(f"[_process_text] Error: {e}", exc_info=True)
        await ycloud.send_text(from_number, "Tuve un error técnico. Intenta de nuevo 🙏")


# ──────────────────────────────────────────────
#  HELPERS DE BD
# ──────────────────────────────────────────────

async def _guardar_transaccion(
    telefono: str,
    tipo: str,
    concepto: str,
    monto: float,
    cantidad: int = 1,
    moneda: str = "PEN",
    categoria: str = "otros",
) -> None:
    precio_unitario = monto / cantidad if cantidad > 0 else monto
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO transacciones
                (telefono, tipo, concepto, monto, cantidad, precio_unitario, moneda, categoria, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            """,
            telefono, tipo, concepto,
            float(monto), int(cantidad), float(precio_unitario),
            moneda, categoria,
        )
    logger.info(f"💾 {tipo.upper()} guardada: {concepto} | S/{monto}")


async def _obtener_reporte(telefono: str, periodo: str) -> dict:
    filtros = {
        "hoy":   "DATE(created_at AT TIME ZONE 'America/Lima') = CURRENT_DATE",
        "ayer":  "DATE(created_at AT TIME ZONE 'America/Lima') = CURRENT_DATE - 1",
        "semana":"created_at >= NOW() - INTERVAL '7 days'",
        "mes":   "created_at >= NOW() - INTERVAL '30 days'",
    }
    where = filtros.get(periodo, filtros["hoy"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'venta'), 0) AS total_ventas,
                COALESCE(SUM(monto) FILTER (WHERE tipo = 'gasto'), 0) AS total_gastos,
                COUNT(*)           FILTER (WHERE tipo = 'venta')       AS num_ventas
            FROM transacciones
            WHERE telefono = $1 AND {where}
            """,
            telefono,
        )

    tv = float(row["total_ventas"])
    tg = float(row["total_gastos"])
    return {
        "periodo":          periodo,
        "total_ventas":     tv,
        "total_gastos":     tg,
        "num_transacciones": int(row["num_ventas"]),
        "ganancia_neta":    tv - tg,
    }