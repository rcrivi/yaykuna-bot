"""
Cliente para Meta WhatsApp Cloud API.
Envía mensajes de texto y maneja la verificación del webhook.
"""
import httpx
import os
import hashlib
import hmac

PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
API_URL  = f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages"


async def enviar_mensaje(to: str, texto: str) -> bool:
    """
    Envía un mensaje de texto al número 'to' (formato: 569XXXXXXXX).
    Retorna True si fue exitoso.
    """
    if not PHONE_ID or not TOKEN:
        print("[WhatsApp] ERROR: WHATSAPP_PHONE_ID o WHATSAPP_TOKEN no configurados")
        return False

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
            r = await client.post(API_URL, json=payload, headers=headers)
            if r.status_code != 200:
                print(f"[WhatsApp] Error {r.status_code}: {r.text}")
                return False
            return True
    except Exception as e:
        print(f"[WhatsApp] Excepción al enviar: {e}")
        return False


async def marcar_leido(message_id: str) -> None:
    """Marca el mensaje como leído (doble check azul)."""
    if not PHONE_ID or not TOKEN:
        return
    payload = {
        "messaging_product": "whatsapp",
        "status":            "read",
        "message_id":        message_id
    }
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(API_URL.replace("/messages", "/messages"), json=payload, headers=headers)
    except Exception:
        pass


def verificar_firma(payload_bytes: bytes, firma_header: str) -> bool:
    """
    Verifica la firma HMAC-SHA256 que Meta incluye en cada webhook.
    Protege contra peticiones falsas.
    """
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if not app_secret:
        return True  # En desarrollo sin secret, permitir todo

    if not firma_header or not firma_header.startswith("sha256="):
        return False

    firma_esperada = hmac.new(
        app_secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(firma_header[7:], firma_esperada)


def extraer_mensaje(body: dict) -> tuple[str, str, str] | None:
    """
    Extrae (wa_id, message_id, texto) del payload del webhook de Meta.
    Retorna None si no hay mensaje de texto válido.
    """
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Solo procesar mensajes entrantes de texto
        messages = value.get("messages", [])
        if not messages:
            return None

        msg = messages[0]
        if msg.get("type") != "text":
            return None

        wa_id      = msg["from"]           # Número del cliente: 569XXXXXXXX
        message_id = msg["id"]
        texto      = msg["text"]["body"].strip()

        return wa_id, message_id, texto
    except (KeyError, IndexError):
        return None
