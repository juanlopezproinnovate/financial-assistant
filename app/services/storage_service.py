import os
import asyncio
from functools import partial
from supabase import create_client, Client
from app.config import settings

import httpx

# Monkeypatch httpx.Client para soportar supabase==2.10.0 con httpx==0.27.2
_original_client_init = httpx.Client.__init__
def _patched_client_init(self, *args, **kwargs):
    if 'proxy' in kwargs:
        kwargs['proxies'] = kwargs.pop('proxy')
    _original_client_init(self, *args, **kwargs)

_original_async_client_init = httpx.AsyncClient.__init__
def _patched_async_client_init(self, *args, **kwargs):
    if 'proxy' in kwargs:
        kwargs['proxies'] = kwargs.pop('proxy')
    _original_async_client_init(self, *args, **kwargs)

httpx.Client.__init__ = _patched_client_init
httpx.AsyncClient.__init__ = _patched_async_client_init

# Inicializamos el cliente
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

async def upload_audio_to_supabase(file_path: str, file_name: str) -> str:
    """
    Sube un archivo a Supabase Storage de forma no bloqueante 
    y devuelve la URL pública.
    """
    loop = asyncio.get_event_loop()
    
    try:
        with open(file_path, 'rb') as f:
            # Ejecutamos la subida síncrona en un hilo separado
            upload_func = partial(
                supabase.storage.from_(settings.SUPABASE_BUCKET).upload,
                path=file_name,
                file=f,
                file_options={"content-type": "audio/mpeg", "x-upsert": "true"}
            )
            await loop.run_in_executor(None, upload_func)
        
        # Obtener URL pública (esta operación suele ser instantánea/string mapping)
        res = supabase.storage.from_(settings.SUPABASE_BUCKET).get_public_url(file_name)
        
        # En versiones recientes de la librería, res puede ser un string directo 
        # o un objeto con la propiedad public_url. Ajustamos por seguridad:
        if isinstance(res, dict):
            return res.get("publicURL", "")
        return str(res)
        
    except Exception as e:
        print(f"Error subiendo a Supabase: {e}")
        raise e