"""
Punto de entrada principal de FastAPI.
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import connect_db, disconnect_db
from app.routers import health, webhook, auth
from fastapi.middleware.cors import CORSMiddleware

# Logging básico — en Railway los logs aparecen en la consola
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


from app.services.scheduler_service import scheduler_service

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Se ejecuta al iniciar y al apagar la app."""
    logger.info("🚀 Iniciando Chatbot Tacna…")
    await connect_db()
    
    # Iniciar scheduler de tareas en segundo plano
    scheduler_service.start()
    
    yield
    
    scheduler_service.stop()
    await disconnect_db()
    logger.info("🛑 Chatbot Tacna detenido.")


app = FastAPI(
    title="Chatbot WhatsApp — Comerciantes Tacna",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
   CORSMiddleware,
   allow_origins=["*"],
   allow_credentials=True,
   allow_methods=["*"],
   allow_headers=["*"],
)

# Routers
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(auth.router)