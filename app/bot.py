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


def _build_system_prompt(rest_config: dict) -> str:
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
        carta_seccion = (
            "\n## CARTA DIGITAL\n"
            "Cuando el cliente pida la carta o el menu, usa la herramienta `enviar_carta`.\n"
            f"URL de carta: {carta_url}\n"
        )

    menu_seccion = (
        "\n## CARTA\n"
        "REGLAS CRITICAS PARA BUSCAR EN LA CARTA:\n"
        "1. Los platos pueden estar escritos en mayusculas, minusculas o mixto — son equivalentes.\n"
        "2. El cliente puede escribir solo parte del nombre (ej: 'chaufa' o 'fetuccini huancaina') — busca coincidencias parciales en TODAS las secciones.\n"
        "3. NUNCA digas que un plato no existe sin antes haber revisado TODAS las secciones de la carta.\n"
        "4. Si el cliente escribe una palabra clave, lista TODOS los platos que la contengan.\n"
        "5. Si no encuentras el plato exacto, sugiere los mas parecidos que SI esten en la carta.\n\n"
        f"{menu_txt}\n"
    ) if menu_txt else ""

    return (
        f"Eres el asistente virtual de **{nombre}**, ubicado en {direccion}.\n"
        f"Tu nombre es **{nombre} Bot**.\n\n"
        "---\n"
        "## PERSONALIDAD Y FORMA DE HABLAR\n"
        "- Habla de tu, nunca de usted. Tono cercano y caloroso, como un anfitrion real del local.\n"
        "- Responde como lo haria una persona: breve cuando el cliente es breve, mas completo cuando lo necesita.\n"
        "- NUNCA abras la conversacion listando opciones o capacidades numeradas. Espera que el cliente diga que necesita.\n"
        "- Si el cliente manda solo 'hola' o un saludo, responde con algo corto y caloroso: 'Hola! Como te puedo ayudar?' o 'Buenas! Que necesitas?' -- sin parrafos.\n"
        "- Usa expresiones naturales y varia siempre: 'Dale', 'Perfecto!', 'Claro que si', 'Te anoto', 'Listo!', 'Con gusto', 'Ya te lo registro'.\n"
        "- No uses siempre la misma frase para confirmar o despedirte -- varia cada vez.\n"
        "- Detecta el idioma del cliente (espanol o ingles) y responde en ese idioma.\n"
        "- Si no sabes algo, dilo con naturalidad: 'Eso no lo tengo claro, pero te puedo ayudar con...'.\n"
        "- Usa emojis solo cuando sumen calidez al mensaje, no en cada respuesta.\n\n"
        "---\n"
        "## INFORMACION DEL RESTAURANTE\n"
        f"- **Nombre:** {nombre}\n"
        f"- **Direccion:** {direccion}\n"
        f"- **Telefono:** {tel}\n"
        f"- **Instagram:** {ig}\n"
        f"- **Horarios:** {horarios}\n\n"
        "---\n"
        + menu_seccion
        + carta_seccion +
        "---\n"
        "## TUS CAPACIDADES\n"
        "1. **Reservar mesa** -- verificar disponibilidad y confirmar al instante\n"
        "2. **Pedidos para llevar (takeaway / retiro en local)** -- tomar el pedido del cliente\n"
        "3. **Responder sobre carta y precios**\n"
        "4. **Buscar reservas existentes** -- por numero de telefono\n"
        "5. **Cancelar reservas**\n"
        "6. **Info del restaurante** -- horarios, direccion, etc.\n\n"
        "---\n"
        "## PEDIDOS PARA LLEVAR\n"
        "- El restaurante SI acepta pedidos para llevar (takeaway / retiro en local).\n"
        "- FLUJO CORTO -- maximo 2 intercambios para cerrar un pedido simple:\n"
        "  1. El cliente dice que quiere pedir (y lo que quiere).\n"
        "  2. Confirmas los items y registras el pedido. Listo.\n"
        "- YA TIENES el nombre y telefono del cliente en tu contexto de sesion.\n"
        "  NO los vuelvas a pedir. Usaos directamente al llamar crear_pedido.\n"
        "- Si el cliente no menciona hora de retiro, calcula hora_actual + 30 minutos y usala sin preguntar.\n"
        "- Al confirmar, muestra un resumen breve: items, total y hora de retiro.\n"
        f"- Si el sistema falla, deriva al {tel}.\n"
        "- NUNCA digas que no hacemos pedidos -- SI los hacemos.\n\n"
        "---\n"
        "## REGLAS DE RESERVA\n"
        "- Verifica SIEMPRE disponibilidad antes de confirmar\n"
        "- Datos requeridos: nombre completo, email, telefono, personas, fecha, hora, sector\n"
        "- Recolecta los datos de forma conversacional\n"
        "- El sector puede ser: Salon, Terraza, Bar o Privado\n"
        "- Canal siempre se registra como 'WhatsApp'\n"
        "- Maximo 20 personas -- mas personas, escalar al admin\n\n"
        "---\n"
        "## ESCALADO AL ADMINISTRADOR\n"
        "Escala cuando:\n"
        "- El cliente tiene una queja grave\n"
        "- La consulta esta fuera de tu alcance\n"
        "- Solicita mas de 20 personas\n\n"
        "Al escalar, usa un mensaje natural y distinto cada vez -- nunca la misma frase repetida.\n"
        "Adapta el tono segun la situacion. Ejemplos de como sonar (no copies literal):\n"
        "  'Eso lo tiene que ver alguien del equipo, ya les aviso para que te contacten!'\n"
        "  'Te paso con el equipo del local, ellos te pueden ayudar mejor con eso 👌'\n"
        "  'Eso queda fuera de lo que puedo resolver, pero le aviso a alguien ahora mismo.'\n"
        "  'Perfecto, deja que le pase tu mensaje al equipo y te responden a la brevedad.'\n\n"
        "---\n"
        "## PEDIDOS — REGLAS CRITICAS\n"
        "- Cuando crear_pedido retorne exitosamente, SIEMPRE debes decirle al cliente su numero de pedido.\n"
        "  El numero viene en el campo 'id' del resultado. Ejemplo: 'Tu numero de pedido es el #42'.\n"
        "- NUNCA digas que el sistema no genero numero de pedido. El id SIEMPRE viene en la respuesta.\n"
        "- Resume el pedido: items, total y hora de retiro.\n\n"
        "## PAGOS Y TRANSFERENCIA\n"
        "- Si el resultado de crear_pedido incluye 'requiere_transferencia: true',\n"
        "  DEBES informar al cliente que por el monto del pedido se requiere transferencia bancaria.\n"
        "- Entrega los datos_transferencia que vienen en el resultado del pedido.\n"
        "- Indica que el pedido quedara pendiente hasta confirmar el pago.\n"
        "- El cliente puede tambien pagar en caja al retirar (si el local lo permite).\n\n"
        "## COMPROBANTES DE TRANSFERENCIA\n"
        "- Si el cliente te envia una imagen, analízala visualmente.\n"
        "- Si es un comprobante de transferencia bancaria:\n"
        "  PASO 1 — Extrae del comprobante: monto_comprobante, destinatario y estado.\n"
        "  PASO 2 — El contexto [SISTEMA] ya incluye el pedido automaticamente con su total.\n"
        "    Usa el pedido_id y total que vienen en [SISTEMA]. No preguntes al cliente.\n"
        "  PASO 3 — Verifica el monto: compara monto_comprobante con total del pedido en [SISTEMA].\n"
        "    SI monto_comprobante >= total del pedido Y destinatario es correcto:\n"
        "      Llama a marcar_transferencia_ok con pedido_id y monto_verificado=monto_comprobante.\n"
        "      Responde: 'Comprobante recibido ✅ Verificamos $XX.XXX. Tu pedido #NN quedo registrado. Te avisamos cuando este listo!'\n"
        "    SI monto_comprobante < total del pedido:\n"
        "      Responde: 'El monto del comprobante ($XX.XXX) no coincide con el total de tu pedido ($YY.YYY). "
        "Por favor verifica y reenvía el comprobante correcto.'\n"
        "      NO llames marcar_transferencia_ok.\n"
        "    SI el destinatario no corresponde al restaurante:\n"
        "      Responde: 'El comprobante parece ser de una transferencia a otro destinatario. "
        "Por favor reenvía el comprobante correcto a nombre de [nombre del restaurante].'\n"
        "      NO llames marcar_transferencia_ok.\n"
        "  PASO 4 — Si [SISTEMA] indica que no hay pedido o pide el numero: pregunta '¿Cual es el numero de tu pedido?'\n"
        "- Si la imagen NO es un comprobante de transferencia, responde normalmente.\n\n"
        "---\n"
        "## FORMATO DE RESPUESTAS\n"
        "- Respuestas cortas y directas (maximo 3-4 parrafos)\n"
        "- Sin Markdown complejo (no tablas, no encabezados #)\n"
        "- Emojis con moderacion\n"
        "- NUNCA uses asteriscos (*) en tus respuestas. Ni para negritas, ni para listas, ni para URLs. Texto plano siempre.\n"
        "- Ejemplo INCORRECTO: *Chaufa de Pollo* o **$14.500**\n"
        "- Ejemplo CORRECTO: Chaufa de Pollo $14.500\n"
    )


def _contexto_dinamico(rest_config: dict, nombre_cliente: str = "",
                        es_conocido: bool = False,
                        config_pub: dict = None,
                        wa_id: str = "") -> str:
    tz_str = rest_config.get("zona_horaria", "America/Santiago")
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("America/Santiago")

    ahora = datetime.now(tz)
    dias  = ["Lunes","Martes","Miercoles","Jueves","Viernes","Sabado","Domingo"]
    dia   = dias[ahora.weekday()]

    ctx = (
        "\n---\n"
        "## CONTEXTO ACTUAL\n"
        f"- **Fecha:** {dia} {ahora.strftime('%d/%m/%Y')}\n"
        f"- **Hora local:** {ahora.strftime('%H:%M')} hrs\n"
    )

    if config_pub:
        hora_cierre_hoy = config_pub.get("hora_cierre_hoy")
        pedidos_hoy     = config_pub.get("pedidos_hoy", True)
        if hora_cierre_hoy:
            ya_cerrado = False
            try:
                h_cierre, m_cierre = [int(x) for x in hora_cierre_hoy.split(":")]
                ya_cerrado = (ahora.hour, ahora.minute) >= (h_cierre, m_cierre)
            except Exception:
                pass

            if ya_cerrado:
                ctx += f"- **AVISO HOY:** El restaurante cerro a las {hora_cierre_hoy} hrs por evento especial.\n"
                ctx += "- No ofrecer reservas ni pedidos para HOY MISMO -- ya cerramos.\n"
                ctx += "- Para reservas futuras (desde manana) si puedo ayudar normalmente.\n"
                ctx += "- Para pedidos futuros derivar al telefono del restaurante.\n"
            else:
                ctx += f"- **AVISO HOY:** El restaurante cierra a las {hora_cierre_hoy} hrs por evento especial.\n"
                ctx += f"- Reservas y pedidos disponibles solo hasta las {hora_cierre_hoy} hrs hoy.\n"
                if not pedidos_hoy:
                    ctx += "- Pedidos para llevar NO disponibles hoy -- evento especial con cocina ocupada.\n"

    if nombre_cliente and es_conocido:
        ctx += f"- **Cliente:** {nombre_cliente} -- ya nos escribio antes, saludalo por nombre.\n"
    elif nombre_cliente:
        ctx += f"- **Nombre del cliente:** {nombre_cliente}\n"

    # Datos de sesion disponibles para pedidos (el sistema los usa como fallback)
    if wa_id:
        ctx += f"- **Telefono sesion (wa_id):** {wa_id} -- usar como telefono en crear_pedido si el cliente no da otro.\n"
    if nombre_cliente:
        ctx += f"- **Nombre sesion:** {nombre_cliente} -- usar como nombre en crear_pedido sin preguntarlo.\n"

    return ctx


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
        "name": "crear_pedido",
        "description": "Registra un pedido para llevar (takeaway). Llamar en cuanto el cliente confirme los items. nombre y telefono son opcionales -- el sistema los completa automaticamente desde la sesion, NO los pidas al cliente. hora_retiro tambien es opcional -- si no la menciono el cliente, omitela y el sistema calculara hora_actual+30min.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":      {"type": "string",  "description": "Opcional -- se toma de la sesion si no se provee"},
                "telefono":    {"type": "string",  "description": "Opcional -- se toma del numero WhatsApp si no se provee"},
                "hora_retiro": {"type": "string",  "description": "Opcional -- hora estimada de retiro ej: 13:00. Si el cliente no la menciono, omitir."},
                "items": {
                    "type": "array",
                    "description": "Lista de productos pedidos",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre":   {"type": "string",  "description": "Nombre del plato"},
                            "precio":   {"type": "integer", "description": "Precio unitario en pesos"},
                            "cantidad": {"type": "integer", "description": "Cantidad"}
                        },
                        "required": ["nombre", "precio", "cantidad"]
                    }
                },
                "notas": {"type": "string", "description": "Observaciones adicionales del cliente"}
            },
            "required": ["items"]
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
        "name": "marcar_transferencia_ok",
        "description": "Registra que el comprobante de transferencia de un pedido fue verificado visualmente. Llamar SOLO cuando hayas analizado la imagen y el comprobante sea válido (monto y destinatario correctos).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pedido_id":       {"type": "integer", "description": "ID del pedido al que corresponde el comprobante"},
                "monto_verificado": {"type": "integer", "description": "Monto en pesos (CLP) que aparece en el comprobante"}
            },
            "required": ["pedido_id", "monto_verificado"]
        }
    },
    {
        "name": "buscar_pedido_pendiente",
        "description": "Busca el pedido pendiente mas reciente del cliente actual (el que esta en la conversacion). Usar cuando llega un comprobante de transferencia pero no hay pedido_id en el historial de la conversacion.",
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


async def ejecutar_herramienta(nombre: str, args: dict,
                                session_id: str, wa_id: str,
                                rest_config: dict, api: ApiClient,
                                flujo_config: dict = None) -> str:
    flujo_config = flujo_config or {}
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

        elif nombre == "crear_pedido":
            # Calcular total del pedido para verificar si requiere transferencia
            items = args.get("items", [])
            total = sum(
                int(it.get("precio", 0)) * int(it.get("cantidad", 1))
                for it in items
            )
            monto_minimo = int(flujo_config.get("monto_transferencia", 0))
            datos_transf = flujo_config.get("datos_transferencia", "").strip()

            # Fallbacks: nombre y telefono desde sesion si Claude no los proveyó
            sesion = _get_sesion(session_id)
            nombre_cliente = args.get("nombre") or sesion.get("nombre") or "Cliente"
            telefono_cliente = args.get("telefono") or wa_id

            # hora_retiro: si Claude no la calculó, usar hora_actual + 30 min
            hora_retiro = args.get("hora_retiro", "")
            if not hora_retiro:
                try:
                    tz_str = rest_config.get("zona_horaria", "America/Santiago")
                    from zoneinfo import ZoneInfo
                    ahora = datetime.now(ZoneInfo(tz_str))
                    from datetime import timedelta
                    retiro = ahora + timedelta(minutes=30)
                    hora_retiro = retiro.strftime("%H:%M")
                except Exception:
                    hora_retiro = "a convenir"

            notas_extra = args.get("notas", "")
            notas = f"Retiro: {hora_retiro}" + (f" | {notas_extra}" if notas_extra else "") if hora_retiro else notas_extra

            print(f"[Bot] crear_pedido → wa_id={wa_id}, nombre={nombre_cliente}, tel={telefono_cliente}, items={len(items)}, notas='{notas[:40]}'")
            try:
                data = await api.crear_pedido(
                    wa_id    = wa_id,
                    nombre   = nombre_cliente,
                    telefono = telefono_cliente,
                    items    = items,
                    notas    = notas,
                )
                print(f"[Bot] crear_pedido ← OK: {data}")
            except Exception as api_err:
                print(f"[Bot] crear_pedido ← ERROR {type(api_err).__name__}: {api_err}")
                raise

            # Si el pedido supera el monto configurado, agregar aviso de transferencia
            if monto_minimo > 0 and total >= monto_minimo and datos_transf:
                data["requiere_transferencia"] = True
                data["monto_total"]            = total
                data["datos_transferencia"]    = datos_transf
                data["mensaje"] = (
                    f"Pedido registrado. Por el monto (${total:,}), "
                    "el local requiere pago por transferencia bancaria antes de ingresarlo a cocina."
                )

            return json.dumps(data, ensure_ascii=False)

        elif nombre == "marcar_transferencia_ok":
            pedido_id = int(args.get("pedido_id", 0))
            monto     = int(args.get("monto_verificado", 0))
            if not pedido_id:
                return json.dumps({"error": "pedido_id requerido"})
            data = await api.marcar_transferencia_ok(pedido_id, monto)
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "buscar_pedido_pendiente":
            data = await api.buscar_pedido_pendiente(wa_id)
            return json.dumps(data, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Herramienta '{nombre}' no reconocida"})

    except Exception as e:
        return json.dumps({"error": str(e)})


async def procesar_mensaje(session_id: str, wa_id: str, texto: str,
                            nombre: str, rest_config: dict,
                            api: ApiClient,
                            imagen_b64: str = None,
                            imagen_mime: str = "image/jpeg") -> str:
    config_pub = {}
    try:
        config_pub = await api.get_config_publico()
        bot_activo = config_pub.get("bot_activo", True)
    except Exception:
        bot_activo = True

    if not bot_activo:
        tel = rest_config.get("tel", "")
        return (
            f"Hola! En este momento te atendemos directamente.\n"
            f"Escríbenos al {tel} y te ayudamos de inmediato 😊"
        )

    sesion = _get_sesion(session_id)
    sesion["updated"] = datetime.utcnow()

    flujo = {}
    try:
        flujo = await api.get_flujo_config()
        clave_presencial = flujo.get("palabra_clave_presencial", "*mesa*").strip()
    except Exception:
        clave_presencial = "*mesa*"

    if texto.strip().lower() == clave_presencial.lower():
        sesion["canal"] = "Presencial"
        respuesta = (
            "Hola! Para cuantas personas sería y que fecha tienes en mente?"
        )
        sesion["messages"].append({"role": "user",      "content": "[Modo presencial activado]"})
        sesion["messages"].append({"role": "assistant", "content": respuesta})
        return respuesta

    es_cliente_conocido = False
    if nombre and nombre != wa_id:
        if not sesion.get("nombre"):
            sesion["nombre"]    = nombre
            es_cliente_conocido = True
        nombre_sesion = sesion["nombre"]
    else:
        nombre_sesion = sesion.get("nombre", "")

    # Construir contenido del mensaje (texto simple o imagen + texto)
    if imagen_b64:
        # Pre-buscar el pedido del cliente para inyectarlo como contexto.
        # El bot ya tiene el dato y no necesita decidir si llamar un tool.
        pedido_ctx_txt = ""
        try:
            pedido_ctx  = await api.buscar_pedido_pendiente(wa_id)
            situacion   = pedido_ctx.get("situacion", "error")
            p           = pedido_ctx.get("pedido")

            if situacion == "pendiente" and p:
                total = p.get('total', '?')
                pedido_ctx_txt = (
                    f"\n[SISTEMA — pedido encontrado automaticamente: "
                    f"pedido_id={p['id']}, TOTAL_PEDIDO=${total}, estado=pendiente_de_pago. "
                    f"INSTRUCCION: compara el monto del comprobante con TOTAL_PEDIDO=${total}. "
                    f"Si el monto coincide o es mayor Y el destinatario es correcto, "
                    f"llama marcar_transferencia_ok con pedido_id={p['id']} SIN preguntar nada al cliente. "
                    f"Si el monto es menor, informa la diferencia al cliente.]"
                )
            elif situacion == "ya_pagado" and p:
                pedido_ctx_txt = (
                    f"\n[SISTEMA — ATENCION: el pedido #{p['id']} ya tiene el pago registrado "
                    f"(transferencia_ok=1, monto=${p.get('transferencia_monto', p.get('total','?'))}). "
                    f"NO llames marcar_transferencia_ok. "
                    f"Informa al cliente que su comprobante ya fue recibido anteriormente y el pedido esta confirmado.]"
                )
            elif situacion == "cancelado" and p:
                pedido_ctx_txt = (
                    f"\n[SISTEMA — el pedido mas reciente #{p['id']} esta CANCELADO. "
                    f"Informa al cliente que ese pedido fue cancelado y que si quiere hacer un nuevo pedido con gusto lo ayudas. "
                    f"NO registres el comprobante.]"
                )
            elif situacion == "otro_estado" and p:
                pedido_ctx_txt = (
                    f"\n[SISTEMA — el pedido #{p['id']} esta en estado '{p.get('estado','')}' "
                    f"(ya procesado, no requiere pago por transferencia). "
                    f"Informa al cliente el estado actual y que si tiene dudas puede consultar al equipo.]"
                )
            elif situacion == "sin_pedidos":
                pedido_ctx_txt = (
                    "\n[SISTEMA — este cliente no tiene pedidos registrados. "
                    "Si el comprobante parece valido, pregunta al cliente el numero de pedido (#NN).]"
                )
            else:
                pedido_ctx_txt = (
                    "\n[SISTEMA — no se pudo obtener informacion del pedido. "
                    "Si el comprobante es valido, pregunta al cliente el numero de pedido.]"
                )
        except Exception:
            pass

        texto_base = texto if texto and texto != "[imagen]" else ""
        texto_final = (
            (texto_base + " — " if texto_base else "")
            + "El cliente acaba de enviar esta imagen (posible comprobante de transferencia). "
            + "Analízala y actua segun las instrucciones de COMPROBANTES DE TRANSFERENCIA."
            + pedido_ctx_txt
        )

        user_content = [
            {
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": imagen_mime,
                    "data":       imagen_b64,
                }
            },
            {
                "type": "text",
                "text": texto_final,
            }
        ]
    else:
        user_content = texto

    sesion["messages"].append({"role": "user", "content": user_content})

    if len(sesion["messages"]) > 20:
        sesion["messages"] = sesion["messages"][-20:]

    # Fusionar carta_url y menu desde la API (DB) si no vienen en rest_config (env vars)
    effective_config = dict(rest_config)
    if config_pub.get("carta_url") and not effective_config.get("carta_url"):
        effective_config["carta_url"] = config_pub["carta_url"]
    if config_pub.get("menu") and not effective_config.get("menu"):
        effective_config["menu"] = config_pub["menu"]

    system_base    = _build_system_prompt(effective_config)
    system_dynamic = system_base + _contexto_dinamico(effective_config, nombre_sesion, es_cliente_conocido, config_pub, wa_id)

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
                        session_id, wa_id, effective_config, api,
                        flujo_config=flujo
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
