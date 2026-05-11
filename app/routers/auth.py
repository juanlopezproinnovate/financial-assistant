from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
import random
import logging
from app.services import ycloud
from app.database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

class OtpRequest(BaseModel):
    phone: str

class OtpVerify(BaseModel):
    phone: str
    code: str

# Diccionario temporal para guardar códigos (En producción usar Redis o DB)
# Por ahora lo guardaremos en memoria para que funcione tu MVP
otp_store = {}

@router.post("/request-otp")
async def request_otp(payload: OtpRequest):
    phone = payload.phone
    
    # 1. Generar código aleatorio de 6 dígitos
    code = f"{random.randint(100000, 999999)}"
    otp_store[phone] = code
    
    logger.info(f"🔑 OTP generado para {phone}: {code}")
    
    try:
        # 2. Enviar por WhatsApp usando YCloud
        mensaje = f"Tu código de acceso para el Financial Assistant es: {code}. No lo compartas con nadie. 🛡️"
        await ycloud.send_text(phone, mensaje)
        
        return {"success": True, "message": "Código enviado correctamente"}
    except Exception as e:
        logger.error(f"Error al enviar OTP: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo enviar el código por WhatsApp"
        )

@router.post("/verify-otp")
async def verify_otp(payload: OtpVerify):
    phone = payload.phone
    code = payload.code
    
    # 1. Validar código
    if phone in otp_store and otp_store[phone] == code:
        # Eliminar código usado
        del otp_store[phone]
        
        # 2. Buscar datos del negocio para devolverlos al dashboard
        pool = await get_pool()
        async with pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT id, nombre_negocio, whatsapp_numero FROM negocios WHERE whatsapp_numero = $1",
                phone
            )
            
        if not user:
             raise HTTPException(status_code=404, detail="Usuario no encontrado después de verificar")

        return {
            "success": True,
            "token": "fake-jwt-token", # Aquí podrías generar un JWT real
            "user": {
                "id": str(user["id"]),
                "name": user["nombre_negocio"],
                "phone": user["whatsapp_numero"]
            }
        }
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Código de verificación incorrecto"
    )
