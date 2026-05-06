"""
Endpoints de salud — Railway y tú usarán estos para verificar que todo está vivo.
"""
import logging
from fastapi import APIRouter
from app.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/")
async def root():
    return {"message": "Chatbot Tacna 🛍️ — API activa"}


@router.get("/health")
async def health():
    """Health check básico."""
    return {"status": "ok"}


@router.get("/health/db")
async def health_db():
    """Verifica que la base de datos responde."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "conectada", "result": result}
    except Exception as e:
        logger.error(f"DB health check falló: {e}")
        return {"status": "error", "detail": str(e)}
