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


async def crear_reserva_publica(data: dict) -> dict:
    """Crea reserva pública (status pending). Canal = WhatsApp."""
    data["canal"] = "WhatsApp"
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
        # No hacer raise — si falla el registro no queremos romper el flujo
        return r.json() if r.status_code < 300 else {"ok": False}
