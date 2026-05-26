"""
Cliente HTTP para la API de Reservas PHP.
Diseñado para multi-restaurante: cada instancia apunta a una API distinta.
"""
import httpx
from typing import Optional


class ApiClient:
    """Cliente HTTP para una instancia de la API de reservas.

    Uso:
        api = ApiClient(api_url="https://mirestaurante.cl/api-reservas",
                        api_user="admin", api_pass="Admin2026!")
    """

    def __init__(self, api_url: str, api_user: str, api_pass: str, bot_secret: str = ""):
        self._url    = api_url.rstrip("/")
        self._user   = api_user
        self._pass   = api_pass
        self._secret = bot_secret
        self._token: Optional[str] = None

    # ── Autenticacion ──────────────────────────────────────────

    async def _get_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self._url}/auth", json={
                "username": self._user,
                "password": self._pass
            })
            r.raise_for_status()
            self._token = r.json()["token"]
        return self._token

    async def _auth_headers(self) -> dict:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}"}

    def _bot_headers(self) -> dict:
        return {"X-Bot-Secret": self._secret} if self._secret else {}

    def invalidar_token(self):
        """Fuerza re-autenticacion en el proximo request."""
        self._token = None

    # ── Endpoints publicos (sin auth) ──────────────────────────

    async def get_config_publico(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self._url}/config/publico")
            r.raise_for_status()
            return r.json()

    async def get_disponibilidad(self, fecha: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self._url}/disponibilidad", params={"fecha": fecha})
            r.raise_for_status()
            return r.json()

    async def crear_reserva_publica(self, data: dict, canal: str = "WhatsApp") -> dict:
        data["canal"] = canal
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self._url}/reservas/publico", json=data)
            r.raise_for_status()
            return r.json()

    async def buscar_reserva_por_telefono(self, telefono: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self._url}/reservas/buscar", params={"telefono": telefono})
            r.raise_for_status()
            return r.json()

    async def cancelar_reserva(self, reserva_id: int, telefono: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.put(
                f"{self._url}/reservas/publico/{reserva_id}/cancelar",
                json={"telefono": telefono}
            )
            r.raise_for_status()
            return r.json()

    # ── Endpoints privados (con JWT) ───────────────────────────

    async def confirmar_reserva(self, reserva_id: int) -> dict:
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.put(
                f"{self._url}/reservas/{reserva_id}",
                json={"status": "confirmed"},
                headers=headers
            )
            if r.status_code == 401:
                self.invalidar_token()
                headers = await self._auth_headers()
                r = await client.put(
                    f"{self._url}/reservas/{reserva_id}",
                    json={"status": "confirmed"},
                    headers=headers
                )
            r.raise_for_status()
            return r.json()

    # ── Endpoints del bot (X-Bot-Secret) ──────────────────────

    async def registrar_mensaje(self, wa_id: str, nombre: str, direccion: str,
                                 mensaje: str, origen: str = "bot",
                                 meta_message_id: str = None) -> None:
        data = {
            "wa_id":     wa_id,
            "nombre":    nombre,
            "direccion": direccion,
            "mensaje":   mensaje,
            "origen":    origen,
        }
        if meta_message_id:
            data["meta_message_id"] = meta_message_id
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{self._url}/wa/mensajes/registrar",
                    json=data,
                    headers=self._bot_headers()
                )
        except Exception:
            pass

    async def bajo_control_humano(self, wa_id: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{self._url}/wa/conversaciones/{wa_id}/control",
                    headers=self._bot_headers()
                )
                return r.json().get("control_humano", False)
        except Exception:
            return False

    async def get_flujo_config(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{self._url}/config/flujo/publico",
                    headers=self._bot_headers()
                )
                return r.json()
        except Exception:
            return {
                "flujo_activo":           1,
                "flujo_followup_reserva": 7,
                "flujo_followup_pedido":  3,
                "palabra_clave_presencial": "*mesa*"
            }

    async def registrar_escalado(self, wa_id: str, motivo: str,
                                  mensaje: str, nombre: str = "") -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{self._url}/bot/escalado",
                    json={"wa_id": wa_id, "motivo": motivo,
                          "mensaje": mensaje, "nombre": nombre},
                    headers=self._bot_headers()
                )
        except Exception:
            pass

    # ── Modulo pedidos ─────────────────────────────────────────

    async def crear_pedido(self, wa_id: str, nombre: str, telefono: str,
                            items: list, notas: str = "") -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self._url}/pedidos/publico",
                json={
                    "wa_id":    wa_id,
                    "nombre":   nombre,
                    "telefono": telefono,
                    "items":    items,
                    "notas":    notas,
                    "canal":    "WhatsApp"
                },
                headers=self._bot_headers()
            )
            r.raise_for_status()
            return r.json()

    async def ver_mis_pedidos(self, wa_id: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{self._url}/pedidos/buscar",
                    params={"wa_id": wa_id},
                    headers=self._bot_headers()
                )
                return r.json()
        except Exception:
            return {"pedidos": []}

    async def marcar_pedido_notificado(self, pedido_id: int) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.put(
                    f"{self._url}/pedidos/{pedido_id}/notificado",
                    headers=self._bot_headers()
                )
        except Exception:
            pass

    async def marcar_transferencia_ok(self, pedido_id: int, monto: int = None) -> dict:
        """Marca un pedido como 'comprobante de transferencia verificado' por el bot."""
        data = {}
        if monto is not None:
            data["monto"] = monto
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.put(
                    f"{self._url}/pedidos/{pedido_id}/transferencia",
                    json=data,
                    headers=self._bot_headers()
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"error": str(e)}
