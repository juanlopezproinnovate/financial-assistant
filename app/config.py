"""
Configuración centralizada. Lee variables desde .env (o Railway env vars).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:

    AWS_ACCESS_KEY_ID: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION_NAME: str = os.getenv("AWS_REGION_NAME", "us-east-1")

    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "") # ej: https://xyz.supabase.co
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "") # Service Role Key (importante para subir archivos)
    SUPABASE_BUCKET: str = "audios-bot"

    # YCloud
    YCLOUD_API_KEY: str = os.getenv("YCLOUD_API_KEY", "5b8cb6df4a9d5564d9a4025666b48816")
    YCLOUD_WEBHOOK_TOKEN: str = os.getenv("YCLOUD_WEBHOOK_TOKEN", "")
    YCLOUD_PHONE: str = os.getenv("YCLOUD_PHONE", "+51931784951")   # tu número en formato E.164, ej: +51912345678
    YCLOUD_API_BASE: str = "https://api.ycloud.com/v2"

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Groq
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # Base de datos
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.edxtetiaubdbjptnoxxz.supabase.co:5432/postgres")

    # General
    TZ: str = os.getenv("TZ", "America/Lima")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


settings = Settings()
