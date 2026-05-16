"""
app/routers/webhook.py  (v3 — LangGraph)

Responsabilidad única: recibir eventos de YCloud y llamar al grafo.
Toda la lógica de negocio vive en app/graph/.

El flujo es:
  YCloud POST → _handle_inbound → _process_text → run_graph → send_text
"""

import os
import logging
import uuid
from fastapi import APIRouter, Request, Header, HTTPException, status

from app.config import settings
from app.services import ycloud
from app.services.groq_service import procesar_audio_whatsapp
from app.services.polly_service import polly_service
from app.services.storage_service import upload_audio_to_supabase
from app.graph.graph import run_graph

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _verify_token(token: str | None) -> None:
    if not settings.YCLOUD_WEBHOOK_TOKEN:
        return
    if token != settings.YCLOUD_WEBHOOK_TOKEN:
        logger.warning(f"Token inválido: {token!r}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


@router.post("")
async def receive_event(
    request: Request,
    x_ycloud_webhook_token: str | None = Header(default=None),
):
    _verify_token(x_ycloud_webhook_token)

    body       = await request.json()
    event_type = body.get("type", "")
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
    logger.info(f"[RAW BODY] {body}")
    msg         = body.get("whatsappInboundMessage", {})
    from_number = msg.get("from", "")
    msg_type    = msg.get("type", "")

    logger.info(f"📨 Mensaje de {from_number} | tipo={msg_type}")

    if not from_number:
        logger.warning("Webhook sin 'from' — ignorado")
        return

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()
        logger.info(f"   Texto: {text!r}")
        await _process_text(from_number, text, es_audio=False)

    elif msg_type == "audio":
        await _process_audio(from_number, msg)

    elif msg_type == "image":
        await ycloud.send_text(from_number, "🖼️ Recibí tu imagen.")

    else:
        logger.info(f"   Tipo no manejado: {msg_type}")


async def _process_audio(from_number: str, msg: dict) -> None:
    await ycloud.send_text(from_number, "🎙️ Escuchando tu audio...")

    audio_data = msg.get("audio", {})
    audio_url  = audio_data.get("link", "")
    mime_type  = audio_data.get("mime_type", "audio/ogg")

    if not audio_url:
        await ycloud.send_text(from_number, "No pude acceder al audio. ¿Puedes escribirlo? ✍️")
        return

    texto_transcrito = await procesar_audio_whatsapp(audio_url, mime_type)

    if not texto_transcrito:
        await ycloud.send_text(from_number, "No entendí el audio 😅 ¿Puedes escribirlo o intentar de nuevo?")
        return

    logger.info(f"[Audio→Texto] {from_number}: '{texto_transcrito}'")
    await ycloud.send_text(from_number, f"🎙️ Escuché: «{texto_transcrito}»")
    await _process_text(from_number, texto_transcrito, es_audio=True)


async def _process_text(from_number: str, text: str, es_audio: bool = False) -> None:
    try:
        # ── Todo el procesamiento vive en el grafo ──
        respuesta = await run_graph(
            telefono=from_number,
            mensaje=text,
            es_audio=es_audio,
        )

        await ycloud.send_text(from_number, respuesta)

        if es_audio:
            await _responder_con_voz(from_number, respuesta)

    except Exception as e:
        logger.error(f"[_process_text] Error: {e}", exc_info=True)
        await ycloud.send_text(from_number, "Tuve un error técnico. Intenta de nuevo 🙏")


async def _responder_con_voz(from_number: str, texto: str) -> None:
    try:
        local_path  = await polly_service.text_to_speech(texto)
        if not local_path:
            return
        file_name   = f"voz_{uuid.uuid4()}.mp3"
        url_publica = await upload_audio_to_supabase(local_path, file_name)
        await ycloud.send_audio(from_number, url_publica)
        if os.path.exists(local_path):
            os.remove(local_path)
    except Exception as e:
        logger.error(f"[Voz] Error al generar/enviar audio: {e}")