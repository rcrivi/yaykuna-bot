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


async def enviar_carta_interactiva(to: str, phone_id: str = "") -> bool:
    pid = phone_id or _DEFAULT_PHONE_ID
    if not pid or not TOKEN:
        return False
    url = _api_url(pid)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type":  "application/json"
    }
    mensajes = [
        {
            "messaging_product": "whatsapp",
            "to":   to,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": "Aqui tienes nuestra carta completa"},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "Ver carta online",
                        "url": "https://yaykuna.cl/carta.html"
                    }
                }
            }
        },
        {
            "messaging_product": "whatsapp",
            "to":   to,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": "Tambien disponible para descargar:"},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "Descargar PDF",
                        "url": "https://yaykuna.cl/Carta/Cartayaykuna.pdf"
                    }
                }
            }
        }
    ]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for msg_payload in mensajes:
                r = await client.post(url, json=msg_payload, headers=headers)
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


def extraer_mensaje(body: dict):
    """Extrae (wa_id, message_id, texto) del payload del webhook de Meta."""
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        messages = value.get("messages", [])
        if not messages:
            return None
        msg = messages[0]
        if msg.get("type") != "text":
            return None
        wa_id      = msg["from"]
        message_id = msg["id"]
        texto      = msg["text"]["body"].strip()
        return wa_id, message_id, texto
    except (KeyError, IndexError):
        return None
