"""
Router del webhook de YCloud.

Estructura real del payload (doc oficial YCloud v2):
{
  "id": "evt_xxx",
  "type": "whatsapp.inbound_message.received",
  "apiVersion": "v2",
  "createTime": "2024-01-01T12:00:00.000Z",
  "inboundMessage": {
    "id": "...",
    "from": "51912345678",       <- número del cliente
    "to": "51987654321",         <- tu número de negocio
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


async def _handle_inbound(body: dict) -> None:
    """
    Procesa mensajes entrantes.
    Clave correcta según doc YCloud: 'inboundMessage' (NO 'whatsapp.message')
    """
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
        # TODO: transcribir con Groq Whisper
        await ycloud.send_text(from_number, "🎙️ Recibí tu audio, pronto lo proceso.")

    elif msg_type == "image":
        await ycloud.send_text(from_number, "🖼️ Recibí tu imagen.")

    else:
        logger.info(f"   Tipo no manejado aún: {msg_type}")


async def _process_text(from_number: str, text: str) -> None:
    """Respuesta de prueba — aquí se conectará Gemini más adelante."""
    await ycloud.send_text(
        from_number,
        f"✅ Recibí: «{text}»\n\n¡El bot está vivo! 🛍️ Pronto te ayudaré con tu negocio."
    )
