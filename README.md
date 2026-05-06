# Chatbot WhatsApp — Comerciantes Tacna 🛍️

MVP para el concurso EIN Tacna 2026 (ProInnóvate).

## Stack
- **FastAPI** + Uvicorn
- **YCloud** — BSP de WhatsApp
- **Gemini 2.5 Flash** — IA conversacional
- **Groq Whisper** — transcripción de voz
- **Supabase** (PostgreSQL + asyncpg)
- **Railway** — hosting

---

## Setup local

### 1. Clonar y entrar al proyecto
```bash
git clone <repo>
cd chatbot-tacna
```

### 2. Crear entorno virtual e instalar dependencias
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configurar variables de entorno
```bash
cp .env.example .env
# Editar .env con tus credenciales reales
```

### 4. Correr migraciones en Supabase
Abre Supabase → SQL Editor → pega y ejecuta `migrations/001_initial_schema.sql`

### 5. Levantar el servidor
```bash
uvicorn app.main:app --reload
```

El servidor corre en http://localhost:8000

---

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Bienvenida |
| GET | `/health` | Estado del servidor |
| GET | `/health/db` | Estado de la base de datos |
| POST | `/webhook` | Recibe eventos de YCloud |

---

## Configurar webhook en YCloud

1. Despliega en Railway y copia tu URL pública, ej: `https://chatbot-tacna.up.railway.app`
2. En el panel de YCloud → WhatsApp → Webhook URL: `https://chatbot-tacna.up.railway.app/webhook`
3. Token: el mismo valor que pusiste en `YCLOUD_WEBHOOK_TOKEN`

---

## Variables de entorno (Railway)

Agregar en Railway → Variables:

```
YCLOUD_API_KEY=...
YCLOUD_WEBHOOK_TOKEN=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
DATABASE_URL=postgresql+asyncpg://...
TZ=America/Lima
```
