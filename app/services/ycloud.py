"""
Servicio YCloud — envía mensajes de WhatsApp usando la API REST de YCloud.
Docs: https://docs.ycloud.com/reference/whatsapp-message-sending-guide

Notas importantes:
- "from" es OBLIGATORIO: tu número de negocio registrado en YCloud (E.164)
- Header de autenticación: X-API-Key
- Endpoint asíncrono (recomendado): POST /v2/whatsapp/messages
- Endpoint síncrono (OTP/urgente):  POST /v2/whatsapp/messages/sendDirectly
"""
import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)


def _headers() -> dict:
    """Headers frescos para cada request (evita problema si la key cambia en runtime)."""
    return {
        "Content-Type": "application/json",
        "X-API-Key": settings.YCLOUD_API_KEY,
    }


async def send_audio(to: str, audio_url: str):
    payload = {
        "from": settings.YCLOUD_PHONE,
        "to": to,
        "type": "audio",
        "audio": {"link": audio_url}
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.YCLOUD_API_BASE}/whatsapp/messages",
            headers=_headers(),
            json=payload,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"YCloud error {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    logger.info(f"✉️  Mensaje de voz enviado a {to} | id={data.get('id')}")
    return data

async def send_text(to: str, text: str) -> dict:
    """
    Envía un mensaje de texto plano a un número de WhatsApp.

    Args:
        to:   Número destino en formato E.164, ej: "+51912345678"
        text: Texto a enviar (máx. 4096 caracteres)

    Returns:
        Respuesta JSON de YCloud con el id del mensaje
    """
    payload = {
        "from": settings.YCLOUD_PHONE,   # OBLIGATORIO según la doc de YCloud
        "to": to,
        "type": "text",
        "text": {"body": text},
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.YCLOUD_API_BASE}/whatsapp/messages",
            headers=_headers(),
            json=payload,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"YCloud error {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    logger.info(f"✉️  Mensaje enviado a {to} | id={data.get('id')}")
    return data


async def send_template(to: str, template_name: str, language: str = "es", components: list | None = None) -> dict:
    """
    Envía un mensaje con plantilla aprobada por Meta.
    Necesario para contactar usuarios fuera de la ventana de 24h.
    """
    payload = {
        "from": settings.YCLOUD_PHONE,
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components or [],
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.YCLOUD_API_BASE}/whatsapp/messages",
            headers=_headers(),
            json=payload,
        )

    if resp.status_code not in (200, 201):
        logger.error(f"YCloud template error {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    data = resp.json()
    logger.info(f"📋 Template '{template_name}' enviado a {to} | id={data.get('id')}")
    return data
