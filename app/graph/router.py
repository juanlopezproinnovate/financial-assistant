"""
app/graph/router.py

Nodo router: primer nodo que recibe SIEMPRE el mensaje.

Responsabilidades:
1. Cargar negocio y sesión de la BD
2. Si está en onboarding → derivar a nodo onboarding
3. Si tiene sub_estado activo (venta pendiente, edición, etc.)
   → checar si el usuario quiere interrumpir o continuar el flujo
4. Si no → llamar al LLM para detectar intent y derivar al nodo correcto

La clave es que el router SIEMPRE puede interceptar mensajes como
"¿cuánto vendí hoy?" aunque el usuario esté en medio de un flujo de
selección de producto. Si el intent es suficientemente claro y distinto
al sub_estado actual, interrumpe y atiende la nueva solicitud.
"""

import logging
from app.graph.state import QuriState
from app.services.onboarding_service import onboarding_service
from app.services.gemini_service import gemini_service

logger = logging.getLogger(__name__)

# Intents que pueden interrumpir cualquier sub_estado activo
INTENTS_INTERRUPTORES = {
    "REPORTE", "SALUDO", "AYUDA", "CATALOGO",
    "VENTA", "GASTO", "INVENTARIO",  # el usuario siempre puede registrar operaciones
}

# Sub-estados que NO se pueden interrumpir (necesitan respuesta del usuario)
SUB_ESTADOS_BLOQUEANTES = {
    "ESPERANDO_SELECCION_STOCK",
    "ESPERANDO_SELECCION_ELIMINAR",
    "ESPERANDO_SELECCION_EDITAR",
    "ESPERANDO_EDICION_TRANSACCION",
    "ESPERANDO_DECISION_PRODUCTO_NUEVO",
    "ESPERANDO_PRECIO_VENTA",
}


async def router_node(state: QuriState) -> QuriState:
    """
    Nodo router principal. Decide a qué nodo derivar cada mensaje.
    """
    telefono = state["telefono"]
    mensaje  = state["mensaje"]

    # ── 1. Cargar negocio y sesión ──────────────────────────
    negocio = await onboarding_service.get_negocio(telefono)
    sesion  = await onboarding_service.get_sesion(telefono) if negocio else None
    datos_temp = onboarding_service._leer_datos_temp(sesion)

    # ── 2. Negocio nuevo o en onboarding ───────────────────
    if not negocio or not negocio.get("onboarding_completo"):
        logger.info(f"[Router] {telefono} → ONBOARDING")
        return {
            **state,
            "negocio": negocio or {},
            "sesion": sesion or {},
            "siguiente_nodo": "onboarding",
            "datos_pendientes": datos_temp,
        }

    negocio_id = str(negocio["id"])
    historial  = onboarding_service._leer_historial(sesion)
    sub_estado = datos_temp.get("sub_estado", "")

    # ── 3. Hay sub_estado activo → checar si interrumpir ───
    # Limpiar sub_estados que no son válidos (restos del onboarding u otros flujos)
    if sub_estado and sub_estado not in SUB_ESTADOS_BLOQUEANTES:
        logger.info(f"[Router] {telefono} | sub_estado residual '{sub_estado}' ignorado → flujo normal")
        sub_estado = ""

    if sub_estado in SUB_ESTADOS_BLOQUEANTES:
        # Llamar al LLM igualmente para ver si el usuario quiere otra cosa
        contexto = _build_contexto(negocio)
        result = await gemini_service.procesar_mensaje(
            mensaje=mensaje,
            historial=historial,
            contexto_negocio=contexto,
        )
        intent_detectado = result.get("intent", "DESCONOCIDO")

        # Si el intent es un interruptor claro → abandonar sub_estado
        if intent_detectado in INTENTS_INTERRUPTORES:
            logger.info(
                f"[Router] {telefono} | sub_estado={sub_estado} "
                f"INTERRUMPIDO por intent={intent_detectado}"
            )
            return {
                **state,
                "negocio": dict(negocio),
                "negocio_id": negocio_id,
                "sesion": sesion or {},
                "historial": historial,
                "intent": intent_detectado,
                "datos_nlp": result.get("datos", {}),
                "items": result.get("items", []),
                "sub_estado": "",           # limpiar sub_estado
                "datos_pendientes": {},
                "siguiente_nodo": _intent_a_nodo(intent_detectado)
            }

        # Si no → continuar en el sub_estado actual
        logger.info(
            f"[Router] {telefono} | sub_estado={sub_estado} "
            f"continúa (intent={intent_detectado})"
        )
        return {
            **state,
            "negocio": dict(negocio),
            "negocio_id": negocio_id,
            "sesion": sesion or {},
            "historial": historial,
            "intent": intent_detectado,
            "datos_nlp": result.get("datos", {}),
            "items": result.get("items", []),
            "sub_estado": sub_estado,
            "datos_pendientes": datos_temp,
            "siguiente_nodo": "sub_estado_activo"            
        }

    # ── 4. Flujo normal: detectar intent con LLM ───────────
    contexto = _build_contexto(negocio)
    result = await gemini_service.procesar_mensaje(
        mensaje=mensaje,
        historial=historial,
        contexto_negocio=contexto,
    )

    intent   = result.get("intent", "DESCONOCIDO")
    datos    = result.get("datos", {})
    items    = result.get("items", []) 
    nodo_dst = _intent_a_nodo(intent)

    logger.info(f"[Router] {telefono} | intent={intent} → nodo={nodo_dst}")

    return {
        **state,
        "negocio": dict(negocio),
        "negocio_id": negocio_id,
        "sesion": sesion or {},
        "historial": historial,
        "intent": intent,
        "datos_nlp": datos,
        "items": items,    
        "sub_estado": "",
        "datos_pendientes": datos_temp,
        "siguiente_nodo": nodo_dst,
        # La respuesta del LLM para INCOMPLETO/SALUDO/AYUDA ya viene lista
        "respuesta": result.get("respuesta", "") if intent in ("INCOMPLETO", "SALUDO", "AYUDA", "DESCONOCIDO") else "",
    }


# ──────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────

def _build_contexto(negocio: dict) -> dict:
    return {
        "nombre":       negocio.get("nombre_negocio", ""),
        "tipo_ropa":    negocio.get("rubro", ""),
        "zona_horaria": negocio.get("zona_horaria", "America/Lima"),
    }


def _intent_a_nodo(intent: str) -> str:
    """Mapea un intent a su nodo correspondiente en el grafo."""
    mapa = {
        "VENTA":                 "venta",
        "GASTO":                 "gasto",
        "INVENTARIO":            "inventario",
        "REPORTE":               "reporte",
        "ELIMINAR_TRANSACCION":  "eliminar",
        "EDITAR_TRANSACCION":    "editar",
        "INCOMPLETO":            "respuesta_directa",
        "SALUDO":                "respuesta_directa",
        "AYUDA":                 "respuesta_directa",
        "DESCONOCIDO":           "respuesta_directa",
        "CATALOGO":              "catalogo"
    }
    return mapa.get(intent, "respuesta_directa")


def routing_condition(state: QuriState) -> str:
    """
    Función de condición para el grafo LangGraph.
    Retorna el nombre del siguiente nodo basado en state["siguiente_nodo"].
    """
    return state.get("siguiente_nodo", "respuesta_directa")