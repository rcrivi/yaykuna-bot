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
        try:
            # Usar /config/bot (requiere bot secret) para evitar bloqueo del WAF de Hostinger
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self._url}/config/bot",
                    headers=self._bot_headers()
                )
                r.raise_for_status()
                data = r.json()
                tiene_menu = bool(data.get("menu"))
                print(f"[ApiClient] config OK — menu={'si ({} chars)'.format(len(data.get('menu',''))) if tiene_menu else 'VACIO'}")
                return data
        except Exception as e:
            print(f"[ApiClient] config ERROR: {e}")
            raise

    async def get_disponibilidad(self, fecha: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self._url}/disponibilidad",
                params={"fecha": fecha},
                headers=self._bot_headers()   # WAF de Hostinger bloquea sin este header
            )
            r.raise_for_status()
            return r.json()

    async def crear_reserva_publica(self, data: dict, canal: str = "WhatsApp") -> dict:
        data["canal"] = canal
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self._url}/reservas/publico",
                json=data,
                headers=self._bot_headers()
            )
            r.raise_for_status()
            return r.json()

    async def buscar_reserva_por_telefono(self, telefono: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self._url}/reservas/buscar",
                params={"telefono": telefono},
                headers=self._bot_headers()
            )
            r.raise_for_status()
            return r.json()

    async def cancelar_reserva(self, reserva_id: int, telefono: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.put(
                f"{self._url}/reservas/publico/{reserva_id}/cancelar",
                json={"telefono": telefono},
                headers=self._bot_headers()
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
                                 meta_message_id: str = None,
                                 imagen_b64: str = None,
                                 imagen_mime: str = "image/jpeg") -> None:
        data = {
            "wa_id":     wa_id,
            "nombre":    nombre,
            "direccion": direccion,
            "mensaje":   mensaje,
            "origen":    origen,
        }
        if meta_message_id:
            data["meta_message_id"] = meta_message_id
        if imagen_b64:
            data["imagen_b64"]  = imagen_b64
            data["imagen_mime"] = imagen_mime
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{self._url}/wa/mensajes/registrar",
                    json=data,
                    headers=self._bot_headers()
                )
        except Exception:
            pass

    async def buscar_pedido_pendiente(self, wa_id: str) -> dict:
        """Retorna contexto completo del pedido mas reciente del cliente para manejo de comprobantes."""
        try:
            result = await self.ver_mis_pedidos(wa_id)
            pedidos = result.get("pedidos", [])
            if not pedidos:
                return {"situacion": "sin_pedidos", "pedido": None}

            # Ordenar por id descendente (mas reciente primero)
            pedidos.sort(key=lambda p: int(p.get("id", 0)), reverse=True)
            reciente = pedidos[0]

            ya_pagado  = bool(reciente.get("transferencia_ok"))
            estado     = reciente.get("estado", "")
            cancelado  = estado in ("cancelled", "cancelado", "canceled")

            if cancelado:
                return {"situacion": "cancelado",  "pedido": reciente}
            if ya_pagado:
                return {"situacion": "ya_pagado",  "pedido": reciente}
            if estado in ("pending", "pendiente", "new", ""):
                return {"situacion": "pendiente",  "pedido": reciente}
            if estado in ("listo", "ready"):
                return {"situacion": "listo",      "pedido": reciente}
            if estado in ("confirmado", "confirmed", "en_preparacion"):
                return {"situacion": "en_preparacion", "pedido": reciente}

            # Otro estado (entregado, etc.)
            return {"situacion": "otro_estado", "pedido": reciente}

        except Exception as e:
            return {"situacion": "error", "pedido": None, "error": str(e)}

    async def modificar_reserva(self, reserva_id: int, cambios: dict) -> dict:
        """Modifica una reserva existente (fecha, hora, sector, personas)."""
        headers = await self._auth_headers()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.put(
                f"{self._url}/reservas/{reserva_id}",
                json=cambios,
                headers=headers
            )
            if r.status_code == 401:
                self.invalidar_token()
                headers = await self._auth_headers()
                r = await client.put(
                    f"{self._url}/reservas/{reserva_id}",
                    json=cambios,
                    headers=headers
                )
            r.raise_for_status()
            return r.json()

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
        payload = {
            "wa_id":    wa_id,
            "nombre":   nombre,
            "telefono": telefono,
            "items":    items,
            "notas":    notas,
            "canal":    "WhatsApp"
        }
        url = f"{self._url}/pedidos/publico"
        print(f"[ApiClient] crear_pedido → POST {url}")
        print(f"[ApiClient] crear_pedido payload: {payload}")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=self._bot_headers())
            print(f"[ApiClient] crear_pedido ← HTTP {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()

    async def ver_mis_pedidos(self, wa_id: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(
                    f"{self._url}/pedidos/buscar",
                    params={"wa_id": wa_id},
                    headers=self._bot_headers()
                )
                if r.status_code != 200:
                    print(f"[ApiClient] ver_mis_pedidos HTTP {r.status_code}: {r.text[:200]}")
                    return {"pedidos": []}
                data = r.json()
                print(f"[ApiClient] ver_mis_pedidos wa_id={wa_id} → {len(data.get('pedidos', []))} pedidos")
                return data
        except Exception as e:
            print(f"[ApiClient] ver_mis_pedidos error: {e}")
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

    async def agregar_items_pedido(self, pedido_id: int, items: list) -> dict:
        """Agrega items a un pedido existente (cualquier estado). Fusiona con los items actuales."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.put(
                    f"{self._url}/pedidos/{pedido_id}/items",
                    json={"items": items},
                    headers=self._bot_headers()
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"error": str(e)}

    async def get_historial_cliente(self, wa_id: str) -> dict:
        """Retorna historial de pedidos de un cliente para reconocimiento y personalizacion."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{self._url}/pedidos/historial",
                    params={"wa_id": wa_id},
                    headers=self._bot_headers()
                )
                r.raise_for_status()
                return r.json()
        except Exception:
            return {"es_nuevo": True, "total_pedidos": 0}
