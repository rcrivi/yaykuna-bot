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
from .api_client import registrar_mensaje, bajo_control_humano

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "yaykuna_bot_2026")

# IDs de mensajes ya procesados (evitar duplicados)
_procesados: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[Yaykuna Bot] 🍽️  Iniciando servidor...")
    yield
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
