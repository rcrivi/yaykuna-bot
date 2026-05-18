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

# ── Sesiones en memoria (wa_id → {messages, nombre, idioma, updated, canal}) ──
_sesiones: dict[str, dict] = {}
SESSION_TTL_HORAS = 4  # Limpia sesión inactiva tras 4 horas

# ── Cache de palabra clave presencial ────────────────────────
_presencial_cache: dict = {"clave": "*mesa*", "updated": None}
PRESENCIAL_TTL = 300  # Refresca cada 5 minutos

async def _get_palabra_clave_presencial() -> str:
    """Retorna la palabra clave configurada en el panel (cache de 5 min)."""
    from datetime import datetime, timedelta
    cache = _presencial_cache
    if cache["updated"] and datetime.utcnow() - cache["updated"] < timedelta(seconds=PRESENCIAL_TTL):
        return cache["clave"]
    try:
        data = await api_client.get_flujo_config()
        clave = data.get("palabra_clave_presencial", "*mesa*").strip()
        if clave:
            cache["clave"]   = clave
            cache["updated"] = datetime.utcnow()
    except Exception:
        pass
    return cache["clave"]

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
- Sushi Fusion (mango, camarones, palta, puerro) — $18.990
- Maki al Gratín (queso crema, palta, kanikama, jaiba) — $18.990
- Maki Acevichado (camarón crocante, queso crema, palta, bocaditos de ceviche) — $18.990
- Culico Chop (maki crocante, queso crema, palta, ceviche de pescado y camarón, leche de tigre) — $19.990
- Mis Tres Causas (palta al olivo, camarón-jaiba, pollo) — $22.990
- Ceviche Afrodisiaco ⭐ (pescado, camarón, calamar en tinta, ostiones parmesanos, camarones al pesto) — $24.990
- Filete Bárbaro (ternero a la parrilla, champiñones, relleno parmesana, papas fritas) — $22.990
- Filete Pachamanquero 400g (huacatay, camarones, pulpo, risotto betarraga) — $22.990
- Filete Afrodisiaco (pescado, pulpo, maracuyá, camarones apanados, palta) — $19.990
- Tiradito Oriental (atún sellado, especias orientales, wantón frito) — $17.990
- Fetuccini Inspiración del Chef (salmón parrillero, alcaparras, champiñones) — $17.990
- Filete Pisco Mar 400g (camarones flambeados al pisco, ají amarillo, risotto norteño) — $22.990

### 🍤 Festival de Piqueos
- Piqueo Frutos del Mar (pulpo al olivo, tiradito, chicharrón crocante, ceviche, calamar, bocaditos) — $29.990
- Jalea Especial para 2 (pescado, mariscos, ceviche, papas fritas) — $29.990
- Causa Limeña (palta, salsa huancaína) — $14.990
- Causa al Panko (pulpo, jaiba, camarones fritos al panko) — $15.990
- Leche de Tigre — $11.990
- Leche de Tigre a la Criolla (ají amarillo, chicharrones mixtos) — $14.990
- Pulpo al Olivo (salsa oliva, palta, crackers) — $15.990

### 🥗 Entradas Frías — Ceviches
- Ceviche el Seductor ⭐ (salsa blanca de ostras y camarones) — $16.990
- Causa Acevichada para 2 (pescado, camarones, calamar, pulpo, caldo al pisco, ají amarillo, camote) — $14.990
- Ceviche Frutos del Mar (mariscos, huacatay, ají limón, caldo de pescado) — $16.990
- Ceviche de Pescado ⭐ (clásico, limón, cebolla, choclo, camote) — $15.990
- Ceviche Fresco Mar (toro, camarones, pollo, especias, leche de tigre) — $16.990
- Ceviche Mixto (pescado, mariscos, camarones, camote, choclo) — $16.500

### 🔥 Entradas Calientes
- Camarones Tropicales con Puré de Camote (salsa de maracuyá) — $13.990
- Ostiones a la Parmesana ⭐ (flambeados, queso parmesano) — $17.990
- Chicharrón de Camarones (apanados, papas fritas, tártara) — $15.990
- Chicharrón Mixto (pescado, camarones, mariscos, papas fritas, tártara) — $12.990

### 🍲 Caldos Reponedores
- Caldillo de Congrio, Almejas y Choros (choros, camarones, calamares, tomates, yuzu) — $10.990
- Sudado Especial Yaykuna con Camarones (piñeiros, filete, aceitunas, tomates, yuzu) — $12.990
- Parihuela Limeña con Mariscos (concentrada, pescado, jaiba, salsa madre) — $18.990

### 🥩 Parrilladas
- Megaparrillada Yaykuna (5 pers.) — lomo liso, chorizos, cuadril, pechugas, anticuchos, chuletas, papas doradas, ensalada — $74.990
- Parrillada Memorable (3 pers.) — entraña, chuletas, anticuchos, pechuga, chorizos, presas, champiñón — $58.000
- Parrillada del Valle (2 pers.) — pechuga, lomo liso, entraña, anticucho, chorizos, presas, papas doradas — $48.000
- Parrillada Mar Adentro (2 pers.) — pulpo, calamares, salmón, ostiones, camarones, papas brujas, champiñón — $45.000

### 🔪 Cortes Premium a las Brasas
- Entrecot 700g (lomo fino + filete, ensalada surtida) — $25.990
- Lomo Vetado 400g (ensalada de verduras) — $22.990
- Lomo Liso 400g (guarnición de verduras) — $20.990
- Cuadril de Lomo 400g (ensalada surtida) — $22.990
- Entraña Fina 400g — $25.990
- Pechuga a la Brasa 350g (papas fritas, chimichurri) — $13.990
- Chuleta de Centro (2 unidades, papas fritas, papas doradas) — $13.990
- Pulpo a la Brasa para 2 (ostiones, olivo, salsa triada) — $24.990
- Salmón a la Parrilla 350g (alcaparras, ensalada mixta) — $18.990
- Atún Rojo Parrillero 350g (risotto de ajo) — $19.950
- Brochetas Mixtas (3 un., filete, pulpo, pollo, papas fritas) — $19.990
- Chorizo con Queso Fundido (champiñón) — $19.990
- Filet Mignon a lo Macho 400g (camarón, pulpo, calamar, salsa macho) — $20.990
- Entraña al Grill 400g (parmesano a la parrilla) — $24.990
- Cuadril al Ajo 400g (hambreada con ajo a la brasa) — $22.990
- Garrón de Cordero 400g (estilo peruano, risotto norteño) — $22.990

### 🍖 Especialidades de Carnes y Aves
- Lomo Saltado ⭐ (filete, cebollas, tomates, especias orientales, arroz, papas fritas) — $16.990
- Lostrogonof y Champiñones (filete en salsa cremosa, fideos) — $16.200
- Tacu-Tacu con Lomito al Jugo (poroto y arroz al olivo, lomito al ajo) — $15.990
- Filete a la Pimienta Verde (plancha, salsa pimienta verde, risotto de ajo) — $16.200
- Filete Café de París (gratiado, guarnición de verduras) — $15.990
- Filete Sol y Sombra (plancha, camarones, champiñones, salsa de erizo, arroz blanco) — $15.990
- Ají de Gallina con Arroz Blanco (salsa ají amarillo, queso fresco, huevo, aceitunas) — $14.990

### 🐟 Mar & Pescados
- Salmón Pizzaiola (queso mozzarella, camarones, champiñones, salsa pastiriso) — $15.990
- Salmón a la Menière con Camarones (dorado a la plancha, arroz blanco) — $15.990
- Congrio en Salsa de Mariscos (grillado, dos quesos, ostiones, arroz blanco) — $16.500
- Reineta a la Francesa (camarones, pulpo, champiñones, gratinado dos quesos) — $15.990
- Reineta a la Huancaína (plancha, camarones, pollo, arroz al cilantro) — $15.990
- Salmón Yaykuna ⭐ (plancha, camarones, pulpo, bechamel, arroz blanco) — $15.990
- Salmón Crocante (plancha, risotto de ajo) — $15.990
- Atún Saltado con Risotto al Cilantro (trozos de atún, verduras, palta, tomate) — $16.200
- Salmón a la Silla Miñón (plancha, camarones, champiñones, papitas al merkén, salsa madre) — $16.200
- Selladito de Atún al Estragón (aceite de oliva, camarones, arroz blanco) — $16.200
- Filete Mar Adentro (gratiado, salsa blanca, camarones, champiñones) — $16.200
- Filete Grillado a lo Macho (colas de camarón, tomate, arroz blanco) — $16.200

### 🍚 Arroces, Risottos & Pastas
- Chaufa Tapadito con Tortilla de Camarones (fideos tostados, estilo chifa) — $15.300
- Risotto en su Tinta de Calamar ⭐ — $15.990
- Chaufa Mar y Tierra (pollo, camarones, oriental, verduritas fritas) — $15.990
- Chaufa de Pollo (oriental, camarones fritos) — $14.500
- Arroz con Mariscos ⭐ (vino blanco, mariscos, pollo criollo) — $15.990
- Risotto con Camarones (hongos frescos, camarones, parmesano) — $15.990
- Risotto a la Huancaína (cremoso, lomito saltado) — $15.990
- Cordón Bleu de Vacuno con Jamón y Queso (dos quesos, salsa blanca, champiñones, parmesano) — $16.200
- Fetuccini a la Huancaína con Pulpito Parrillero (plancha, salsa huancaína) — $16.200
- Volcán de Mariscos (fideos, camarones, pulpo, calamares, risotto bechamel, mozzarella) — $15.990
- Ravioles con Jaiba y Queso Ricota (pasta fresca, salsa mariscos, parmesano) — $15.990
- Fetuccini con Lomito Saltado (salsa huancaína, parmesano) — $15.990

### 👶 Menú Kids
- Bistec Ranchero a la Brasa (papas fritas, tomate asado, cebolla) — $13.990
- Bistec a la Plancha (papas fritas) — $13.990
- Pescado a la Plancha (papas fritas naturales, arroz blanco) — $13.990
- Chicharrón de Pollo (papas fritas) — $12.990

### ➕ Agregados
- Arroz Blanco — $3.200
- Papas Fritas Naturales — $6.900
- Ensalada Chilena — $4.990
- Ensalada Mixta de Estación — $5.990
- Porción de Risotto — $8.200

### 🍮 Postres
- Cheesecake de Arándano ⭐ (arándanos frescos, helado artesanal) — $7.990
- Brownie de Chocolate y Nueces (helado vainilla y frutilla, salsa chocolate) — $7.990
- Tiramisú ⭐ (capuchino italiano, queso, cacao) — $7.990
- Suspiro Limeño (canela, vainilla, merengue, almíbar de aguaje) — $7.990
- Torta de Tres Leches (bizcocho, frambuesas) — $7.990
- Crema Volteada (muy suave, cocida a fuego lento) — $7.500
- Panacota de Estación (cuajilla, frutas de estación, cacao) — $7.990
- Degustación de Postres — $13.990
- Helado Natural: 1 porción — $3.990 · 2 porciones con frutilla — $4.990

### ☕ Cafetería Yaykuna
- Café Expreso — $3.200
- Café Expreso Doble — $3.990
- Café Americano — $3.990
- Café Cortado — $3.990
- Capuchino — $4.500
- Chocolate — $3.200
- Infusión de Hierbas — $3.200

### 🍹 Cócteles de Autor Yaykuna ($9.200 c/u)
Yaykuna Tropical · Chabelita · Al Rojo Vivo · Flor de Verano · Simón el Seductor
Gin Karibeño · Orgullo Amazónico · La Soñada ⭐ · Mai Tai · Atardecer del Valle
Mocktails ($9.200): El Regalón · Niña Bonita · La Garota

### 🍸 Aperitivos
- Buenazo Yaykuna (pisco, naranja, maracuyá) — $8.990
- Machuca Fuerte (pisco, frutilla, menta, maracuyá) — $8.990
- Sour Sabores — $9.200
- Algarrobina — $8.990
- Chilcano Clásico y Sabores — $9.990
- Piña Colada — $8.990
- Vaína — $8.990
- Caipiriña — $7.990

### 🥂 Cócteles Clásicos
- Mojito Clásico — $7.500 · Mojito Sabores — $8.500
- Daiquirí Clásico — $7.500 · Daiquirí Sabores — $8.500
- Manhattan — $7.500 · Kir Royal — $7.500
- Amareto Sour Catedral — $9.990 · Whisky Sour Catedral — $9.990
- Jarez Sour Clásico — $7.500 · Martini Dry — $7.990
- Spritz (Aperol, Ramazzotti) — $7.990

### 🥃 Piscos Macerados (Clásico / Cátedra / Vaticano)
- Hoja de Coca: $7.990 / $9.200 / $14.990
- Arándanos: $7.990 / $9.200 / $14.990
- Romero/Frutilla: $7.990 / $9.200 / $14.990
- Aguaymanto: $7.990 / $9.200 / $14.990

### 🥃 Piscos Puros (Clásico / Cátedra / Vaticano)
- Pisco Puro Aromático: $7.990 / $9.990 / $15.990
- Pisco Blend Aromático: $8.500 / $10.990 / $16.990
- Mosto Joven Aromático: $8.990 / $11.990 / $17.990

### 🍺 Sours para Llevar
- 1 litro — $17.900 · De Sabores 1 litro — $18.990
- Medio litro — $9.990 · De Sabores medio litro — $10.990

### 🥤 Bebidas & Jugos
- Inka Kola — $3.990
- Coca-Cola, Fanta, Sprite, Ginger Ale — $2.990
- Agua Mineral — $2.990
- Jugos Naturales — $5.990
- Chicha Morada — $5.990
- Limonada Clásica — $5.200
- Limonada Menta / Jengibre — $5.990
- Limonada Sabores — $5.990

### 🍺 Cervezas
- Cusqueña Rubia — $5.200
- Cusqueña Negra — $5.500
- Kunstmann Torobayo — $5.200
- Heineken — $5.200
- Corona — $5.200
- Heineken Cero (sin alcohol) — $4.990
- Calafate (Cerveza Austral) — $5.990

### 🥃 Whisky
- Jack Daniel's — $7.990
- Johnnie Walker Red — $7.990
- Johnnie Walker Black — $8.990
- Chivas Regal 12 Años — $9.990
- Chivas Regal 18 Años — $10.990

### 🍸 Vodka
- Stolichnaya — $8.990 · Absolut Blue — $8.990
- Skyy Blue — $8.990 · Grey Goose — $9.990

### 🍃 Gin
- Tanqueray — $7.990 · Beefeater — $7.990
- Hendrick's — $8.990 · Bombay Sapphire — $8.990 · Tanqueray Ten — $10.990

### 🍹 Ron
- Ron Bacardí 8 Años — $8.990 · Havana Club Añejo Rva — $8.990
- Havana Club 5 Años — $8.990 · Havana Club 7 Años — $9.990
- Bacardí Añejo — $8.990 · Zacapa 23 Años — $13.990

### 🫗 Bajativos
- Amaretto Disaronno — $7.200 · Gran Marnier — $7.200 · Frangelico — $7.200
- Drambuie — $7.200 · Cointreau — $7.200 · Anís Sambuca Galliano — $7.200
- Crema de Baileys — $6.990 · Licor de Café Kahlúa — $6.990
- Fernet Branca — $6.990 · Bitter Araucano — $5.200 · Menta Frape — $5.200

### 🍷 Vinos Blancos & Espumantes
- Viña Mar Brut — $15.990 · Viña Mar Rosé — $15.990 · Viña Mar Brut 375cc — $9.990
- Bouchon Extra Brut — $19.990
- Chardonnay: Apaltagua Rva $15.800 · Casas Patronales Rva $14.990 · Santa Ema Select Terroir $16.600 · Casas del Bosque Rva $18.990
- Sauvignon Blanc: Casas Patronales $14.990 · J. Bouchon $15.990 · Santa Ema $16.990 · Casas del Bosque $18.990 · Miguel Torres $19.990
- Late Harvest: Casas Patronales Lujuria $21.990

### 🍷 Vinos Tintos
- Cabernet Sauvignon: Apaltagua $15.800 · Casas Patronales $14.990 · J. Bouchon $15.990 · Santa Ema $16.990 · Perez Cruz Gran Rva $21.990 · Miguel Torres $19.990
- Carmenere: Casas Patronales $14.990 · J. Bouchon $15.990 · Santa Ema $16.990 · Miguel Torres $19.990
- Merlot: Casas Patronales $14.990 · J. Bouchon $15.990 · Santa Ema $16.990 · Miguel Torres $19.990
- Ensamblajes: Santa Ema 60/40 $20.990 · Toro de Piedra Gran Rva $22.990 · Casas Patronales $14.990
- Grandes vinos: Santa Ema Catalina $42.900 · Peres Cruz Limited Edition $32.900
- Por copa Reserva — $5.990 · Media botella Santa Ema $10.580 · Media botella Miguel Torres $10.990


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
- Usa los precios exactos de la carta de arriba — NUNCA inventes precios ni redondees
- **Si el cliente pide algo que NO está en la carta:**
  1. Dile honestamente que no tenemos ese plato
  2. Pregúntale qué tipo de comida busca (carne, pescado, pasta, entrada, postre, bebida, etc.)
  3. Sugiere **solo** platos que existan textualmente en la carta con su precio exacto
  4. NUNCA adivines ni inventes nombres parecidos ni precios aproximados
  5. Si no sabes qué sugerir, envía la carta con `enviar_carta` para que el cliente elija
- **NUNCA llamar `crear_pedido` con un plato que no aparezca textualmente en la carta** — si hay duda, pregunta al cliente que elija entre opciones concretas de la carta

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
            # 1. Crear en estado pending (canal WhatsApp o Presencial según sesión)
            canal   = _get_sesion(wa_id).get("canal", "WhatsApp")
            reserva = await api_client.crear_reserva_publica(args, canal=canal)
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

    # ── Detección de modo presencial ──────────────────────────
    clave_presencial = await _get_palabra_clave_presencial()
    if texto.strip().lower() == clave_presencial.strip().lower():
        sesion["canal"] = "Presencial"
        return (
            "✅ *Modo presencial activado*\n\n"
            "Hola, estoy listo para ayudarte con la reserva desde el local 🍽️\n"
            "¿Para cuántas personas y qué fecha/hora tienes en mente?"
        )

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
