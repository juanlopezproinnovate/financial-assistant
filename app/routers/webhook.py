"""
app/routers/webhook.py
"""
import logging
from fastapi import APIRouter, Request, Header, HTTPException, status
from app.config import settings
from app.services import ycloud
from app.services.gemini_service import gemini_service
from app.services.onboarding_service import onboarding_service
from app.services.groq_service import procesar_audio_whatsapp
from app.database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_token(token: str | None) -> None:
    if not settings.YCLOUD_WEBHOOK_TOKEN:
        return
    if token != settings.YCLOUD_WEBHOOK_TOKEN:
        logger.warning(f"Token inválido recibido: {token!r}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


@router.post("")
async def receive_event(
    request: Request,
    x_ycloud_webhook_token: str | None = Header(default=None),
):
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


async def _handle_inbound(body: dict) -> None:

    logger.info(f"[RAW BODY*] {body}")
    msg: dict = body.get("whatsappInboundMessage", {})
    from_number: str = msg.get("from", "")
    msg_type: str = msg.get("type", "")

    logger.info(f"📨 Mensaje de {from_number} | tipo={msg_type}")
    logger.info(f"[RAW BODY_] {body}")

    if not from_number:
        logger.warning("Webhook sin 'from' — ignorado")
        return

    if msg_type == "text":
        text: str = msg.get("text", {}).get("body", "").strip()
        logger.info(f"   Texto: {text!r}")
        await _process_text(from_number, text)

    elif msg_type == "audio":
        await _process_audio(from_number, msg)

    elif msg_type == "image":
        await ycloud.send_text(from_number, "🖼️ Recibí tu imagen.")

    else:
        logger.info(f"   Tipo no manejado aún: {msg_type}")


async def _process_audio(from_number: str, msg: dict) -> None:
    """
    Pipeline de audio:
    1. Avisa que está procesando
    2. Descarga y transcribe con Groq
    3. Procesa el texto como si lo hubiera escrito
    """
    await ycloud.send_text(from_number, "🎙️ Escuchando tu audio...")

    # LOG: ver todo el contenido del msg
    logger.info(f"[Audio] msg completo: {msg}")

    audio_data = msg.get("audio", {})
    logger.info(f"[Audio] audio_data: {audio_data}")

    audio_url  = audio_data.get("link", "")
    mime_type  = audio_data.get("mime_type", "audio/ogg")

    logger.info(f"[Audio] url='{audio_url}' | mime='{mime_type}'")

    if not audio_url:
        await ycloud.send_text(from_number, "No pude acceder al audio. ¿Puedes escribirlo? ✍️")
        return

    texto_transcrito = await procesar_audio_whatsapp(audio_url, mime_type)

    if not texto_transcrito:
        await ycloud.send_text(
            from_number,
            "No entendí el audio 😅 ¿Puedes escribirlo o intentar de nuevo?"
        )
        return

    logger.info(f"[Audio→Texto] {from_number}: '{texto_transcrito}'")

    # Confirmar transcripción al usuario
    await ycloud.send_text(from_number, f"🎙️ Escuché: «{texto_transcrito}»")

    # Procesar igual que texto normal
    await _process_text(from_number, texto_transcrito)


async def _process_text(from_number: str, text: str) -> None:
    try:
        negocio = await onboarding_service.get_negocio(from_number)
        en_onboarding = (
            not negocio
            or not negocio.get("onboarding_completo")
        )

        # ── FLUJO A: Onboarding ──
        if en_onboarding:
            respuesta = await onboarding_service.procesar(from_number, text)
            await ycloud.send_text(from_number, respuesta)
            return

        # ── FLUJO B: NLP activo ──
        contexto = {
            "nombre":    negocio.get("nombre_negocio", ""),
            "tipo_ropa": negocio.get("rubro", ""),
        }
        result = await gemini_service.procesar_mensaje(
            mensaje=text,
            contexto_negocio=contexto,
        )

        intent    = result.get("intent", "DESCONOCIDO")
        datos     = result.get("datos", {})
        respuesta = result.get("respuesta", "¿En qué te ayudo? 😊")

        logger.info(f"[NLP] intent={intent} | datos={datos}")

        if intent == "VENTA" and datos.get("total"):
            await _guardar_transaccion(
                negocio_id  = str(negocio["id"]),
                tipo        = "venta",
                descripcion = datos.get("producto", "venta"),
                monto       = datos.get("total", 0),
                moneda      = datos.get("moneda", "PEN"),
            )

        elif intent == "GASTO" and datos.get("monto"):
            await _guardar_transaccion(
                negocio_id  = str(negocio["id"]),
                tipo        = "gasto",
                descripcion = datos.get("concepto", "gasto"),
                monto       = datos.get("monto", 0),
                moneda      = datos.get("moneda", "PEN"),
            )

        elif intent == "REPORTE":
            datos_reporte = await _obtener_reporte(str(negocio["id"]), datos.get("periodo", "hoy"))
            respuesta = await gemini_service.generar_resumen_reporte(datos_reporte)

        await ycloud.send_text(from_number, respuesta)

    except Exception as e:
        logger.error(f"[_process_text] Error: {e}", exc_info=True)
        await ycloud.send_text(from_number, "Tuve un error técnico. Intenta de nuevo 🙏")


async def _guardar_transaccion(
    negocio_id: str,
    tipo: str,
    descripcion: str,
    monto: float,
    moneda: str = "PEN",
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO transacciones
                (negocio_id, tipo, descripcion, monto, moneda, fecha, origen_registro)
            VALUES ($1, $2, $3, $4, $5, CURRENT_DATE, 'whatsapp')
            """,
            negocio_id, tipo, descripcion, float(monto), moneda,
        )
    logger.info(f"💾 {tipo.upper()}: {descripcion} | {moneda} {monto}")


async def _obtener_reporte(negocio_id: str, periodo: str) -> dict:
    filtros = {
        "hoy":   "fecha = CURRENT_DATE",
        "ayer":  "fecha = CURRENT_DATE - 1",
        "semana":"fecha >= CURRENT_DATE - 7",
        "mes":   "fecha >= CURRENT_DATE - 30",
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