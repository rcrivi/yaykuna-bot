"""
Cliente HTTP para la API de Reservas PHP.
Todas las llamadas al backend pasan por aquí.
"""
import httpx
import os
from typing import Optional

API_URL    = os.getenv("RESERVAS_API_URL", "").rstrip("/")
API_USER   = os.getenv("RESERVAS_API_USER", "admin")
API_PASS   = os.getenv("RESERVAS_API_PASS", "")
BOT_SECRET = os.getenv("BOT_SECRET", "")  # Secreto compartido con la API PHP

_token: Optional[str] = None


async def _get_token() -> str:
    """Obtiene (o reutiliza) el JWT de la API."""
    global _token
    if _token:
        return _token
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{API_URL}/auth", json={
            "username": API_USER,
            "password": API_PASS
        })
        r.raise_for_status()
        _token = r.json()["token"]
    return _token


async def _auth_headers() -> dict:
    token = await _get_token()
    return {"Authorization": f"Bearer {token}"}


# ── Endpoints públicos (sin auth) ─────────────────────────────

async def get_config_publico() -> dict:
    """Horarios, sectores, fechas bloqueadas, max personas."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_URL}/config/publico")
        r.raise_for_status()
        return r.json()


async def get_disponibilidad(fecha: str) -> dict:
    """Horarios disponibles para una fecha YYYY-MM-DD."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_URL}/disponibilidad", params={"fecha": fecha})
        r.raise_for_status()
        return r.json()


async def crear_reserva_publica(data: dict, canal: str = "WhatsApp") -> dict:
    """Crea reserva pública (status pending). Canal = WhatsApp o Presencial."""
    data["canal"] = canal
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{API_URL}/reservas/publico", json=data)
        r.raise_for_status()
        return r.json()


async def buscar_reserva_por_telefono(telefono: str) -> dict:
    """Busca reservas del cliente por teléfono."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_URL}/reservas/buscar", params={"telefono": telefono})
        r.raise_for_status()
        return r.json()


async def cancelar_reserva(reserva_id: int, telefono: str) -> dict:
    """Cancela una reserva verificando el teléfono del cliente."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{API_URL}/reservas/publico/{reserva_id}/cancelar",
            json={"telefono": telefono},
            headers={"X-HTTP-Method-Override": "PUT"}
        )
        r.raise_for_status()
        return r.json()


async def get_dias_cierre() -> dict:
    """Días de la semana que el restaurante está cerrado."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_URL}/config/dias-cierre")
        r.raise_for_status()
        return r.json()


# ── Endpoints autenticados ────────────────────────────────────

async def confirmar_reserva(reserva_id: int) -> dict:
    """Cambia estado de reserva a 'confirmed' (requiere auth)."""
    global _token
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{API_URL}/reservas/{reserva_id}",
            json={"status": "confirmed"},
            headers={**headers, "X-HTTP-Method-Override": "PUT"}
        )
        if r.status_code == 401:
            # Token expirado — refrescar y reintentar una vez
            _token = None
            headers = await _auth_headers()
            r = await client.put(
                f"{API_URL}/reservas/{reserva_id}",
                json={"status": "confirmed"},
                headers={**headers, "X-HTTP-Method-Override": "PUT"}
            )
        r.raise_for_status()
        return r.json()


async def crear_pedido(wa_id: str, nombre: str, telefono: str, items: list, notas: str = "") -> dict:
    """Crea un pedido para llevar (takeaway). items = [{nombre, precio, cantidad}]"""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API_URL}/pedidos/publico", json={
            "wa_id":    wa_id,
            "nombre":   nombre,
            "telefono": telefono,
            "items":    items,
            "notas":    notas,
        }, headers=headers)
        r.raise_for_status()
        return r.json()


async def ver_mis_pedidos(wa_id: str) -> dict:
    """Busca pedidos recientes del cliente por wa_id (últimas 24h)."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_URL}/pedidos/buscar", params={"wa_id": wa_id}, headers=headers)
        r.raise_for_status()
        return r.json()


async def marcar_pedido_notificado(pedido_id: int) -> None:
    """Marca el pedido como notificado al local (evita doble aviso)."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.put(
                f"{API_URL}/pedidos/{pedido_id}/notificado",
                json={},
                headers={**headers, "X-HTTP-Method-Override": "PUT"}
            )
    except Exception:
        pass


async def registrar_mensaje(wa_id: str, nombre: str, direccion: str, mensaje: str,
                            origen: str = "bot", meta_message_id: str = "") -> None:
    """Guarda un mensaje en la BD del inbox WhatsApp. No lanza excepción si falla."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{API_URL}/wa/mensajes/registrar", json={
                "wa_id":            wa_id,
                "nombre":           nombre,
                "direccion":        direccion,   # "entrante" | "saliente"
                "mensaje":          mensaje,
                "origen":           origen,       # "bot" | "humano"
                "meta_message_id":  meta_message_id,
            }, headers=headers)
    except Exception:
        pass  # El inbox no debe romper el flujo del bot


async def bajo_control_humano(wa_id: str) -> bool:
    """Verifica si la conversación está bajo control humano (bot pausado para este cliente)."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_URL}/wa/conversaciones/{wa_id}/control", headers=headers)
            if r.status_code == 200:
                return r.json().get("control_humano", False)
    except Exception:
        pass
    return False


async def get_flujo_config() -> dict:
    """Lee la configuración de follow-up desde la API (con cache interno)."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{API_URL}/config/flujo/publico", headers=headers)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    # Valores por defecto si la API no responde
    return {
        "flujo_activo": 1,
        "flujo_followup_reserva": 7,
        "flujo_followup_pedido": 3,
        "palabra_clave_presencial": "*mesa*",
    }


async def registrar_escalado(wa_id: str, motivo: str, mensaje: str, nombre: str = "") -> dict:
    """Registra un escalado al administrador en la base de datos."""
    headers = {"X-Bot-Secret": BOT_SECRET} if BOT_SECRET else {}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{API_URL}/bot/escalado", json={
            "wa_id":   wa_id,
            "motivo":  motivo,
            "mensaje": mensaje,
            "nombre":  nombre,
        }, headers=headers)
        # No hacer raise — si fa