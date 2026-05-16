"""
app/graph/graph.py

Arma el grafo LangGraph completo y expone run_graph(),
que es el único punto de entrada desde webhook.py.

Persistencia:
  - El sub_estado y datos_pendientes se guardan en sesiones.datos_temporales
    después de cada turno, igual que antes pero ahora lo gestiona el grafo.
  - El historial de corto plazo también se persiste aquí.
"""

import json
import logging
from langgraph.graph import StateGraph, END

from app.graph.state import QuriState
from app.graph.router import router_node, routing_condition
from app.graph.nodes.negocio import (
    venta_node,
    gasto_node,
    inventario_node,
    reporte_node,
    eliminar_node,
    editar_node,
    respuesta_directa_node,
    sub_estado_activo_node,
)
from app.services.onboarding_service import onboarding_service
from app.graph.nodes.catalogo_node import catalogo_node

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
#  Construcción del grafo
# ──────────────────────────────────────────────────────────

async def onboarding_node(state: QuriState) -> QuriState:
    """
    Wrapper que llama al onboarding_service.procesar() existente sin modificarlo.
    Así el onboarding sigue funcionando exactamente igual que antes.
    """
    telefono = state["telefono"]
    mensaje  = state["mensaje"]
    respuesta = await onboarding_service.procesar(telefono, mensaje)
    return {**state, "respuesta": respuesta}

def _build_graph() -> StateGraph:
    g = StateGraph(QuriState)

    # Nodos
    g.add_node("router",           router_node)
    g.add_node("venta",            venta_node)
    g.add_node("gasto",            gasto_node)
    g.add_node("inventario",       inventario_node)
    g.add_node("reporte",          reporte_node)
    g.add_node("eliminar",         eliminar_node)
    g.add_node("editar",           editar_node)
    g.add_node("respuesta_directa",respuesta_directa_node)
    g.add_node("sub_estado_activo",sub_estado_activo_node)
    # Nodo onboarding: usa el servicio existente sin modificarlo
    g.add_node("onboarding",       onboarding_node)
    g.add_node("catalogo", catalogo_node)

    # Entry point
    g.set_entry_point("router")

    # Router decide a dónde ir
    g.add_conditional_edges(
        "router",
        routing_condition,
        {
            "venta":             "venta",
            "gasto":             "gasto",
            "inventario":        "inventario",
            "reporte":           "reporte",
            "eliminar":          "eliminar",
            "editar":            "editar",
            "respuesta_directa": "respuesta_directa",
            "sub_estado_activo": "sub_estado_activo",
            "onboarding":        "onboarding",
            "catalogo":          "catalogo"
        },
    )

    # Todos los nodos terminan en END (el webhook envía la respuesta)
    for nodo in [
        "venta", "gasto", "inventario", "reporte",
        "eliminar", "editar", "respuesta_directa",
        "sub_estado_activo", "onboarding", "catalogo",
    ]:
        g.add_edge(nodo, END)

    return g.compile()


# Instancia compilada (singleton)
_graph = _build_graph()


# ──────────────────────────────────────────────────────────
#  Nodo onboarding: wrapper del servicio existente
# ──────────────────────────────────────────────────────────




# ──────────────────────────────────────────────────────────
#  Punto de entrada principal
# ──────────────────────────────────────────────────────────

async def run_graph(telefono: str, mensaje: str, es_audio: bool = False) -> str:
    """
    Ejecuta el grafo para un mensaje entrante.
    Retorna el texto de respuesta a enviar por WhatsApp.

    Internamente:
    1. Construye el estado inicial
    2. Carga sub_estado y datos_pendientes desde la BD (via router)
    3. Ejecuta el grafo
    4. Persiste el nuevo sub_estado y historial
    5. Retorna la respuesta
    """
    estado_inicial: QuriState = {
        "telefono": telefono,
        "mensaje":  mensaje,
        "es_audio": es_audio,
        # El resto lo carga el router desde la BD
    }

    try:
        resultado = await _graph.ainvoke(estado_inicial)
    except Exception as e:
        logger.error(f"[Graph] Error ejecutando grafo para {telefono}: {e}", exc_info=True)
        return "Tuve un error técnico. Intenta de nuevo 🙏"

    respuesta  = resultado.get("respuesta") or "¿En qué te ayudo? 😊"
    negocio_id = resultado.get("negocio_id")

    # ── Persistir estado para el próximo turno ──
    if negocio_id:
        await _persistir_estado(resultado, negocio_id, mensaje, respuesta)

    return respuesta


# ──────────────────────────────────────────────────────────
#  Persistencia
# ──────────────────────────────────────────────────────────

async def _persistir_estado(
    resultado: QuriState,
    negocio_id: str,
    mensaje_usuario: str,
    respuesta_bot: str,
) -> None:
    """
    Guarda en sesiones.datos_temporales:
    - sub_estado actual
    - datos_pendientes
    - historial de corto plazo (últimos 3 turnos)
    """
    try:
        sub_estado       = resultado.get("sub_estado", "")
        datos_pendientes = resultado.get("datos_pendientes", {}) or {}
        historial        = resultado.get("historial", []) or []

        # Actualizar historial (solo para flujos activos, no sub_estados intermedios)
        if sub_estado not in ("INCOMPLETO",) and mensaje_usuario and respuesta_bot:
            historial.append({"role": "user",      "content": mensaje_usuario})
            historial.append({"role": "assistant",  "content": respuesta_bot})
            # Mantener solo los últimos 3 turnos (6 mensajes)
            if len(historial) > 6:
                historial = historial[-6:]
        elif sub_estado == "INCOMPLETO":
            # Para INCOMPLETO sí guardar, el siguiente mensaje necesita contexto
            historial.append({"role": "user",      "content": mensaje_usuario})
            historial.append({"role": "assistant",  "content": respuesta_bot})
            if len(historial) > 6:
                historial = historial[-6:]

        datos_temp = {
            **datos_pendientes,
            "sub_estado":        sub_estado,
            "historial_mensajes": historial,
        }

        # El estado de la sesión refleja el sub_estado o "activo"
        estado_sesion = sub_estado if sub_estado else "activo"

        await onboarding_service.upsert_sesion(negocio_id, estado_sesion, datos_temp)

    except Exception as e:
        logger.error(f"[Graph] Error persistiendo estado para {negocio_id}: {e}")