"""
app/services/groq_service.py
Transcripción de audios WhatsApp con Groq Whisper large-v3-turbo
Gratuito: 2,000 RPD / 7,200 segundos por hora
"""

import logging
import httpx
from groq import AsyncGroq
from app.config import settings

logger = logging.getLogger(__name__)

client = AsyncGroq(api_key=settings.GROQ_API_KEY)


async def descargar_audio(url: str) -> bytes:
    """
    Descarga el audio desde la URL de YCloud.
    YCloud sirve el audio temporalmente, hay que descargarlo rápido.
    """
    headers = {"X-API-Key": settings.YCLOUD_API_KEY}
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url, headers=headers)
        response.raise_for_status()
        return response.content


async def transcribir_audio(audio_bytes: bytes, formato: str = "ogg") -> str:
    """
    Transcribe audio con Groq Whisper large-v3-turbo.

    Args:
        audio_bytes: bytes del archivo de audio
        formato: extensión del archivo (ogg, mp4, webm, wav, mp3)

    Returns:
        Texto transcrito o string vacío si falla
    """
    try:
        # Groq espera un file-like object con nombre
        transcription = await client.audio.transcriptions.create(
            file=(f"audio.{formato}", audio_bytes),
            model="whisper-large-v3-turbo",
            language="es",          # forzar español
            response_format="text", # solo el texto, sin metadata
        )
        texto = transcription.strip()
        logger.info(f"[Groq] Transcripción: '{texto[:100]}'")
        return texto

    except Exception as e:
        logger.error(f"[Groq] Error transcribiendo: {e}")
        return ""


async def procesar_audio_whatsapp(audio_url: str, mime_type: str = "audio/ogg") -> str:
    """
    Pipeline completo: descarga → transcribe → retorna texto.

    Args:
        audio_url: URL del audio desde YCloud
        mime_type: tipo MIME del audio de WhatsApp

    Returns:
        Texto transcrito o "" si falla
    """
    # Mapear mime_type a extensión
    extensiones = {
        "audio/ogg": "ogg",
        "audio/ogg; codecs=opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp4": "mp4",
        "audio/webm": "webm",
        "audio/wav": "wav",
    }
    formato = extensiones.get(mime_type, "ogg")

    try:
        logger.info(f"[Groq] Descargando audio: {audio_url[:60]}...")
        audio_bytes = await descargar_audio(audio_url)
        logger.info(f"[Groq] Audio descargado: {len(audio_bytes)} bytes")

        texto = await transcribir_audio(audio_bytes, formato)
        return texto

    except Exception as e:
        logger.error(f"[Groq] Pipeline falló: {e}")
        return ""