"""
Servidor FastAPI — Bot de reservas multi-restaurante.
Maneja:
  - GET  /webhook  -> verificacion de Meta
  - POST /webhook  -> mensajes entrantes de WhatsApp (multi-tenant)
  - GET  /health   -> health check de Railway
  - POST /send-message -> envio manual desde el panel PHP
"""
import os
import json
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

from .bot import procesar_mensaje, _sesiones as _sesiones_bot
from .whatsapp import enviar_mensaje, marcar_leido, verificar_firma, extraer_mensaje
from .api_client import ApiClient

# ── Configuracion multi-restaurante ───────────────────────────
# Formato RESTAURANTES (variable de entorno JSON):
# {
#   "PHONE_NUMBER_ID_1": {
#     "nombre":     "Restaurante Uno",
#     "direccion":  "Calle 123, Santiago",
#     "tel":        "+56 9 XXXX XXXX",
#     "ig":         "@restaurante_uno",
#     "wa_local":   "569XXXXXXXXX",        <- wa_id del local para notificaciones
#     "api_url":    "https://uno.cl/api-reservas",
#     "api_user":   "admin",
#     "api_pass":   "Admin2026!",
#     "bot_secret": "secreto_uno",
#     "system_prompt": "..."               <- opcional, prompt personalizado
#   },
#   "PHONE_NUMBER_ID_2": { ... }
# }

_RESTAURANTES_JSON = os.getenv("RESTAURANTES", "{}")
try:
    RESTAURANTES: dict = json.loads(_RESTAURANTES_JSON)
except json.JSONDecodeError:
    print("[Config] ERROR: Variable RESTAURANTES no es JSON valido. Usando modo single-tenant.")
    RESTAURANTES = {}

# Modo single-tenant (compatibilidad con instalacion original de Yaykuna)
_SINGLE_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
if _SINGLE_PHONE_ID and _SINGLE_PHONE_ID not in RESTAURANTES:
    RESTAURANTES[_SINGLE_PHONE_ID] = {
        "nombre":     os.getenv("RESTAURANTE_NOMBRE",    "Restaurante"),
        "direccion":  os.getenv("RESTAURANTE_DIRECCION", ""),
        "tel":        os.getenv("RESTAURANTE_TEL",       ""),
        "ig":         os.getenv("RESTAURANTE_IG",        ""),
        "wa_local":   os.getenv("RESTAURANTE_WA_ID",     ""),
        "api_url":    os.getenv("RESERVAS_API_URL",      ""),
        "api_user":   os.getenv("RESERVAS_API_USER",     "admin"),
        "api_pass":   os.getenv("RESERVAS_API_PASS",     ""),
        "bot_secret": os.getenv("BOT_SECRET",            ""),
    }

# Crear instancias de ApiClient por restaurante
_api_clients: dict[str, ApiClient] = {}
for _pid, _cfg in RESTAURANTES.items():
    _api_clients[_pid] = ApiClient(
        api_url    = _cfg.get("api_url", ""),
        api_user   = _cfg.get("api_user", "admin"),
        api_pass   = _cfg.get("api_pass", ""),
        bot_secret = _cfg.get("bot_secret", ""),
    )

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "bot_reservas_2026")

# IDs de mensajes procesados (evitar duplicados de Meta)
_procesados: set[str] = set()

# Cache de config de flujo por restaurante
_flujo_configs: dict[str, dict] = {}
_flujo_task: asyncio.Task | None = None


# ── Loop de follow-up (verifica sesiones inactivas) ───────────

async def _loop_followup():
    """Revisa cada 60s las sesiones inactivas y envia follow-up si corresponde."""
    from datetime import datetime, timedelta

    config_actualizado: dict[str, datetime | None] = {pid: None for pid in RESTAURANTES}

    while True:
        await asyncio.sleep(60)
        try:
            for phone_id, rest_config in RESTAURANTES.items():
                api = _api_clients.get(phone_id)
                if not api:
                    continue

                # Refrescar config de flujo cada 5 min por restaurante
                ahora = datetime.utcnow()
                ultima = config_actualizado.get(phone_id)
                if ultima is None or (ahora - ultima).seconds > 300:
                    _flujo_configs[phone_id] = await api.get_flujo_config()
                    config_actualizado[phone_id] = ahora

                flujo = _flujo_configs.get(phone_id, {})
                if not flujo.get("flujo_activo", 1):
                    continue

                min_reserva = int(flujo.get("flujo_followup_reserva", 7))
                min_pedido  = int(flujo.get("flujo_followup_pedido",  3))

                # Solo procesar sesiones de ESTE restaurante
                prefix = f"{phone_id}:"
                for session_id, sesion in list(_sesiones_bot.items()):
                    if not session_id.startswith(prefix):
                        continue

                    wa_id = session_id[len(prefix):]
                    msgs  = sesion.get("messages", [])
                    if not msgs:
                        continue

                    ultimo = msgs[-1]
                    if ultimo.get("role") != "assistant":
                        continue
                    if sesion.get("followup_enviado"):
                        continue

                    inactivo_min = (datetime.utcnow() - sesion["updated"]).seconds // 60

                    historial = " ".join(
                        (m.get("content") or "") if isinstance(m.get("content"), str)
                        else " ".join(b.get("text", "") for b in (m.get("content") or []) if isinstance(b, dict))
                        for m in msgs
                    )
                    es_pedido  = any(w in historial.lower() for w in ["pedido","llevar","takeaway","carrito","items","plato"])
                    es_reserva = any(w in historial.lower() for w in ["reserva","mesa","personas","fecha","hora","sector"])

                    if not (es_pedido or es_reserva):
                        continue

                    limite = min_pedido if es_pedido else min_reserva
                    if inactivo_min >= limite:
                        tipo = "pedido" if es_pedido else "reserva"
                        msg_followup = f"Sigues por ahi? Cuando quieras continuamos con tu {tipo}."
                        try:
                            ok = await enviar_mensaje(wa_id, msg_followup, phone_id=phone_id)
                            if ok:
                                sesion["followup_enviado"] = True
                                await api.registrar_mensaje(
                                    wa_id, sesion.get("nombre", ""), "saliente", msg_followup, origen="bot"
                                )
                                print(f"[Flujo] Followup enviado a {wa_id} ({tipo}, {inactivo_min} min)")
                        except Exception as e:
                            print(f"[Flujo] Error enviando followup a {wa_id}: {e}")

        except Exception as e:
            print(f"[Flujo] Error en loop: {e}")


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _flujo_task
    restaurantes_activos = list(RESTAURANTES.keys())
    print(f"[Bot] Iniciando servidor — {len(restaurantes_activos)} restaurante(s): {restaurantes_activos}")
    _flujo_task = asyncio.create_task(_loop_followup())
    yield
    if _flujo_task:
        _flujo_task.cancel()
    print("[Bot] Servidor apagado.")


app = FastAPI(
    title       = "Bot Reservas Multi-Restaurante",
    description = "Agente WhatsApp de reservas — multi-tenant",
    version     = "2.0.0",
    lifespan    = lifespan,
    docs_url    = None,
    redoc_url   = None,
)

# Servir panel de administracion
panel_path = os.path.join(os.path.dirname(__file__), "..", "panel")
if os.path.isdir(panel_path):
    app.mount("/panel", StaticFiles(directory=panel_path, html=True), name="panel")


# ── Health check ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":       "ok",
        "version":      "2.0.0",
        "restaurantes": len(RESTAURANTES),
    }


# ── Verificacion del webhook ───────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[Webhook] Verificacion exitosa")
        return PlainTextResponse(challenge)

    print(f"[Webhook] Token invalido: {token}")
    raise HTTPException(status_code=403, detail="Token invalido")


# ── Envio manual desde el panel PHP ───────────────────────────

@app.post("/send-message")
async def send_message(request: Request):
    body       = await request.json()
    phone_id   = body.get("phone_id", _SINGLE_PHONE_ID)
    bot_secret = RESTAURANTES.get(phone_id, {}).get("bot_secret", "")
    secret_hdr = request.headers.get("X-Bot-Secret", "")

    if bot_secret and secret_hdr != bot_secret:
        raise HTTPException(status_code=403, detail="Secreto invalido")

    wa_id   = body.get("wa_id", "").strip()
    mensaje = body.get("mensaje", "").strip()
    if not wa_id or not mensaje:
        raise HTTPException(status_code=400, detail="wa_id y mensaje son requeridos")

    ok = await enviar_mensaje(wa_id, mensaje, phone_id=phone_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje")
    return {"ok": True}


# ── Recepcion de mensajes de Meta ─────────────────────────────

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    firma   = request.headers.get("X-Hub-Signature-256", "")
    payload = await request.body()

    if not verificar_firma(payload, firma):
        print("[Webhook] Firma invalida")
        raise HTTPException(status_code=403, detail="Firma invalida")

    body = json.loads(payload)
    asyncio.create_task(_procesar_en_background(body))
    return Response(status_code=200)


async def _procesar_en_background(body: dict):
    """Procesa el mensaje identificando el restaurante por phone_number_id."""
    # Identificar restaurante
    try:
        phone_id = body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    except (KeyError, IndexError):
        return

    rest_config = RESTAURANTES.get(phone_id)
    api         = _api_clients.get(phone_id)

    if not rest_config or not api:
        print(f"[Bot] Restaurante no configurado para phone_id={phone_id}")
        return

    resultado = extraer_mensaje(body)
    if not resultado:
        return

    wa_id, message_id, texto = resultado
    session_id = f"{phone_id}:{wa_id}"

    # Reset follow-up al recibir nuevo mensaje
    if session_id in _sesiones_bot:
        _sesiones_bot[session_id]["followup_enviado"] = False

    # Evitar duplicados
    if message_id in _procesados:
        return
    _procesados.add(message_id)
    if len(_procesados) > 1000:
        _procesados.clear()

    restaurante_nombre = rest_config.get("nombre", "Restaurante")
    print(f"[Bot] [{restaurante_nombre}] {wa_id}: {texto[:80]}")

    try:
        nombre = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        nombre = wa_id

    try:
        await marcar_leido(message_id, phone_id=phone_id)

        await api.registrar_mensaje(
            wa_id, nombre, "entrante", texto,
            origen="bot", meta_message_id=message_id
        )

        if await api.bajo_control_humano(wa_id):
            print(f"[Bot] {wa_id} bajo control humano — sin respuesta del bot")
            return

        respuesta = await procesar_mensaje(
            session_id  = session_id,
            wa_id       = wa_id,
            texto       = texto,
            nombre      = nombre,
            rest_config = rest_config,
            api         = api,
        )

        ok = await enviar_mensaje(wa_id, respuesta, phone_id=phone_id)
        if ok:
            print(f"[Bot] Respuesta enviada a {wa_id}")
            await api.registrar_mensaje(wa_id, nombre, "saliente", respuesta, origen="bot")
        else:
            print(f"[Bot] Error enviando respuesta a {wa_id}")

    except Exception as e:
        print(f"[Bot] Error procesando mensaje de {wa_id}: {e}")
        try:
            await enviar_mensaje(
                wa_id,
                "Lo siento, tuve un problema tecnico. Por favor intenta nuevamente en un momento.",
                phone_id=phone_id
            )
        except Exception:
            pass
