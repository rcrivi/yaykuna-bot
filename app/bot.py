"""
Nucleo del agente de reservas -- multi-restaurante.
Mantiene sesiones separadas por restaurante (phone_id:wa_id).
"""
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import anthropic
from .api_client import ApiClient

# -- Cliente Anthropic
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL  = "claude-haiku-4-5-20251001"

# -- Sesiones en memoria (session_id -> dict)
# session_id = "PHONE_ID:wa_id"
_sesiones: dict[str, dict] = {}
SESSION_TTL_HORAS = 4


def _limpiar_sesiones_viejas():
    limite = datetime.utcnow() - timedelta(hours=SESSION_TTL_HORAS)
    viejas = [k for k, v in _sesiones.items() if v["updated"] < limite]
    for k in viejas:
        del _sesiones[k]


def _get_sesion(session_id: str) -> dict:
    _limpiar_sesiones_viejas()
    if session_id not in _sesiones:
        _sesiones[session_id] = {
            "messages": [],
            "nombre":   "",
            "idioma":   "es",
            "canal":    "WhatsApp",
            "updated":  datetime.utcnow(),
        }
    return _sesiones[session_id]


# -- System Prompt por restaurante

def _build_system_prompt(rest_config: dict) -> str:
    """Construye el system prompt usando config del restaurante."""

    if rest_config.get("system_prompt"):
        return rest_config["system_prompt"]

    nombre    = rest_config.get("nombre",    "Restaurante")
    direccion = rest_config.get("direccion", "")
    tel       = rest_config.get("tel",       "")
    ig        = rest_config.get("ig",        "")
    horarios  = rest_config.get("horarios",  "Lunes a Sabado 12:30-23:00 hrs - Domingo 12:30-17:00 hrs")
    carta_url = rest_config.get("carta_url", "")
    menu_txt  = rest_config.get("menu", "")

    carta_seccion = ""
    if carta_url:
        carta_seccion = f"""
## CARTA DIGITAL
Cuando el cliente pida la carta o el menu, usa la herramienta `enviar_carta` para enviarla como botones interactivos.
URL de carta: {carta_url}
"""

    menu_seccion = f"\n## CARTA\n{menu_txt}\n" if menu_txt else ""

    return f"""
Eres el asistente virtual de **{nombre}**, ubicado en {direccion}.
Tu nombre es **{nombre} Bot**.

---
## PERSONALIDAD
- Amable, cercano y eficiente
- Detecta el idioma del cliente (espanol o ingles) y responde en ese idioma
- Nunca inventes informacion -- si no sabes algo, dilo con honestidad
- Usa emojis con moderacion para dar calidez

---
## INFORMACION DEL RESTAURANTE
- **Nombre:** {nombre}
- **Direccion:** {direccion}
- **Telefono:** {tel}
- **Instagram:** {ig}
- **Horarios:** {horarios}

---
{menu_seccion}
{carta_seccion}
---
## TUS CAPACIDADES
1. **Reservar mesa** -- verificar disponibilidad y confirmar al instante
2. **Responder sobre carta y precios**
3. **Buscar reservas existentes** -- por numero de telefono
4. **Cancelar reservas**
5. **Info del restaurante** -- horarios, direccion, etc.

---
## REGLAS DE RESERVA
- Verifica SIEMPRE disponibilidad antes de confirmar
- Datos requeridos: nombre completo, email, telefono, personas, fecha, hora, sector
- Recolecta los datos de forma conversacional
- El sector puede ser: Salon, Terraza, Bar o Privado
- Canal siempre se registra como 'WhatsApp'
- Maximo 20 personas -- mas personas, escalar al admin

---
## ESCALADO AL ADMINISTRADOR
Escala cuando:
- El cliente tiene queja grave
- Consulta fuera de tu alcance
- Solicita mas de 20 personas

Mensaje: "Entiendo tu consulta. Voy a comunicarme con nuestro equipo y te responderemos a la brevedad. Gracias por tu paciencia."

---
## FORMATO DE RESPUESTAS
- Respuestas cortas y directas (maximo 3-4 parrafos)
- Sin Markdown complejo (no tablas, no encabezados #)
- Emojis con moderacion
"""


# -- Contexto dinamico (hora actual)

def _contexto_dinamico(rest_config: dict, nombre_cliente: str = "",
                        es_conocido: bool = False,
                        config_pub: dict = None) -> str:
    tz_str = rest_config.get("zona_horaria", "America/Santiago")
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("America/Santiago")

    ahora = datetime.now(tz)
    dias  = ["Lunes","Martes","Miercoles","Jueves","Viernes","Sabado","Domingo"]
    dia   = dias[ahora.weekday()]

    ctx = f"""
---
## CONTEXTO ACTUAL
- **Fecha:** {dia} {ahora.strftime('%d/%m/%Y')}
- **Hora local:** {ahora.strftime('%H:%M')} hrs
"""
    # Estado especial de hoy (cierre anticipado / pedidos)
    if config_pub:
        hora_cierre_hoy = config_pub.get("hora_cierre_hoy")
        pedidos_hoy     = config_pub.get("pedidos_hoy", True)
        if hora_cierre_hoy:
            # Comparar hora actual con hora de cierre
            ya_cerrado = False
            try:
                h_cierre, m_cierre = [int(x) for x in hora_cierre_hoy.split(":")]
                ya_cerrado = (ahora.hour, ahora.minute) >= (h_cierre, m_cierre)
            except Exception:
                pass

            if ya_cerrado:
                ctx += f"- **RESTAURANTE CERRADO HOY:** Ya pasaron las {hora_cierre_hoy} hrs -- el restaurante cerro por evento especial.\n"
                ctx += "- **NO ofrecer reservas ni pedidos para hoy** -- el restaurante ya esta cerrado.\n"
                ctx += "- Si el cliente quiere reservar, ofrecerle fechas de MANANA en adelante.\n"
            else:
                ctx += f"- **AVISO HOY:** El restaurante cierra a las {hora_cierre_hoy} hrs por evento especial.\n"
                ctx += f"- **Horarios disponibles:** solo antes de las {hora_cierre_hoy} hrs.\n"
                if not pedidos_hoy:
                    ctx += "- **Pedidos para llevar:** NO disponibles hoy -- evento especial con cocina ocupada.\n"
                else:
                    ctx += f"- **Pedidos para llevar:** disponibles hasta las {hora_cierre_hoy} hrs.\n"

    if nombre_cliente and es_conocido:
        ctx += f"- **Cliente:** {nombre_cliente} -- ya nos escribio antes, saludalo por nombre.\n"
    elif nombre_cliente:
        ctx += f"- **Nombre del cliente:** {nombre_cliente}\n"

    return ctx


# -- Herramientas (Tools) para Claude

TOOLS = [
    {
        "name": "verificar_disponibilidad",
        "description": "Verifica si hay horarios disponibles para una fecha especifica. Usar SIEMPRE antes de crear una reserva.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD"}
            },
            "required": ["fecha"]
        }
    },
    {
        "name": "crear_reserva",
        "description": "Crea y confirma una reserva. Solo llamar con TODOS los datos y disponibilidad verificada.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string"},
                "email":   {"type": "string"},
                "phone":   {"type": "string"},
                "guests":  {"type": "integer"},
                "date":    {"type": "string", "description": "YYYY-MM-DD"},
                "time":    {"type": "string", "description": "HH:MM"},
                "sector":  {"type": "string", "description": "Salon, Terraza, Bar o Privado"},
                "message": {"type": "string"}
            },
            "required": ["name", "email", "phone", "guests", "date", "time", "sector"]
        }
    },
    {
        "name": "buscar_reserva",
        "description": "Busca reservas del cliente por su numero de telefono.",
        "input_schema": {
            "type": "object",
            "properties": {
                "telefono": {"type": "string"}
            },
            "required": ["telefono"]
        }
    },
    {
        "name": "cancelar_reserva",
        "description": "Cancela una reserva. Requiere ID de reserva y telefono del cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reserva_id": {"type": "integer"},
                "telefono":   {"type": "string"}
            },
            "required": ["reserva_id", "telefono"]
        }
    },
    {
        "name": "enviar_carta",
        "description": "Envia la carta del restaurante al cliente como botones interactivos. Usar cuando pida el menu o los precios.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "escalar_al_admin",
        "description": "Notifica al equipo del restaurante sobre una consulta que el bot no puede resolver.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo":  {"type": "string"},
                "mensaje": {"type": "string"}
            },
            "required": ["motivo", "mensaje"]
        }
    }
]


# -- Ejecutor de herramientas

async def ejecutar_herramienta(nombre: str, args: dict,
                                session_id: str, wa_id: str,
                                rest_config: dict, api: ApiClient) -> str:
    try:
        if nombre == "verificar_disponibilidad":
            data = await api.get_disponibilidad(args["fecha"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "crear_reserva":
            canal   = _get_sesion(session_id).get("canal", "WhatsApp")
            reserva = await api.crear_reserva_publica(args, canal=canal)
            reserva_id = reserva.get("id")
            if reserva_id:
                try:
                    await api.confirmar_reserva(reserva_id)
                    reserva["status"] = "confirmed"
                except Exception as e:
                    print(f"[Bot] No se pudo confirmar reserva {reserva_id}: {e}")
            return json.dumps(reserva, ensure_ascii=False)

        elif nombre == "buscar_reserva":
            data = await api.buscar_reserva_por_telefono(args["telefono"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "cancelar_reserva":
            data = await api.cancelar_reserva(args["reserva_id"], args["telefono"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "enviar_carta":
            carta_url = rest_config.get("carta_url", "")
            if carta_url:
                from .whatsapp import enviar_carta_interactiva
                ok = await enviar_carta_interactiva(wa_id, carta_url=carta_url)
            else:
                ok = False
            return json.dumps({"ok": ok})

        elif nombre == "escalar_al_admin":
            sesion = _get_sesion(session_id)
            await api.registrar_escalado(
                wa_id   = wa_id,
                motivo  = args["motivo"],
                mensaje = args["mensaje"],
                nombre  = sesion.get("nombre", "")
            )
            return json.dumps({"ok": True, "escalado": True})

        else:
            return json.dumps({"error": f"Herramienta '{nombre}' no reconocida"})

    except Exception as e:
        return json.dumps({"error": str(e)})


# -- Funcion principal

async def procesar_mensaje(session_id: str, wa_id: str, texto: str,
                            nombre: str, rest_config: dict,
                            api: ApiClient) -> str:
    """
    Procesa un mensaje de WhatsApp y retorna la respuesta del bot.
    session_id = "PHONE_NUMBER_ID:wa_id"
    """
    config_pub = {}
    try:
        config_pub = await api.get_config_publico()
        bot_activo = config_pub.get("bot_activo", True)
    except Exception:
        bot_activo = True

    if not bot_activo:
        tel = rest_config.get("tel", "")
        return (
            "El bot esta temporalmente pausado.\n"
            f"Para consultas o reservas contactanos directamente al {tel}."
        )

    sesion = _get_sesion(session_id)
    sesion["updated"] = datetime.utcnow()

    # Modo presencial
    try:
        flujo = await api.get_flujo_config()
        clave_presencial = flujo.get("palabra_clave_presencial", "*mesa*").strip()
    except Exception:
        clave_presencial = "*mesa*"

    if texto.strip().lower() == clave_presencial.lower():
        sesion["canal"] = "Presencial"
        respuesta = (
            "Modo presencial activado.\n"
            "Hola, estoy listo para ayudarte con la reserva desde el local.\n"
            "Para cuantas personas y que fecha/hora tienes en mente?"
        )
        sesion["messages"].append({"role": "user",      "content": "[Modo presencial activado]"})
        sesion["messages"].append({"role": "assistant", "content": respuesta})
        return respuesta

    # Reconocimiento del cliente
    es_cliente_conocido = False
    if nombre and nombre != wa_id:
        if not sesion.get("nombre"):
            sesion["nombre"]    = nombre
            es_cliente_conocido = True
        nombre_sesion = sesion["nombre"]
    else:
        nombre_sesion = sesion.get("nombre", "")

    sesion["messages"].append({"role": "user", "content": texto})

    if len(sesion["messages"]) > 20:
        sesion["messages"] = sesion["messages"][-20:]

    system_base    = _build_system_prompt(rest_config)
    system_dynamic = system_base + _contexto_dinamico(rest_config, nombre_sesion, es_cliente_conocido, config_pub)

    max_iteraciones = 5
    for _ in range(max_iteraciones):
        response = await client.messages.create(
            model      = MODEL,
            max_tokens = 1024,
            system     = system_dynamic,
            tools      = TOOLS,
            messages   = sesion["messages"]
        )

        if response.stop_reason == "tool_use":
            sesion["messages"].append({
                "role":    "assistant",
                "content": response.content
            })

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    resultado = await ejecutar_herramienta(
                        block.name, block.input,
                        session_id, wa_id, rest_config, api
                    )
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     resultado
                    })

            sesion["messages"].append({"role": "user", "content": tool_results})
            continue

        texto_respuesta = ""
        for block in response.content:
            if hasattr(block, "text"):
                texto_respuesta += block.text

        sesion["messages"].append({
            "role":    "assistant",
            "content": texto_respuesta
        })

        texto_final = texto_respuesta.strip()
        if not texto_final:
            texto_final = "Disculpa, no entendi bien. Puedes repetirlo?"
        return texto_final

    return "Lo siento, tuve un problema procesando tu mensaje. Por favor intenta de nuevo."
