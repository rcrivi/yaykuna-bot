"""
Núcleo del agente Yaykuna.
Maneja el historial de conversaciones por cliente y coordina
las llamadas a Claude con las herramientas de la API de reservas.
"""
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import anthropic
from . import api_client

CHILE_TZ = ZoneInfo("America/Santiago")


def _ahora_chile() -> datetime:
    return datetime.now(CHILE_TZ)


def _restaurante_abierto(ahora: datetime) -> bool:
    """True si el restaurante está en horario de atención."""
    minutos = ahora.hour * 60 + ahora.minute
    apertura = 12 * 60 + 30   # 12:30
    cierre   = 17 * 60 if ahora.weekday() == 6 else 23 * 60  # Dom 17:00 / resto 23:00
    return apertura <= minutos < cierre


def _contexto_dinamico(nombre_cliente: str = "", es_conocido: bool = False) -> str:
    """Genera el bloque de contexto actual (hora, estado restaurante, cliente)."""
    ahora     = _ahora_chile()
    dias      = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
    dia       = dias[ahora.weekday()]
    es_dom    = ahora.weekday() == 6
    horario   = "12:30–17:00 hrs (Domingo)" if es_dom else "12:30–23:00 hrs"
    abierto   = _restaurante_abierto(ahora)
    estado    = "✅ ABIERTO" if abierto else "🔴 CERRADO"
    hora_abre = "12:30 hrs"
    hora_cierre = "17:00 hrs" if es_dom else "23:00 hrs"

    ctx = f"""
---
## CONTEXTO ACTUAL (se actualiza en cada mensaje)
- **Fecha:** {dia} {ahora.strftime('%d/%m/%Y')}
- **Hora Chile:** {ahora.strftime('%H:%M')} hrs
- **Horario hoy:** {horario}
- **Restaurante:** {estado}
- **Abre:** {hora_abre} · **Cierra:** {hora_cierre}
"""
    if nombre_cliente and es_conocido:
        ctx += f"- **Cliente identificado:** {nombre_cliente} — ya nos escribió antes, salúdalo por su nombre al inicio de la conversación.\n"
    elif nombre_cliente:
        ctx += f"- **Nombre del cliente:** {nombre_cliente}\n"

    return ctx

# ── Cliente Anthropic ─────────────────────────────────────────
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL  = "claude-haiku-4-5-20251001"

# ── Sesiones en memoria (wa_id → {messages, nombre, idioma, updated}) ──
_sesiones: dict[str, dict] = {}
SESSION_TTL_HORAS = 4  # Limpia sesión inactiva tras 4 horas

# ── Cache de estado del bot ───────────────────────────────────
_bot_activo_cache: dict = {"activo": True, "updated": None}
BOT_ACTIVO_TTL = 60  # Refresca estado cada 60 segundos


async def _bot_esta_activo() -> bool:
    """Verifica si el bot está activo consultando la API. Cache de 60s."""
    from datetime import datetime, timedelta
    cache = _bot_activo_cache
    if cache["updated"] and datetime.utcnow() - cache["updated"] < timedelta(seconds=BOT_ACTIVO_TTL):
        return cache["activo"]
    try:
        data = await api_client.get_config_publico()
        cache["activo"]  = data.get("bot_activo", True)
        cache["updated"] = datetime.utcnow()
    except Exception:
        pass  # Si falla la API, dejamos el último valor conocido
    return cache["activo"]


def _limpiar_sesiones_viejas():
    limite = datetime.utcnow() - timedelta(hours=SESSION_TTL_HORAS)
    viejas = [k for k, v in _sesiones.items() if v["updated"] < limite]
    for k in viejas:
        del _sesiones[k]


def _get_sesion(wa_id: str) -> dict:
    _limpiar_sesiones_viejas()
    if wa_id not in _sesiones:
        _sesiones[wa_id] = {
            "messages": [],
            "nombre":   "",
            "idioma":   "es",
            "updated":  datetime.utcnow(),
        }
    return _sesiones[wa_id]


# ── System Prompt bilingüe ────────────────────────────────────
NOMBRE       = os.getenv("RESTAURANTE_NOMBRE",    "Yaykuna — La Cocina Maestra")
DIRECCION    = os.getenv("RESTAURANTE_DIRECCION", "Del Parque 76, Santiago, Chile")
TEL          = os.getenv("RESTAURANTE_TEL",       "+56 9 4649 1245")
IG           = os.getenv("RESTAURANTE_IG",        "@yaykuna_restaurante")
WA_LOCAL     = os.getenv("RESTAURANTE_WA_ID",     "")  # wa_id del número del local para notificaciones

SYSTEM_PROMPT = f"""
Eres el asistente virtual de **{NOMBRE}**, restaurante de cocina peruana auténtica ubicado en {DIRECCION}, Santiago, Chile.
Tu nombre es **Yaykuna Bot** 🍽️

---
## PERSONALIDAD
- Amable, cercano y eficiente
- Lenguaje simple, sin exceso de formalidad
- **Detecta el idioma del cliente** (español o inglés) y responde siempre en ese idioma
- Nunca inventes información — si no sabes algo, dilo con honestidad
- Usa emojis con moderación para dar calidez

---
## INFORMACIÓN DEL RESTAURANTE
- **Nombre:** {NOMBRE}
- **Dirección:** {DIRECCION}
- **Teléfono:** {TEL}
- **Instagram:** {IG}
- **Horarios:** Lunes a Sábado 12:30–23:00 hrs · Domingo 12:30–17:00 hrs

---
## CARTA COMPLETA (precios en pesos chilenos CLP)

### 🌟 Sugerencias del Chef
- Sushi Fusion (mango, camarones, palta) — $18.990
- Maki al Gratín (queso crema, palta, kanikama, jaiba) — $18.990
- Maki Acevichado (camarón crocante, queso crema, palta, bocaditos de ceviche) — $18.990
- Culico Chop (maki crocante, ceviche de pescado y camarón) — $19.990
- Mis Tres Causas (palta al olivo, camarón-jaiba, pollo) — $22.990
- Ceviche Afrodisiaco ⭐ (pescado, camarón, calamar en tinta, ostiones parmesanos) — $24.990
- Filete Bárbaro (ternero, champiñones, relleno parmesana, papas fritas) — $22.990
- Filete Pachamanquero 400g (huacatay, camarones, pulpo, risotto betarraga) — $22.990
- Filete Afrodisiaco (pescado, pulpo, maracuyá, camarones apanados) — $19.990
- Tiradito Oriental (atún sellado, especias orientales, wantón) — $17.990
- Fetuccini del Chef (salmón, alcaparras, champiñones) — $17.990
- Filete Pisco Mar 400g (camarones al pisco, ají amarillo, risotto norteño) — $22.990

### 🍤 Festival de Piqueos
- Piqueo Frutos del Mar (pulpo, tiradito, chicharrón, ceviche, calamar) — $29.990
- Jalea Especial para 2 (pescado, mariscos, ceviche, papas fritas) — $29.990
- Causa Limeña (palta, salsa huancaína) — $14.990
- Causa al Panko (pulpo, jaiba, camarones fritos) — $15.990
- Leche de Tigre — $11.990
- Leche de Tigre a la Criolla (ají amarillo, chicharrones) — $14.990
- Pulpo al Olivo (salsa oliva, palta, crackers) — $15.990

### 🥗 Entradas Frías — Ceviches
- Ceviche el Seductor (salsa blanca de ostras y camarones) — $16.990
- Causa Acevichada para 2 (pescado, camarones, calamar, pulpo al pisco) — $14.990
- Ceviche Frutos del Mar (huacatay, ají limón) — $16.990
- Ceviche de Pescado ⭐ (clásico, limón, cebolla, choclo, camote) — $15.990
- Ceviche Fresco Mar (toro, camarones, pollo) — $16.990
- Ceviche Mixto (pescado, mariscos, camarones) — $16.500

### 🔥 Entradas Calientes
- Camarones Tropicales con Puré de Camote (maracuyá) — $13.990
- Ostiones a la Parmesana ⭐ (flambeados, queso parmesano) — $17.990
- Chicharrón de Camarones (apanados, papas fritas, tártara) — $15.990
- Chicharrón Mixto (pescado, camarones, mariscos) — $12.990
- Caldillo de Congrio, Almejas y Choros — $10.990
- Sudado Especial Yaykuna con Camarones — $12.990
- Parihuela Limeña con Mariscos — $18.990

### 🥩 Parrilladas
- Megaparrillada Yaykuna (5 pers.) — $74.990
- Parrillada Memorable (3 pers.) — $58.000
- Parrillada del Valle (2 pers.) — $48.000
- Parrillada Mar Adentro (2 pers.) — $45.000

### 🔪 Cortes Premium a las Brasas
- Entrecot 700g — $25.990 · Lomo Vetado 400g — $22.990 · Lomo Liso 400g — $20.990
- Cuadril de Lomo 400g — $22.990 · Entraña Fina 400g — $25.990
- Pechuga a la Brasa 350g — $13.990 · Chuleta de Centro (2 un.) — $13.990
- Pulpo a la Brasa para 2 — $24.990 · Salmón a la Parrilla 350g — $18.990
- Atún Rojo Parrillero 350g — $19.950 · Brochetas Mixtas (3 un.) — $19.990
- Filet Mignon a lo Macho 400g — $20.990 · Entraña al Grill 400g — $24.990

### 🐟 Mar & Pescados
- Salmón Pizzaiola — $15.990 · Salmón a la Menière — $15.990
- Congrio en Salsa de Mariscos — $16.500 · Reineta a la Francesa — $15.990
- Salmón Yaykuna ⭐ (camarones, pulpo, bechamel) — $15.990
- Atún Saltado con Risotto al Cilantro — $16.200 · Filete Mar Adentro — $16.200

### 🍚 Arroces, Risottos & Pastas
- Chaufa Tapadito con Tortilla de Camarones — $15.300
- Risotto en su Tinta de Calamar ⭐ — $15.990
- Chaufa Mar y Tierra — $15.990 · Chaufa de Pollo — $14.500
- Arroz con Mariscos ⭐ — $15.990 · Risotto con Camarones — $15.990
- Lomo Saltado ⭐ — $16.990 · Fetuccini con Lomito Saltado — $15.990
- Ravioles con Jaiba y Queso Ricota — $15.990 · Volcán de Mariscos — $15.990

### 🍮 Postres & Cafetería
- Cheesecake de Arándano ⭐ — $7.990 · Brownie Chocolate y Nueces — $7.990
- Tiramisú ⭐ — $7.990 · Suspiro Limeño — $7.990 · Torta de Tres Leches — $7.990
- Degustación de Postres — $13.990 · Helados (1 porción) — $3.990
- Café Expreso — $3.200 · Capuchino — $4.500 · Chocolate — $3.200

### 🍹 Cócteles de Autor Yaykuna ($9.200 c/u)
Yaykuna Tropical · Chabelita · Al Rojo Vivo · Flor de Verano · Simón el Seductor
Gin Karibeño · Orgullo Amazónico · La Soñada ⭐ · Mai Tai · Atardecer del Valle
Mocktails: El Regalón · Niña Bonita · La Garota

### 🍺 Aperitivos & Cócteles Clásicos
- Sour Sabores — $9.200 · Pisco Sour Clásico — $9.990 · Piña Colada — $8.990
- Mojito Clásico — $7.500 · Manhattan — $7.500 · Daiquirí — $7.500
- Sours para llevar: 1 litro $17.900 · Medio litro $9.990

### 🥃 Piscos (clásico / cátedra / vaticano)
- Macerados (Coca, Arándanos, Romero/Frutilla, Aguaymanto): $7.990 / $9.200 / $14.990
- Piscos Puros y Blends: desde $7.990

### 🍺 Bebidas & Cervezas
- Aguas, Jugos, Bebidas: $2.990 – $5.990
- Cervezas (Cusqueña, Heineken, Corona, Kunstmann): $4.990 – $5.990

### 🍷 Vinos (selección)
- Espumantes Viña Mar: $15.990 · Por copa: $5.990
- Blancos y Tintos Reserva: $14.990 – $21.990
- Grandes vinos: desde $32.900

---
## CARTA DIGITAL
Cuando el cliente pida la carta, el menú, los platos o los precios:
1. Llama SIEMPRE a la herramienta `enviar_carta` — esta envía botones interactivos al cliente
2. Luego responde con un texto corto, por ejemplo: "¿Te ayudo con algo en especial o quieres hacer una reserva? 😊"
NUNCA escribas las URLs en el texto del mensaje — usa la herramienta.

---
## TUS CAPACIDADES

1. **Reservar mesa** — verificar disponibilidad y confirmar al instante
2. **Pedido para llevar (takeaway)** — armar el carrito conversacionalmente y confirmar el pedido
3. **Responder sobre carta y precios** — toda la info está arriba
4. **Compartir la carta digital** — https://yaykuna.cl/carta.html cuando el cliente la pida
5. **Buscar reservas existentes** — por número de teléfono del cliente
6. **Ver estado de pedido** — el cliente puede consultar sus pedidos recientes
7. **Cancelar reservas** — el cliente cancela con su número de teléfono
8. **Info del restaurante** — horarios, dirección, estacionamiento, etc.

---
## REGLAS DE PEDIDOS PARA LLEVAR

- Solo pedidos **para retirar en el local** — no hacemos delivery
- Recoge conversacionalmente: qué quiere pedir → confirmar ítems y cantidades → pedir nombre y teléfono → confirmar total → crear pedido
- **SIEMPRE confirmar el resumen** con el cliente antes de llamar `crear_pedido`
- El pago es **en caja al retirar** — no manejamos pagos online
- Usa los precios exactos de la carta de arriba
- Si el cliente pide algo que no está en la carta, díselo y ofrece alternativas

### ⏰ VALIDACIÓN DE HORARIO (MUY IMPORTANTE)
Antes de aceptar o confirmar cualquier pedido para llevar, revisa el CONTEXTO ACTUAL:

- Si el restaurante está **CERRADO** en este momento:
  → NO digas "listo para retirar en 20-30 minutos"
  → Dile: "En este momento estamos cerrados, abrimos a las 12:30 hrs 🕧"
  → Ofrece opciones: (a) tomar el pedido para tenerlo listo cuando abran, o (b) que llame cuando estén abiertos
  → Si acepta opción (a), registra en las notas del pedido el horario de retiro deseado

- Si el restaurante está **ABIERTO**:
  → Tiempo estimado de preparación: **20-30 minutos**
  → Si el cliente quiere retirar más tarde (ej: "lo paso a buscar en 2 horas"), acéptalo y regístralo en las notas

---
## REGLAS DE RESERVA

- **SIEMPRE** verifica disponibilidad antes de confirmar
- Datos requeridos: nombre completo, email, teléfono, nº de personas, fecha, hora, sector
- Recolecta los datos de forma conversacional, uno a la vez si es necesario
- El sector es: Salón, Terraza, Bar o Privado (pregunta preferencia)
- Una vez confirmada: el cliente recibe la confirmación en el chat Y por email
- Canal siempre se registra como 'WhatsApp'
- Máximo de personas que puede reservar directamente por bot: 20. Más personas → escalar al admin

---
## ESCALADO AL ADMINISTRADOR

Escala cuando:
- El cliente tiene una queja o problema grave
- Pregunta algo fuera de tu alcance (eventos especiales, descuentos corporativos, etc.)
- Solicita más de 20 personas
- Hay cualquier situación sensible o inusual

Mensaje al escalar (español): *"Entiendo tu consulta 🙏 Voy a comunicarme con nuestro equipo y te responderemos a la brevedad. ¡Gracias por tu paciencia!"*

Mensaje al escalar (inglés): *"I understand your request 🙏 I'll connect you with our team and they'll get back to you shortly. Thank you for your patience!"*

---
## FORMATO DE RESPUESTAS

- Respuestas cortas y directas (máximo 3–4 párrafos)
- Usa saltos de línea para legibilidad en WhatsApp
- Nunca uses Markdown complejo (no tablas, no encabezados #)
- Usa *negrita* solo para información clave (hora, fecha, nombre)
- Emojis: con moderación, solo cuando aporten calidez
"""

# ── Herramientas (Tools) para Claude ─────────────────────────
TOOLS = [
    {
        "name": "verificar_disponibilidad",
        "description": "Verifica si hay horarios disponibles para una fecha específica en el restaurante. Úsala SIEMPRE antes de crear una reserva.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {
                    "type": "string",
                    "description": "Fecha en formato YYYY-MM-DD (ej: 2026-05-15)"
                }
            },
            "required": ["fecha"]
        }
    },
    {
        "name": "crear_reserva",
        "description": "Crea y confirma una reserva para el cliente. Solo llamar cuando tengas TODOS los datos requeridos y hayas verificado disponibilidad.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":    {"type": "string",  "description": "Nombre completo del cliente"},
                "email":   {"type": "string",  "description": "Email del cliente"},
                "phone":   {"type": "string",  "description": "Teléfono del cliente"},
                "guests":  {"type": "integer", "description": "Número de personas"},
                "date":    {"type": "string",  "description": "Fecha YYYY-MM-DD"},
                "time":    {"type": "string",  "description": "Hora HH:MM (ej: 20:00)"},
                "sector":  {"type": "string",  "description": "Sector: Salón, Terraza, Bar o Privado"},
                "message": {"type": "string",  "description": "Mensaje o solicitud especial (opcional)"}
            },
            "required": ["name", "email", "phone", "guests", "date", "time", "sector"]
        }
    },
    {
        "name": "buscar_reserva",
        "description": "Busca las reservas existentes de un cliente por su número de teléfono.",
        "input_schema": {
            "type": "object",
            "properties": {
                "telefono": {
                    "type": "string",
                    "description": "Número de teléfono del cliente"
                }
            },
            "required": ["telefono"]
        }
    },
    {
        "name": "cancelar_reserva",
        "description": "Cancela una reserva existente. Requiere el ID de la reserva y el teléfono del cliente para verificación.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reserva_id": {"type": "integer", "description": "ID numérico de la reserva"},
                "telefono":   {"type": "string",  "description": "Teléfono del cliente para verificación"}
            },
            "required": ["reserva_id", "telefono"]
        }
    },
    {
        "name": "crear_pedido",
        "description": (
            "Crea un pedido para llevar (takeaway) cuando el cliente quiere pedir comida para retirar en el local. "
            "Úsala solo cuando tengas TODOS los datos: nombre, teléfono, y al menos un ítem con nombre, precio y cantidad confirmados por el cliente. "
            "El pago es en caja al momento de retirar. Tiempo estimado de espera: 20-30 minutos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre":   {"type": "string",  "description": "Nombre completo del cliente"},
                "telefono": {"type": "string",  "description": "Teléfono del cliente"},
                "items": {
                    "type": "array",
                    "description": "Lista de productos pedidos",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre":   {"type": "string",  "description": "Nombre exacto del plato según la carta"},
                            "precio":   {"type": "integer", "description": "Precio unitario en CLP (sin puntos ni símbolo $)"},
                            "cantidad": {"type": "integer", "description": "Cantidad de unidades"}
                        },
                        "required": ["nombre", "precio", "cantidad"]
                    }
                },
                "notas": {"type": "string", "description": "Instrucciones especiales (opcional, ej: sin cebolla)"}
            },
            "required": ["nombre", "telefono", "items"]
        }
    },
    {
        "name": "ver_mis_pedidos",
        "description": "Muestra los pedidos recientes del cliente (últimas 24h) para que pueda ver el estado de su pedido.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "enviar_carta",
        "description": (
            "Envía la carta del restaurante al cliente como botones interactivos (sin mostrar URLs). "
            "Úsala cuando el cliente pida la carta, el menú, los platos, los precios o quiera ver qué hay para comer. "
            "La herramienta envía dos botones: uno para ver la carta online y otro para descargar el PDF."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "escalar_al_admin",
        "description": "Notifica al equipo del restaurante sobre una consulta que el bot no puede resolver. Úsalo para quejas, eventos especiales, grupos grandes (+20 personas) o situaciones fuera de tu alcance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo":  {"type": "string", "description": "Razón del escalado"},
                "mensaje": {"type": "string", "description": "Mensaje del cliente que originó el escalado"}
            },
            "required": ["motivo", "mensaje"]
        }
    }
]


# ── Ejecutor de herramientas ──────────────────────────────────
async def ejecutar_herramienta(nombre: str, args: dict, wa_id: str) -> str:
    """Ejecuta una herramienta y retorna el resultado como string."""
    try:
        if nombre == "verificar_disponibilidad":
            data = await api_client.get_disponibilidad(args["fecha"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "crear_reserva":
            # 1. Crear en estado pending
            reserva = await api_client.crear_reserva_publica(args)
            reserva_id = reserva.get("id")
            # 2. Confirmar inmediatamente
            if reserva_id:
                try:
                    await api_client.confirmar_reserva(reserva_id)
                    reserva["status"] = "confirmed"
                except Exception as e:
                    print(f"[Bot] No se pudo confirmar reserva {reserva_id}: {e}")
            return json.dumps(reserva, ensure_ascii=False)

        elif nombre == "buscar_reserva":
            data = await api_client.buscar_reserva_por_telefono(args["telefono"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "cancelar_reserva":
            data = await api_client.cancelar_reserva(args["reserva_id"], args["telefono"])
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "crear_pedido":
            sesion  = _get_sesion(wa_id)
            pedido  = await api_client.crear_pedido(
                wa_id    = wa_id,
                nombre   = args["nombre"],
                telefono = args["telefono"],
                items    = args["items"],
                notas    = args.get("notas", ""),
            )
            pedido_id = pedido.get("id")

            # Notificar al número del local por WhatsApp
            if pedido_id and WA_LOCAL:
                try:
                    items_txt = "\n".join(
                        f"  {it['cantidad']}× {it['nombre']} — ${it['precio']:,}".replace(",", ".")
                        for it in args["items"]
                    )
                    total = sum(it["precio"] * it["cantidad"] for it in args["items"])
                    notas_txt = f"\n📝 {args['notas']}" if args.get("notas") else ""
                    aviso = (
                        f"🛍️ *NUEVO PEDIDO #{pedido_id}* (Para llevar)\n"
                        f"👤 {args['nombre']} · 📱 {args['telefono']}\n"
                        f"─────────────────\n"
                        f"{items_txt}\n"
                        f"─────────────────\n"
                        f"💰 *Total: ${total:,}*{notas_txt}\n"
                        f"⏰ Pago en caja al retirar"
                    ).replace(",", ".")
                    from .whatsapp import enviar_mensaje
                    await enviar_mensaje(WA_LOCAL, aviso)
                    await api_client.marcar_pedido_notificado(pedido_id)
                except Exception as e:
                    print(f"[Bot] ⚠️ No se pudo notificar al local: {e}")

            return json.dumps(pedido, ensure_ascii=False)

        elif nombre == "ver_mis_pedidos":
            data = await api_client.ver_mis_pedidos(wa_id)
            return json.dumps(data, ensure_ascii=False)

        elif nombre == "enviar_carta":
            from .whatsapp import enviar_carta_interactiva
            ok = await enviar_carta_interactiva(wa_id)
            return json.dumps({"ok": ok, "enviado": ok})

        elif nombre == "escalar_al_admin":
            sesion = _get_sesion(wa_id)
            await api_client.registrar_escalado(
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


# ── Función principal ─────────────────────────────────────────
async def procesar_mensaje(wa_id: str, texto: str, nombre: str = "") -> str:
    """
    Procesa un mensaje entrante del cliente y retorna la respuesta del bot.
    Mantiene el historial de conversación en memoria.
    nombre: nombre del cliente extraído del payload de Meta.
    """
    # Verificar si el bot está activo (cache de 60s)
    if not await _bot_esta_activo():
        return (
            "⏸️ El bot está temporalmente pausado.\n"
            f"Para consultas o reservas contáctanos directamente al {TEL} 🙏"
        )

    sesion = _get_sesion(wa_id)
    sesion["updated"] = datetime.utcnow()

    # ── Reconocimiento del cliente ────────────────────────────
    # Si tenemos nombre y la sesión es nueva (sin historial), marcarlo como conocido
    es_cliente_conocido = False
    if nombre and nombre != wa_id:
        if not sesion.get("nombre"):
            # Primera vez en esta sesión — puede ser cliente que vuelve
            sesion["nombre"]     = nombre
            es_cliente_conocido  = bool(sesion.get("messages"))  # True si ya tenía historial previo
            # En sesión nueva siempre saludamos por nombre si lo tenemos
            es_cliente_conocido  = True
        nombre_sesion = sesion["nombre"]
    else:
        nombre_sesion = sesion.get("nombre", "")

    # Agregar mensaje del usuario al historial
    sesion["messages"].append({"role": "user", "content": texto})

    # Limitar historial a últimos 20 mensajes (evitar contexto infinito)
    if len(sesion["messages"]) > 20:
        sesion["messages"] = sesion["messages"][-20:]

    # ── System prompt dinámico (hora actual + cliente) ────────
    system_dinamico = SYSTEM_PROMPT + _contexto_dinamico(nombre_sesion, es_cliente_conocido)

    # ── Bucle de agente con herramientas ─────────────────────
    max_iteraciones = 5
    for _ in range(max_iteraciones):
        response = await client.messages.create(
            model      = MODEL,
            max_tokens = 1024,
            system     = system_dinamico,
            tools      = TOOLS,
            messages   = sesion["messages"]
        )

        # Si Claude quiere usar herramientas
        if response.stop_reason == "tool_use":
            # Agregar respuesta de Claude al historial
            sesion["messages"].append({
                "role":    "assistant",
                "content": response.content
            })

            # Ejecutar cada herramienta solicitada
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    resultado = await ejecutar_herramienta(block.name, block.input, wa_id)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     resultado
                    })

            # Agregar resultados al historial y continuar el bucle
            sesion["messages"].append({
                "role":    "user",
                "content": tool_results
            })
            continue

        # Claude terminó — extraer texto de respuesta
        texto_respuesta = ""
        for block in response.content:
            if hasattr(block, "text"):
                texto_respuesta += block.text

        # Guardar respuesta del bot en historial
        sesion["messages"].append({
            "role":    "assistant",
            "content": texto_respuesta
        })

        return texto_respuesta.strip()

    # Si se agotaron las iteraciones (no debería pasar)
    return "Lo siento, tuve un problema procesando tu mensaje. Por favor intenta de nuevo 🙏"
