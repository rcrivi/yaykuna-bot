"""
Servidor FastAPI -- Bot de reservas multi-restaurante.
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
from .whatsapp import enviar_mensaje, marcar_leido, verificar_firma, extraer_mensaje, descargar_media
from .api_client import ApiClient

_RESTAURANTES_JSON = os.getenv("RESTAURANTES", "{}")
try:
    RESTAURANTES: dict = json.loads(_RESTAURANTES_JSON)
except json.JSONDecodeError:
    print("[Config] ERROR: Variable RESTAURANTES no es JSON valido. Usando modo single-tenant.")
    RESTAURANTES = {}

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

_api_clients: dict[str, ApiClient] = {}
for _pid, _cfg in RESTAURANTES.items():
    _api_clients[_pid] = ApiClient(
        api_url    = _cfg.get("api_url", ""),
        api_user   = _cfg.get("api_user", "admin"),
        api_pass   = _cfg.get("api_pass", ""),
        bot_secret = _cfg.get("bot_secret", ""),
    )

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "bot_reservas_2026")

_procesados: set[str] = set()
_flujo_configs: dict[str, dict] = {}
_flujo_task: asyncio.Task | None = None

# ── Buffer de mensajes (debounce por wa_id) ─────────────────
_DEBOUNCE_SECS = 10
_msg_buffer: dict[str, list] = {}
_msg_timers: dict[str, asyncio.Task] = {}


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

                    # No enviar follow-up si el cliente se despidio
                    DESPEDIDAS = [
                        # Despedidas explícitas
                        "adios", "hasta luego", "hasta pronto", "hasta la proxima",
                        "nos vemos", "chao", "chao chao", "ciao", "bye", "bye bye",
                        # Agradecimientos de cierre
                        "gracias", "muchas gracias", "ok gracias", "listo gracias",
                        "perfecto gracias", "gracias igual", "gracias de todas formas",
                        # Cierres de conversación
                        "nada mas", "nada más", "eso era todo", "con eso basta",
                        "eso es todo", "ya esta", "ya estuvo", "listo entonces",
                        "ok entonces", "con eso me basta",
                        # Chilenas informales
                        "abrazo", "un abrazo", "abrazo grande", "beso", "besito",
                        "buenas", "estamos", "quedamos", "cuidense", "cuídate",
                        "que esten bien", "que este bien", "que disfrutes",
                        # Con negación
                        "por ahora no", "no por ahora", "no gracias", "nada mas gracias",
                        "no necesito", "no por el momento",
                        # Otras
                        "saludos", "buen provecho", "excelente gracias",
                    ]
                    msgs_usuario = [m for m in msgs if m.get("role") == "user"
                                    and isinstance(m.get("content"), str)]
                    if msgs_usuario:
                        # Revisar los ultimos 2 mensajes del cliente (no solo el ultimo)
                        for msg_reciente in msgs_usuario[-2:]:
                            txt_reciente = msg_reciente["content"].strip().lower()
                            if any(d in txt_reciente for d in DESPEDIDAS):
                                sesion["followup_enviado"] = True  # bloquear futuros
                                break
                        if sesion.get("followup_enviado"):
                            continue

                    # Detectar intencion transaccional SOLO en mensajes recientes del usuario
                    # Usar solo los ultimos 6 mensajes del usuario para evitar falsos positivos
                    # de conversaciones anteriores dentro de la misma sesion de 4 horas
                    msgs_recientes = msgs_usuario[-6:] if len(msgs_usuario) > 6 else msgs_usuario
                    historial_reciente = " ".join(
                        m["content"].lower() for m in msgs_recientes
                    )
                    es_pedido  = any(w in historial_reciente for w in
                                     ["pedido","llevar","takeaway","carrito","quiero pedir",
                                      "para llevar","delivery","plato","menu","carta",
                                      "quiero un","dame un","me das","paso a buscar",
                                      "buscar","retirar","retiro","para llevar",
                                      "me lo preparan","cuanto sale","cuanto cuesta"])
                    es_reserva = any(w in historial_reciente for w in
                                     ["reserva","reservar","mesa","personas","fecha",
                                      "sector","salon","terraza","disponib",
                                      "para cuantas","cuantas personas","quiero una mesa"])
                    # Si ambos coinciden, el pedido tiene prioridad
                    if es_pedido and es_reserva:
                        es_reserva = False

                    if not (es_pedido or es_reserva):
                        continue

                    limite = min_pedido if es_pedido else min_reserva
                    if inactivo_min >= limite:
                        tipo = "pedido" if es_pedido else "reserva"
                        msg_followup = (
                            "Sigues por ahi? Cuando quieras continuamos con tu pedido."
                            if es_pedido else
                            "Sigues por ahi? Cuando quieras te ayudo con la reserva."
                        )
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _flujo_task
    restaurantes_activos = list(RESTAURANTES.keys())
    print(f"[Bot] Iniciando servidor -- {len(restaurantes_activos)} restaurante(s): {restaurantes_activos}")
    _flujo_task = asyncio.create_task(_loop_followup())
    yield
    if _flujo_task:
        _flujo_task.cancel()
    print("[Bot] Servidor apagado.")


app = FastAPI(
    title       = "Bot Reservas Multi-Restaurante",
    description = "Agente WhatsApp de reservas -- multi-tenant",
    version     = "2.0.0",
    lifespan    = lifespan,
    docs_url    = None,
    redoc_url   = None,
)

panel_path = os.path.join(os.path.dirname(__file__), "..", "panel")
if os.path.isdir(panel_path):
    app.mount("/panel", StaticFiles(directory=panel_path, html=True), name="panel")


@app.get("/health")
async def health():
    return {
        "status":       "ok",
        "version":      "2.0.0",
        "restaurantes": len(RESTAURANTES),
    }


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


_INTENTO_TRANSACCIONAL = [
    "reserva","reservar","mesa","quiero","necesito","disponib","pedido",
    "pedir","llevar","takeaway","sector","fecha","hora","persona"
]


async def _debounce_y_responder(session_key: str, phone_id: str,
                                 rest_config: dict, api: "ApiClient"):
    """Espera DEBOUNCE_SECS y luego procesa todos los mensajes acumulados juntos."""
    await asyncio.sleep(_DEBOUNCE_SECS)

    msgs = _msg_buffer.pop(session_key, [])
    _msg_timers.pop(session_key, None)
    if not msgs:
        return

    wa_id  = msgs[0]["wa_id"]
    nombre = msgs[0]["nombre"]

    # Combinar textos (ignorar "[imagen]" si hay texto real)
    textos = [m["texto"] for m in msgs
              if m.get("texto") and m["texto"] not in ("", "[imagen]", "[imagen: comprobante]")]
    texto_combinado = "\n".join(textos) if textos else ""

    # Usar la última imagen disponible
    imagen_b64  = None
    imagen_mime = "image/jpeg"
    for m in reversed(msgs):
        if m.get("imagen_b64"):
            imagen_b64  = m["imagen_b64"]
            imagen_mime = m.get("imagen_mime", "image/jpeg")
            if not texto_combinado:
                texto_combinado = "[imagen]"
            break

    if not texto_combinado and not imagen_b64:
        return

    if len(msgs) > 1:
        print(f"[Bot] Buffer: {len(msgs)} msgs de {wa_id} combinados → '{texto_combinado[:80]}'")

    try:
        respuesta = await procesar_mensaje(
            session_id  = session_key,
            wa_id       = wa_id,
            texto       = texto_combinado,
            nombre      = nombre,
            rest_config = rest_config,
            api         = api,
            imagen_b64  = imagen_b64,
            imagen_mime = imagen_mime,
        )
        ok = await enviar_mensaje(wa_id, respuesta, phone_id=phone_id)
        if ok:
            print(f"[Bot] Respuesta enviada a {wa_id}")
            await api.registrar_mensaje(wa_id, nombre, "saliente", respuesta, origen="bot")
        else:
            print(f"[Bot] Error enviando respuesta a {wa_id}")
    except Exception as e:
        print(f"[Bot] Error procesando buffer de {wa_id}: {e}")
        try:
            await enviar_mensaje(
                wa_id,
                "Lo siento, tuve un problema técnico. Por favor intenta nuevamente.",
                phone_id=phone_id
            )
        except Exception:
            pass


async def _procesar_en_background(body: dict):
    """Procesa el mensaje identificando el restaurante por phone_number_id."""
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

    wa_id, message_id, texto, msg_tipo, media_id, media_mime = resultado
    session_id = f"{phone_id}:{wa_id}"

    # Reset follow-up solo si el cliente retoma intencion transaccional
    if session_id in _sesiones_bot:
        texto_lower = texto.lower()
        if any(w in texto_lower for w in _INTENTO_TRANSACCIONAL):
            _sesiones_bot[session_id]["followup_enviado"] = False

    if message_id in _procesados:
        return
    _procesados.add(message_id)
    if len(_procesados) > 1000:
        _procesados.clear()

    restaurante_nombre = rest_config.get("nombre", "Restaurante")
    tipo_log = "imagen" if msg_tipo == "image" else "texto"
    print(f"[Bot] [{restaurante_nombre}] {wa_id} [{tipo_log}]: {texto[:80]}")

    try:
        nombre = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError):
        nombre = wa_id

    try:
        await marcar_leido(message_id, phone_id=phone_id)

        texto_log = texto if msg_tipo == "text" else (f"[imagen] {texto}".strip() if texto and texto != "[imagen]" else "[imagen: comprobante]")

        # Descargar imagen ANTES de registrar, para adjuntarla al mensaje en la BD
        imagen_b64  = None
        imagen_mime = "image/jpeg"
        if msg_tipo == "image" and media_id:
            import base64
            img_bytes, img_mime = await descargar_media(media_id)
            if img_bytes:
                imagen_b64  = base64.b64encode(img_bytes).decode()
                imagen_mime = img_mime or "image/jpeg"
                print(f"[Bot] Imagen descargada para {wa_id}: {len(img_bytes)} bytes ({imagen_mime})")
            else:
                print(f"[Bot] No se pudo descargar imagen de {wa_id}")

        await api.registrar_mensaje(
            wa_id, nombre, "entrante", texto_log,
            origen="bot", meta_message_id=message_id,
            imagen_b64=imagen_b64, imagen_mime=imagen_mime
        )

        if await api.bajo_control_humano(wa_id):
            print(f"[Bot] {wa_id} bajo control humano -- sin respuesta del bot")
            return

        # ── Buffer + debounce: acumular mensajes y esperar 3 s ──────
        session_key = session_id  # ya tiene formato phone_id:wa_id
        if session_key not in _msg_buffer:
            _msg_buffer[session_key] = []

        _msg_buffer[session_key].append({
            "wa_id":      wa_id,
            "nombre":     nombre,
            "texto":      texto,
            "imagen_b64": imagen_b64,
            "imagen_mime": imagen_mime,
        })

        # Cancelar timer anterior y crear uno nuevo
        timer_anterior = _msg_timers.get(session_key)
        if timer_anterior and not timer_anterior.done():
            timer_anterior.cancel()

        _msg_timers[session_key] = asyncio.create_task(
            _debounce_y_responder(session_key, phone_id, rest_config, api)
        )

    except Exception as e:
        print(f"[Bot] Error procesando mensaje de {wa_id}: {e}")
