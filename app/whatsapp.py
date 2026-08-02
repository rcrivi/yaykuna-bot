"""
Cliente para Meta WhatsApp Cloud API.
Soporta multi-tenant: acepta phone_id opcional, fallback a WHATSAPP_PHONE_ID.
"""
import httpx
import os
import hashlib
import hmac

_DEFAULT_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
TOKEN             = os.getenv("WHATSAPP_TOKEN", "")

PHONE_ID = _DEFAULT_PHONE_ID
API_URL  = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"


def _api_url(phone_id: str = "") -> str:
    pid = phone_id or _DEFAULT_PHONE_ID
    return f"https://graph.facebook.com/v20.0/{pid}/messages"


async def enviar_mensaje(to: str, texto: str, phone_id: str = "") -> bool:
    pid = phone_id or _DEFAULT_PHONE_ID
    if not pid or not TOKEN:
        print("[WhatsApp] ERROR: WHATSAPP_PHONE_ID o WHATSAPP_TOKEN no configurados")
        return False
    url = _api_url(pid)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "text",
        "text":              {"body": texto, "preview_url": False}
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code != 200:
                print(f"[WhatsApp] Error {r.status_code}: {r.text}")
                return False
            return True
    except Exception as e:
        print(f"[WhatsApp] Excepcion al enviar: {e}")
        return False


async def enviar_carta_interactiva(to: str, botones: list, phone_id: str = "") -> bool:
    """
    Envía 1 o 2 mensajes interactivos cta_url según los botones configurados.
    botones: [{"texto": "Ver carta digital", "url": "https://..."}, ...]
    """
    pid = phone_id or _DEFAULT_PHONE_ID
    if not pid or not TOKEN or not botones:
        return False
    url = _api_url(pid)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

    payloads = []
    for i, boton in enumerate(botones):
        body_text = "Aquí te dejo nuestra carta 😊" if i == 0 else "También disponible para descargar:"
        payloads.append({
            "messaging_product": "whatsapp",
            "to":   to,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": body_text},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": boton["texto"],
                        "url":          boton["url"]
                    }
                }
            }
        })

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for payload in payloads:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code != 200:
                    print(f"[WhatsApp] Error carta interactiva {r.status_code}: {r.text}")
                    return False
        return True
    except Exception as e:
        print(f"[WhatsApp] Excepcion enviando carta: {e}")
        return False


async def marcar_leido(message_id: str, phone_id: str = "") -> None:
    pid = phone_id or _DEFAULT_PHONE_ID
    if not pid or not TOKEN:
        return
    url = _api_url(pid)
    payload = {
        "messaging_product": "whatsapp",
        "status":            "read",
        "message_id":        message_id
    }
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload, headers=headers)
    except Exception:
        pass


def verificar_firma(payload_bytes: bytes, firma_header: str) -> bool:
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if not app_secret:
        print("[SECURITY WARNING] WHATSAPP_APP_SECRET no configurada.")
        env = os.getenv("ENVIRONMENT", os.getenv("RAILWAY_ENVIRONMENT", "development"))
        if env in ("production", "prod"):
            return False
        return True
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(
        app_secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(firma_header[7:], firma_esperada)


async def descargar_media(media_id: str) -> tuple[bytes, str] | tuple[None, None]:
    """Descarga un archivo media de Meta WhatsApp API.
    Retorna (bytes, mime_type) o (None, None) si falla.
    """
    if not TOKEN:
        return None, None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Paso 1: obtener la URL de descarga
            r = await client.get(
                f"https://graph.facebook.com/v20.0/{media_id}",
                headers=headers
            )
            if r.status_code != 200:
                print(f"[WhatsApp] Error obteniendo URL de media {media_id}: {r.status_code}")
                return None, None
            data     = r.json()
            url      = data.get("url")
            mime     = data.get("mime_type", "image/jpeg")
            if not url:
                return None, None
            # Paso 2: descargar la imagen
            r2 = await client.get(url, headers=headers)
            if r2.status_code != 200:
                print(f"[WhatsApp] Error descargando media: {r2.status_code}")
                return None, None
            return r2.content, mime
    except Exception as e:
        print(f"[WhatsApp] Excepcion descargando media {media_id}: {e}")
        return None, None


def extraer_mensaje(body: dict):
    """Extrae (wa_id, message_id, texto, tipo, media_id, mime_type) del webhook de Meta.
    - tipo: 'text' o 'image'
    - media_id: ID del archivo en Meta (solo para imágenes)
    - mime_type: tipo MIME de la imagen (solo para imágenes)
    Retorna None si el mensaje no es procesable.
    """
    try:
        entry    = body["entry"][0]
        changes  = entry["changes"][0]
        value    = changes["value"]
        messages = value.get("messages", [])
        if not messages:
            return None
        msg      = messages[0]
        wa_id      = msg["from"]
        message_id = msg["id"]
        msg_type   = msg.get("type", "text")

        if msg_type == "text":
            texto = msg["text"]["body"].strip()
            return wa_id, message_id, texto, "text", None, None

        elif msg_type == "image":
            img      = msg.get("image", {})
            media_id = img.get("id")
            mime     = img.get("mime_type", "image/jpeg")
            caption  = img.get("caption", "").strip()
            texto    = caption or "[imagen]"
            return wa_id, message_id, texto, "image", media_id, mime

        elif msg_type == "audio":
            audio    = msg.get("audio", {})
            media_id = audio.get("id")
            mime     = audio.get("mime_type", "audio/ogg; codecs=opus")
            return wa_id, message_id, "[audio]", "audio", media_id, mime

        else:
            # Tipo no soportado (video, documento, sticker, etc.)
            return None

    except (KeyError, IndexError):
        return None
