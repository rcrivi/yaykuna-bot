"""
Servidor FastAPI — punto de entrada del bot Yaykuna.
Maneja:
  - GET  /webhook  → verificación de Meta
  - POST /webhook  → mensajes entrantes de WhatsApp
  - GET  /health   → health check de Railway
"""
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()

from .bot import procesar_mensaje
from .whatsapp import enviar_mensaje, marcar_leido, verificar_firma, extraer_mensaje
from .api_client import registrar_mensaje, bajo_control_humano, get_flujo_config

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "yaykuna_bot_2026")

# IDs de mensajes ya procesados (evitar duplicados)
_procesados: set[str] = set()


# Cache de config de flujo (se refresca cada 5 min)
_flujo_config: dict = {"flujo_activo": 1, "flujo_followup_reserva": 7, "flujo_followup_pedido": 3}
_flujo_task: asyncio.Task | None = None


async def _loop_followup():
    """Revisa cada 60s las sesiones inactivas y envía follow-up si corresponde."""
    global _flujo_config
    from .bot import _sesiones
    from datetime import datetime, timedelta

    config_actualizado = None

    while True:
        await asyncio.sleep(60)
        try:
            # Refrescar config cada 5 minutos
            ahora = datetime.utcnow()
            if config_actualizado is None or (ahora - config_actualizado).seconds > 300:
                _flujo_config = await get_flujo_config()
                config_actualizado = ahora

            if not _flujo_config.get("flujo_activo", 1):
                continue  # Follow-up desactivado desde el panel

            min_reserva = int(_flujo_config.get("flujo_followup_reserva", 7))
            min_pedido  = int(_flujo_config.get("flujo_followup_pedido",  3))

            for wa_id, sesion in list(_sesiones.items()):
                msgs = sesion.get("messages", [])
                if not msgs:
                    continue

                ultimo = msgs[-1]
                # Solo actuar si el último mensaje fue del bot (cliente no respondió)
                if ultimo.get("role") != "assistant":
                    continue

                # Ya se envió follow-up para esta sesión
                if sesion.get("followup_enviado"):
                    continue

                inactivo_min = (datetime.utcnow() - sesion["updated"]).seconds // 60

                # Detectar si hay reserva o pedido en curso
                historial = " ".join(
                    (m.get("content") or "") if isinstance(m.get("content"), str)
                    else " ".join(b.get("text","") for b in (m.get("content") or []) if isinstance(b, dict))
                    for m in msgs
                )
                es_pedido  = any(w in historial.lower() for w in ["pedido","llevar","takeaway","carrito","items","plato"])
                es_reserva = any(w in historial.lower() for w in ["reserva","mesa","personas","fecha","hora","sector"])

                if not (es_pedido or es_reserva):
                    continue

                limite = min_pedido if es_pedido else min_reserva

                if inactivo_min >= limite:
                    tipo = "pedido" if es_pedido else "reserva"
                    msg_followup = (
                        f"¿Sigues por aquí? 😊 Cuando quieras continuamos con tu {tipo}."
                    )
                    try:
                        ok = await enviar_mensaje(wa_id, msg_followup)
                        if ok:
                            sesion["followup_enviado"] = True
                            await registrar_mensaje(wa_id, sesion.get("nombre",""), "saliente", msg_followup, origen="bot")
                            print(f"[Flujo] ⏰ Follow-up enviado a {wa_id} ({tipo}, {inactivo_min} min inactivo)")
                    except Exception as e:
                        print(f"[Flujo] ❌ Error enviando follow-up a {wa_id}: {e}")

        except Exception as e:
            print(f"[Flujo] ❌ Error en loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _flujo_task
    print(f"[Yaykuna Bot] 🍽️  Iniciando servidor...")
    _flujo_task = asyncio.create_task(_loop_followup())
    yield
    if _flujo_task:
        _flujo_task.cancel()
    print(f"[Yaykuna Bot] Apagando servidor.")


app = FastAPI(
    title       = "Yaykuna WhatsApp Bot",
    description = "Agente de reservas y atención al cliente para Yaykuna",
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = None,  # Desactivar docs en producción
    redoc_url   = None,
)


# ── Servir el panel de administración ────────────────────────
panel_path = os.path.join(os.path.dirname(__file__), "..", "panel")
if os.path.isdir(panel_path):
    app.mount("/panel", StaticFiles(directory=panel_path, html=True), name="panel")


# ── Health check ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "bot": "Yaykuna Bot", "version": "1.0.0"}


# ── Verificación del webhook (Meta lo llama al configurar) ────
@app.get("/webhook")
async def verificar_webhook(request: Request):
    params     = dict(request.query_params)
    mode       = params.get("hub.mode")
    token      = params.get("hub.verify_token")
    challenge  = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print(f"[Webhook] ✅ Verificación exitosa")
        return PlainTextResponse(challenge)

    print(f"[Webhook] ❌ Token inválido: {token}")
    raise HTTPException(status_code=403, detail="Token inválido")


BOT_SECRET = os.getenv("BOT_SECRET", "")


# ── Envío de mensaje manual (llamado por el panel PHP) ────────
@app.post("/send-message")
async def send_message(request: Request):
    """Permite al panel enviar mensajes manuales a través del bot."""
    secret = request.headers.get("X-Bot-Secret", "")
    if BOT_SECRET and secret != BOT_SECRET:
        raise HTTPException(status_code=403, detail="Secreto inválido")

    body    = await request.json()
    wa_id   = body.get("wa_id", "").strip()
    mensaje = body.get("mensaje", "").strip()

    if not wa_id or not mensaje:
        raise HTTPException(status_code=400, detail="wa_id y mensaje son requeridos")

    ok = await enviar_mensaje(wa_id, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje a WhatsApp")

    return {"ok": True}


# ── Recepción de mensajes ──────────────────────────────────────
@app.post("/webhook")
async def recibir_mensaje(request: Request):
    # Verificar firma de Meta
    firma   = request.headers.get("X-Hub-Signature-256", "")
    payload = await request.body()

    if not verificar_firma(payload, firma):
        print("[Webhook] ❌ Firma inválida")
        raise HTTPException(status_code=403, detail="Firma inválida")

    body = await request.json() if not payload else __import__("json").loads(payload)

    # Meta espera 200 inmediatamente — procesamos en background
    asyncio.create_task(_procesar_en_background(body))
    return Response(status_code=200)


async def _procesar_en_background(body: dict):
    """Procesa el mensaje en background para no bloquear el webhook."""
    resultado = extraer_mensaje(body)
    if not resultado:
        return  # No es un mensaje de texto, ignorar

    wa_id, message_id, texto = resultado

    # Reset follow-up al recibir nuevo mensaje del cliente
    from .bot import _sesiones
    if wa_id in _sesiones:
        _sesiones[wa_id]["followup_enviado"] = False

    # Evitar procesar el mismo mensaje dos veces (Meta puede reintentar)
    if message_id in _procesados:
        return
    _procesados.add(message_id)
    # Limpiar cache si crece demasiado
    if len(_procesados) > 1000:
        _procesados.clear()

    print(f"[Bot] 📩 {wa_id}: {texto[:80]}")

    # Extraer nombre del cliente del payload (si viene)
    try:
        nombre = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        nombre = wa_id

    try:
        # Marcar como leído
        await marcar_leido(message_id)

        # Guardar mensaje entrante en el inbox
        await registrar_mensaje(wa_id, nombre, "entrante", texto,
                                origen="bot", meta_message_id=message_id)

        # Verificar si está bajo control humano — si es así, no responder
        if await bajo_control_humano(wa_id):
            print(f"[Bot] ⏸️ {wa_id} bajo control humano — mensaje guardado, sin respuesta del bot")
            return

        # Procesar con Claude (pasamos el nombre para reconocimiento del cliente)
        respuesta = await procesar_mensaje(wa_id, texto, nombre=nombre)

        # Enviar respuesta al cliente
        ok = await enviar_mensaje(wa_id, respuesta)
        if ok:
            print(f"[Bot] ✅ Respuesta enviada a {wa_id}")
            # Guardar respuesta del bot en el inbox
            await registrar_mensaje(wa_id, nombre, "saliente", respuesta, origen="bot")
        else:
            print(f"[Bot] ❌ Error enviando respuesta a {wa_id}")

    except Exception as e:
        print(f"[Bot] ❌ Error procesando mensaje de {wa_id}: {e}")
        try:
            await enviar_mensaje(
                wa_id,
                "Lo siento, tuve un problema técnico. Por favor intenta nuevamente en un momento 🙏"
            )
        except Exception:
            pass
