"""
Configuración centralizada. Lee variables desde .env (o Railway env vars).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # YCloud
    YCLOUD_API_KEY: str = os.getenv("YCLOUD_API_KEY", "5b8cb6df4a9d5564d9a4025666b48816")
    YCLOUD_WEBHOOK_TOKEN: str = os.getenv("YCLOUD_WEBHOOK_TOKEN", "")
    YCLOUD_PHONE: str = os.getenv("YCLOUD_PHONE", "+51931784951")   # tu número en formato E.164, ej: +51912345678
    YCLOUD_API_BASE: str = "https://api.ycloud.com/v2"

    # Gemini
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Groq
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # Base de datos
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # General
    TZ: str = os.getenv("TZ", "America/Lima")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


settings = Settings()
