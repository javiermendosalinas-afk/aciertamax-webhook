# -*- coding: utf-8 -*-
"""
MAX 2.0 — Agente Inmobiliario de Acierta Max (ZMG)
===================================================
Webhook de Wati + Agente Claude con herramientas:
  - Busca propiedades en EasyBroker (venta y renta)
  - Envía fichas con foto por WhatsApp
  - Califica al cliente (Querer-Poder-Cómo-Cuándo-Dónde)
  - Registra leads en Google Sheets con folio ACIERTA-XXXX
  - Enruta correctamente RENTAS (corrige el pendiente conocido)

Despliegue: Render (Flask + gunicorn). Ver GUIA_IMPLEMENTACION.md
Variables de entorno requeridas:
  ANTHROPIC_API_KEY, EASYBROKER_API_KEY, WATI_API_KEY, WATI_BASE_URL,
  GOOGLE_CREDS_JSON (opcional), SHEET_ID (opcional),
  HUMAN_HANDOFF_NUMBER (opcional, tu WhatsApp para escalamiento)
"""

import os
import io
import json
import time
import threading
import requests
from urllib.parse import quote
from flask import Flask, request, jsonify

# ------------------------------------------------------------------
# CONFIGURACIÓN (todo por variables de entorno — nunca en el código)
# ------------------------------------------------------------------
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
EASYBROKER_API_KEY  = os.environ["EASYBROKER_API_KEY"]
WATI_API_KEY        = os.environ["WATI_API_KEY"]
WATI_BASE_URL       = os.environ["WATI_BASE_URL"].rstrip("/")  # ej. https://live-mt-server.wati.io/437629
GOOGLE_CREDS_JSON   = os.environ.get("GOOGLE_CREDS_JSON") or os.environ.get("GOOGLE_CREDENTIALS", "")
SHEET_ID            = os.environ.get("SHEET_ID", "")
if "/d/" in SHEET_ID:  # tolerancia: si pegaron la URL completa, extraer el ID
    SHEET_ID = SHEET_ID.split("/d/")[1].split("/")[0]
HUMAN_HANDOFF       = os.environ.get("HUMAN_HANDOFF_NUMBER", "")
CALENDLY_URL        = os.environ.get("CALENDLY_URL", "")
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

EB_API = "https://api.easybroker.com/v1"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

app = Flask(__name__)

# ------------------------------------------------------------------
# MEMORIA DE CONVERSACIÓN (en RAM, últimos 20 turnos por número)
# Nota: se reinicia si Render reinicia el servicio; suficiente para
# la sesión de WhatsApp de 24h. El lead queda persistido en Sheets.
# ------------------------------------------------------------------
CONVERSATIONS = {}
CONV_LOCK = threading.Lock()
MAX_TURNS = 20
PENDING = {}      # mensajes en ráfaga esperando turno, por número
PHONE_LOCKS = {}  # un candado por número: una respuesta a la vez

def get_history(phone):
    with CONV_LOCK:
        return list(CONVERSATIONS.get(phone, []))

def append_history(phone, role, content):
    with CONV_LOCK:
        h = CONVERSATIONS.setdefault(phone, [])
        h.append({"role": role, "content": content})
        if len(h) > MAX_TURNS:
            del h[: len(h) - MAX_TURNS]

# ------------------------------------------------------------------
# EASYBROKER — funciones de inventario
# ------------------------------------------------------------------
def eb_headers():
    return {"X-Authorization": EASYBROKER_API_KEY, "Accept": "application/json"}

def eb_buscar(operacion=None, tipo=None, zona=None, precio_min=None,
              precio_max=None, recamaras_min=None, limite=5):
    """Busca propiedades publicadas en EasyBroker."""
    params = {"page": 1, "limit": min(int(limite or 5), 10),
              "search[statuses][]": "published"}
    if operacion in ("venta", "sale"):
        params["search[operation_type]"] = "sale"
    elif operacion in ("renta", "rental", "alquiler"):
        params["search[operation_type]"] = "rental"
    if precio_min: params["search[min_price]"] = int(precio_min)
    if precio_max: params["search[max_price]"] = int(precio_max)
    if recamaras_min: params["search[min_bedrooms]"] = int(recamaras_min)
    if tipo: params["search[property_types][]"] = tipo
    r = requests.get(f"{EB_API}/properties", headers=eb_headers(),
                     params=params, timeout=20)
    if r.status_code != 200:
        return {"error": f"EasyBroker respondió {r.status_code}: {r.text[:200]}"}
    data = r.json().get("content", [])
    out = []
    zona_l = (zona or "").lower()
    for p in data:
        loc = p.get("location", "") or ""
        # filtro suave por zona (EasyBroker filtra por location ids;
        # aquí filtramos por texto para simplicidad)
        if zona_l and zona_l not in loc.lower() and zona_l not in (p.get("title") or "").lower():
            continue
        op = (p.get("operations") or [{}])[0]
        out.append({
            "public_id": p.get("public_id"),
            "titulo": p.get("title"),
            "ubicacion": loc,
            "operacion": op.get("type"),
            "precio": op.get("formatted_amount") or op.get("amount"),
            "recamaras": p.get("bedrooms"),
            "banos": p.get("bathrooms"),
            "estacionamientos": p.get("parking_spaces"),
            "construccion_m2": p.get("construction_size"),
        })
    if not out and zona_l:
        # si el filtro de zona vació los resultados, regresa sin filtrar
        # y avisa al agente para que lo comunique con honestidad
        return {"aviso": f"No hay coincidencia exacta en '{zona}'. Opciones cercanas:",
                "propiedades": [{
                    "public_id": p.get("public_id"), "titulo": p.get("title"),
                    "ubicacion": p.get("location"),
                    "precio": (p.get("operations") or [{}])[0].get("formatted_amount"),
                    "recamaras": p.get("bedrooms"),
                } for p in data[:5]]}
    return {"propiedades": out}

def eb_detalle(public_id):
    r = requests.get(f"{EB_API}/properties/{public_id}", headers=eb_headers(), timeout=20)
    if r.status_code != 200:
        return {"error": f"No encontré la propiedad {public_id} ({r.status_code})"}
    p = r.json()
    op = (p.get("operations") or [{}])[0]
    return {
        "public_id": p.get("public_id"),
        "titulo": p.get("title"),
        "descripcion": (p.get("description") or "")[:800],
        "ubicacion": p.get("location", {}).get("name") if isinstance(p.get("location"), dict) else p.get("location"),
        "operacion": op.get("type"),
        "precio": op.get("formatted_amount") or op.get("amount"),
        "recamaras": p.get("bedrooms"), "banos": p.get("bathrooms"),
        "medio_banos": p.get("half_bathrooms"),
        "estacionamientos": p.get("parking_spaces"),
        "construccion_m2": p.get("construction_size"),
        "terreno_m2": p.get("lot_size"),
        "url_publica": p.get("public_url"),
        "foto": p.get("title_image_full"),
        "num_fotos": len(p.get("property_images") or []),
    }

# ------------------------------------------------------------------
# WATI — envío de mensajes y fichas
# ------------------------------------------------------------------
def wati_headers():
    return {"Authorization": f"Bearer {WATI_API_KEY}"}

def wati_send_text(phone, text):
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone}"
    r = requests.post(url, headers=wati_headers(),
                      params={"messageText": text}, timeout=20)
    return r.status_code in (200, 201)

def wati_send_image(phone, image_url, caption=""):
    """Descarga la foto de EasyBroker y la sube a Wati como archivo de sesión."""
    try:
        img = requests.get(image_url, timeout=25)
        if img.status_code != 200:
            return False
        url = f"{WATI_BASE_URL}/api/v1/sendSessionFile/{phone}"
        files = {"file": ("propiedad.jpg", io.BytesIO(img.content), "image/jpeg")}
        r = requests.post(url, headers=wati_headers(),
                          params={"caption": caption[:1000]}, files=files, timeout=40)
        return r.status_code in (200, 201)
    except Exception:
        return False

def enviar_ficha(phone, public_id):
    """Ficha comercial: foto con caption + mensaje de detalle."""
    d = eb_detalle(public_id)
    if "error" in d:
        return d
    precio = d.get("precio") or "Precio a consultar"
    partes = []
    if d.get("recamaras"): partes.append(f"🛏 {d['recamaras']} rec")
    if d.get("banos"): partes.append(f"🛁 {d['banos']} baños")
    if d.get("estacionamientos"): partes.append(f"🚗 {d['estacionamientos']} autos")
    if d.get("construccion_m2"): partes.append(f"📐 {d['construccion_m2']} m² const.")
    if d.get("terreno_m2"): partes.append(f"🌳 {d['terreno_m2']} m² terreno")
    caption = f"🏡 {d.get('titulo','Propiedad')}\n📍 {d.get('ubicacion','ZMG')}\n💰 {precio}"
    detalle = " · ".join(partes)
    cuerpo = f"{detalle}\n\n{(d.get('descripcion') or '').strip()[:400]}"
    if d.get("url_publica"):
        cuerpo += f"\n\n🔗 Ficha completa y fotos: {d['url_publica']}"
    cuerpo += "\n\n_Acierta Max — 20 años haciendo que suceda_ ✅"
    ok_img = False
    if d.get("foto"):
        ok_img = wati_send_image(phone, d["foto"], caption)
    if not ok_img:
        wati_send_text(phone, caption)
    wati_send_text(phone, cuerpo)
    return {"enviada": True, "propiedad": d.get("titulo"), "public_id": public_id}

# ------------------------------------------------------------------
# GOOGLE SHEETS — registro de leads con folio ACIERTA-XXXX
# ------------------------------------------------------------------
def registrar_lead(phone, nombre="", interes="", operacion="", presupuesto="",
                   zona="", notas=""):
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return {"registrado": False, "motivo": "Sheets no configurado"}
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        libro = gspread.authorize(creds).open_by_key(SHEET_ID)
        # Pestaña propia de MAX: se crea sola la primera vez, con
        # encabezados correctos, sin tocar las pestañas existentes.
        try:
            sh = libro.worksheet("Leads MAX")
        except Exception:
            sh = libro.add_worksheet(title="Leads MAX", rows=1000, cols=10)
            sh.append_row(["FOLIO", "FECHA Y HORA", "WHATSAPP", "NOMBRE",
                           "OPERACIÓN", "INTERÉS", "PRESUPUESTO", "ZONA",
                           "NOTAS", "ESTATUS"])
        n = len(sh.get_all_values())  # incluye encabezado
        folio = f"ACIERTA-{n:04d}"
        sh.append_row([folio, time.strftime("%Y-%m-%d %H:%M"), phone, nombre,
                       operacion, interes, presupuesto, zona, notas, "NUEVO"])
        return {"registrado": True, "folio": folio}
    except Exception as e:
        return {"registrado": False, "motivo": str(e)[:200]}

def avisar_humano(phone, resumen):
    """Escala a Javier/equipo cuando el cliente está listo o pide humano."""
    if HUMAN_HANDOFF:
        wati_send_text(HUMAN_HANDOFF,
            f"🔥 LEAD CALIENTE\nCliente: {phone}\n{resumen[:600]}")
    return {"avisado": bool(HUMAN_HANDOFF)}

# ------------------------------------------------------------------
# AGENTE CLAUDE — definición de herramientas y system prompt
# ------------------------------------------------------------------
TOOLS = [
    {"name": "buscar_propiedades",
     "description": "Busca propiedades disponibles en el inventario de Acierta Max (EasyBroker). Úsala cuando el cliente diga qué busca. SIEMPRE distingue venta vs renta.",
     "input_schema": {"type": "object", "properties": {
         "operacion": {"type": "string", "enum": ["venta", "renta"]},
         "tipo": {"type": "string", "description": "house, apartment, land, commercial (opcional)"},
         "zona": {"type": "string", "description": "Colonia o municipio ZMG, ej. Zapopan, Tlaquepaque"},
         "precio_min": {"type": "number"}, "precio_max": {"type": "number"},
         "recamaras_min": {"type": "number"},
         "limite": {"type": "number", "description": "máx 10, default 5"}},
      "required": ["operacion"]}},
    {"name": "enviar_ficha",
     "description": "Envía al cliente la ficha comercial de una propiedad (foto + datos + liga). Úsala cuando el cliente muestre interés en una propiedad específica de los resultados. Máximo 3 fichas por turno.",
     "input_schema": {"type": "object", "properties": {
         "public_id": {"type": "string"}}, "required": ["public_id"]}},
    {"name": "registrar_lead",
     "description": "Registra o actualiza el lead en el CRM cuando ya tengas al menos nombre + operación + interés. Úsala UNA vez por conversación cuando el prospecto esté calificado.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"}, "interes": {"type": "string"},
         "operacion": {"type": "string"}, "presupuesto": {"type": "string"},
         "zona": {"type": "string"}, "notas": {"type": "string"}},
      "required": ["nombre", "operacion"]}},
    {"name": "avisar_humano",
     "description": "Notifica al equipo humano de Acierta. Úsala cuando: el cliente pida hablar con una persona, quiera agendar visita, esté listo para ofertar, o haga una pregunta legal/fiscal que no debes responder.",
     "input_schema": {"type": "object", "properties": {
         "resumen": {"type": "string", "description": "Resumen del cliente y su necesidad"}},
      "required": ["resumen"]}},
]

SYSTEM_PROMPT = """Eres MAX, el asesor digital de Acierta Max, inmobiliaria con 20 años de experiencia en la Zona Metropolitana de Guadalajara, dirigida por Javier Mendoza. Conversas por WhatsApp en español mexicano, cálido, profesional y BREVE (máximo 3-4 líneas por mensaje; WhatsApp no es para párrafos largos).
""" + (f"""
AGENDA DE CITAS: cuando el cliente quiera agendar cita o visita, además de avisar_humano, compártele esta liga para que elija directamente el día y la hora en la agenda: {CALENDLY_URL} — dile: "Puedes apartar aquí mismo el día y la hora que mejor te acomoden".
""" if CALENDLY_URL else "") + """

TU MISIÓN: entender qué necesita el cliente, mostrarle las mejores opciones del inventario y conectarlo con un asesor humano en el momento correcto. Cliente-céntrico siempre: estás del lado del cliente.

SI EL CLIENTE QUIERE COMPRAR (o rentar para sí) — FLUJO COMPRADOR:
1. Dale acceso al catálogo completo: "Puedes ver todo nuestro inventario en https://www.aciertamax.com" (compártelo temprano, es transparencia).
2. Inmediatamente ofrece el diferenciador: "¿Prefieres explorar por tu cuenta, o te doy ATENCIÓN PERSONALIZADA aquí mismo? Puedo hacer contigo un COACHING INMOBILIARIO CON IA: te hago las preguntas correctas y busco exactamente lo que satisface tus necesidades."
3. Si acepta el coaching: aplica Querer-Poder-Cómo-Cuándo-Dónde con calidez, una pregunta a la vez, y usa buscar_propiedades + enviar_ficha con lo mejor que encuentres.
4. Registra el lead cuando tengas nombre + operación + interés, y avisar_humano cuando pida visita u oferta.

SI EL CLIENTE QUIERE VENDER O RENTAR SU PROPIEDAD — FLUJO CAPTACIÓN (muy valioso):
1. Agradece la confianza y aclara con amabilidad: "Trabajamos exclusivamente la Zona Metropolitana de Guadalajara (Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco y El Salto)". Si su propiedad está fuera de la ZMG, agradece y ofrece registrar sus datos por si podemos referirlo.
2. Si está en la ZMG: comenta los beneficios de Acierta Max — 20+ años de experiencia, agentes certificados y miembros AMPI, miles de operaciones, opinión de valor profesional SIN COSTO, difusión en los principales portales y aciertamax.com, acompañamiento completo y seguro hasta la firma.
3. Pide con gusto una CITA: "¿Nos permites una cita para conocer tu propiedad y entregarte una opinión de valor sin costo ni compromiso? ¿Qué día te acomoda?"
4. Pregunta lo esencial (una a la vez): tipo de propiedad, colonia/municipio, y si es para venta o renta.
5. Pide su nombre → registrar_lead con operacion="CAPTACIÓN-VENDEDOR" y todo en notas → SIEMPRE avisar_humano (prioridad máxima) y confirma que un asesor certificado lo contacta hoy mismo.
NUNCA des un precio o valor de su propiedad por chat: eso lo entrega el asesor con la opinión de valor profesional.

MODELO DE CALIFICACIÓN (obtén esto conversando con naturalidad, NO como interrogatorio):
1. QUERER: ¿busca comprar o RENTAR? (distingue SIEMPRE; si dice rentar, alquilar, arrendar → operacion=renta)
2. PODER: presupuesto aproximado; si compra, ¿contado, crédito bancario o Infonavit?
3. CÓMO: ¿para vivir, invertir, oficina?
4. CUÁNDO: ¿urge o está explorando?
5. DÓNDE: zona de la ZMG (Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco).

PROPIEDADES EN CAMPAÑA (el sistema ya envió la ficha oficial si el cliente la mencionó; tú continúa calificando y resolviendo dudas SOLO con estos datos):
1. THE BLOCK EASY LIVING (también le dicen "el de ITESO"): depto en RENTA $18,000/mes + mant. $2,800. 1 recámara, 2 baños, 65 m², piso 4, amueblado disponible. Periférico Sur 8331, El Mante, Tlaquepaque, junto a ITESO. No aceptan mascotas. Liga oficial: https://www.aciertamax.com/property/iteso-amplio-departamento-nuevo-vista-panoramica-roof-garden-ubicacion-premium?agent=javier373&lang=es
2. SANTA ANA 360: depto en VENTA $1,820,000. 2 recámaras, 2 baños, 53 m², año 2022, estacionamiento techado. Santa Ana Tepetitlán, Zapopan, cerca de Bugambilias. Acepta crédito bancario, INFONAVIT y contado. Pet friendly. Liga oficial: https://www.aciertamax.com/property/departamento-equipado-de-2-recamaras-en-santa-ana-360-cerca-de-bugambilias?agent=javier373&lang=es
3. BELLA VITTORIA: deptos en VENTA desde $3,400,000, A ESTRENAR. 2 recámaras, 2 baños, 70-75 m², 1-2 cajones. Cobre 4232, Lomas de la Victoria, Tlaquepaque, a minutos de Plaza del Sol. Créditos bancarios e INFONAVIT/COFINAVIT, entrega inmediata, registrado ante PROFECO. Liga oficial: https://www.aciertamax.com/property/invierte-en-bella-vittoria-2-recamaras-con-excelente-ubicacion?agent=javier373&lang=es
Para PARQUE MORELOS y el resto del inventario: usa buscar_propiedades.

REGLAS DE ORO:
- NUNCA prometas tiempos exactos de contacto ("en 30-60 minutos"); di "hoy mismo" o "a la brevedad".
- NUNCA sugieras contactar directamente a Javier Mendoza ni a ninguna persona del equipo por nombre; el canal es: "un asesor certificado te contactará".
- Si ya ofreciste las mismas opciones y el cliente las rechazó, NO las vuelvas a ofrecer; reconócelo y pasa a alternativas (registrar su búsqueda para avisarle, ampliar criterios, o cita con asesor).
- Si NO te queda claro si la persona quiere COMPRAR o VENDER su propiedad, PREGÚNTALO antes de buscar o asumir. Frases como "vendo", "quiero vender", "pongo en venta" = VENDEDOR (captación), aunque mencione precios o características: esos datos describen SU propiedad, no lo que busca comprar.
- Si la conversación parece continuar algo que no recuerdas, discúlpate brevemente y confirma: "Para atenderte bien, ¿me confirmas si buscas comprar/rentar, o vender tu propiedad?"
- Un saludo inicial cálido con tu nombre (MAX de Acierta Max) solo la primera vez.
- Usa buscar_propiedades en cuanto sepas operación + una pista más (zona o presupuesto). No esperes a tener todo.
- Ofrece fichas: "¿Te mando la ficha con fotos?" y usa enviar_ficha si acepta (máx 3 por turno).
- Cuando tengas nombre + operación + interés → registrar_lead (una sola vez).
- Cliente quiere visita, ofertar, o pide humano → avisar_humano Y dile que un asesor le escribe en breve.
- NUNCA des asesoría legal, fiscal o hipotecaria definitiva; NUNCA negocies precios; NUNCA inventes propiedades ni datos: solo lo que devuelven las herramientas.
- Si preguntan algo fuera de bienes raíces, redirige con amabilidad.
- Si no hay resultados, dilo con honestidad y ofrece registrar su búsqueda para avisarle cuando llegue algo (registrar_lead con notas).
"""

def call_claude(messages):
    # Blindaje: la API exige turnos alternados user/assistant. Si dos
    # mensajes del cliente llegaron seguidos, se fusionan.
    limpio = []
    for m in messages:
        if limpio and limpio[-1]["role"] == m["role"] \
           and isinstance(limpio[-1]["content"], str) and isinstance(m["content"], str):
            limpio[-1] = {"role": m["role"],
                          "content": limpio[-1]["content"] + "\n" + m["content"]}
        else:
            limpio.append(m)
    r = requests.post(ANTHROPIC_API, timeout=60, headers={
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }, json={
        "model": CLAUDE_MODEL, "max_tokens": 1024,
        "system": SYSTEM_PROMPT, "tools": TOOLS, "messages": limpio,
    })
    if r.status_code != 200:
        print(f"[MAX-ERROR] Claude API {r.status_code}: {r.text[:500]}", flush=True)
    r.raise_for_status()
    return r.json()

def run_tool(name, args, phone):
    print(f"[MAX] Herramienta: {name} {json.dumps(args, ensure_ascii=False)[:200]}", flush=True)
    try:
        if name == "buscar_propiedades":
            out = eb_buscar(**args)
        elif name == "enviar_ficha":
            out = enviar_ficha(phone, args.get("public_id", ""))
        elif name == "registrar_lead":
            out = registrar_lead(phone, **args)
        elif name == "avisar_humano":
            out = avisar_humano(phone, args.get("resumen", ""))
        else:
            out = {"error": f"herramienta desconocida {name}"}
    except Exception as e:
        out = {"error": f"fallo en {name}: {str(e)[:200]}"}
    print(f"[MAX] Resultado {name}: {json.dumps(out, ensure_ascii=False)[:300]}", flush=True)
    return out

def agent_reply(phone, user_text):
    """Bucle agéntico: Claude decide, ejecuta herramientas, responde."""
    append_history(phone, "user", user_text)
    messages = get_history(phone)
    for _ in range(6):  # máx 6 vueltas de herramientas
        resp = call_claude(messages)
        content = resp.get("content", [])
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if resp.get("stop_reason") != "tool_use":
            final = "\n".join(t for t in texts if t).strip() or "¿Me repites por favor? 🙂"
            append_history(phone, "assistant", final)
            return final
        # registrar el turno del asistente con sus tool_use
        messages.append({"role": "assistant", "content": content})
        results = []
        for tu in tool_uses:
            out = run_tool(tu["name"], tu.get("input", {}), phone)
            results.append({"type": "tool_result", "tool_use_id": tu["id"],
                            "content": json.dumps(out, ensure_ascii=False)})
        messages.append({"role": "user", "content": results})
    append_history(phone, "assistant", "Dame un momento, ya te comparto la información. 🙌")
    return "Dame un momento, ya te comparto la información. 🙌"

# ------------------------------------------------------------------
# CAMPAÑAS ACTIVAS — respuesta inmediata con ficha exacta
# Cuando el mensaje menciona un desarrollo en campaña, MAX manda la
# ficha (foto + datos + liga oficial) EN SEGUNDOS, y luego califica.
# ------------------------------------------------------------------
CAMPANAS = {
    "block": {
        "claves": ["block", "iteso", "the block"],
        "foto": "https://assets.easybroker.com/property_images/6057125/107111726/EB-WG7125.png",
        "caption": "🏙 THE BLOCK EASY LIVING — Vive más. Muévete menos.\n📍 Periférico Sur M. Gómez Morín 8331, a un paso de ITESO\n💰 RENTA $18,000/mes · Amueblado disponible",
        "cuerpo": ("🛏 1 recámara amplia con baño y clóset · 🛁 medio baño de visitas · "
                   "📐 65 m² · 🚗 estacionamiento · piso 4\n\n"
                   "✨ Roof garden panorámico, salón social, áreas lounge y home office, "
                   "lavandería equipada, seguridad y acceso controlado.\n"
                   "📍 Acceso inmediato a ITESO, Periférico Sur, López Mateos, zona industrial "
                   "(HP, Flex, Continental, Tata), Punto Sur y Galerías Santa Anita.\n\n"
                   "🔗 Ficha completa con las 11 fotos:\n"
                   "https://www.aciertamax.com/property/iteso-amplio-departamento-nuevo-vista-panoramica-roof-garden-ubicacion-premium?agent=javier373&lang=es\n\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿La buscas para ti o para alguien más? Si gustas te agendo una visita esta misma semana 🙌",
    },
    "santa_ana": {
        "claves": ["santa ana", "santaana", "santa ana 360"],
        "foto": "https://assets.easybroker.com/property_images/6102602/108091829/EB-WL2602.png",
        "caption": "🏡 SANTA ANA 360 — Zapopan sur, a minutos de Bugambilias\n📍 Santa Ana Tepetitlán, Zapopan\n💰 VENTA $1,820,000 MXN",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños completos · 📐 53 m² · 🚗 estacionamiento "
                   "techado · piso 3 · construido en 2022\n\n"
                   "✨ Equipamiento superior: filtración de agua total, purificador UV, "
                   "persianas blackout, cocina con granito, todo eléctrico.\n"
                   "🏢 Vigilancia 24h, roof garden, asadores, áreas verdes, pet friendly.\n"
                   "💳 Se aceptan créditos bancarios, INFONAVIT y recursos propios. "
                   "Libre de gravamen, disponibilidad inmediata.\n\n"
                   "🔗 Ficha completa con las 22 fotos:\n"
                   "https://www.aciertamax.com/property/departamento-equipado-de-2-recamaras-en-santa-ana-360-cerca-de-bugambilias?agent=javier373&lang=es\n\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo comprarías con crédito bancario, INFONAVIT o recursos propios? Con eso te digo el paso a paso y te agendo visita 🙌",
    },
    "bellavittoria": {
        "claves": ["bella", "vittoria", "bellavittoria"],
        "foto": "https://assets.easybroker.com/property_images/5810277/101922700/EB-VI0277.png",
        "caption": "🏛 BELLA VITTORIA — Vive el estilo de vida que mereces\n📍 Cobre 4232, Lomas de la Victoria, Tlaquepaque (dentro de Periférico)\n💰 VENTA desde $3,400,000 MXN",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 70–75 m² · 🚗 1-2 cajones "
                   "(opción con preparación para auto eléctrico) · A ESTRENAR\n\n"
                   "✨ Lobby tipo hotel, roof top panorámico equipado, terraza de eventos, "
                   "asadores, juegos infantiles, sala de juegos, seguridad 24h.\n"
                   "📍 A minutos de Plaza del Sol, dentro de Periférico.\n"
                   "💳 Créditos bancarios e INFONAVIT/COFINAVIT · Entrega inmediata · "
                   "Documentación 100% en regla, registrado ante PROFECO.\n\n"
                   "🔗 Ficha completa con fotos y video:\n"
                   "https://www.aciertamax.com/property/invierte-en-bella-vittoria-2-recamaras-con-excelente-ubicacion?agent=javier373&lang=es\n\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo buscas para vivir o como inversión? Hay unidades desde ese precio y te puedo agendar visita al desarrollo esta semana 🙌",
    },
}

def detectar_campana(texto):
    t = texto.lower()
    for nombre, c in CAMPANAS.items():
        if any(k in t for k in c["claves"]):
            return nombre, c
    return None, None

def responder_campana(phone, texto, campana):
    """Ficha inmediata (foto + cuerpo + seguimiento) y deja registro
    en la memoria para que el agente continúe con contexto."""
    ok_img = wati_send_image(phone, campana["foto"], campana["caption"])
    if not ok_img:
        wati_send_text(phone, campana["caption"])
    wati_send_text(phone, campana["cuerpo"])
    wati_send_text(phone, campana["seguimiento"])
    append_history(phone, "user", texto)
    append_history(phone, "assistant",
        f"[Envié la ficha oficial de campaña con foto, datos y liga] {campana['caption']} "
        f"Y pregunté: {campana['seguimiento']}")

# ------------------------------------------------------------------
# WEBHOOK WATI
# ------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    # Ignorar mensajes propios / eventos que no son texto entrante
    if data.get("owner") is True:
        return jsonify(ok=True)
    phone = data.get("waId") or ""
    text = (data.get("text") or "").strip()
    if not phone or not text:
        return jsonify(ok=True)
    # ANTI-DUPLICADOS: Wati a veces manda el mismo evento 2 veces.
    # Ignoramos si ya vimos el mismo id de mensaje, o el mismo
    # (teléfono + texto) en los últimos 30 segundos.
    msg_id = data.get("id") or data.get("whatsappMessageId") or f"{phone}:{text}"
    ahora = time.time()
    with CONV_LOCK:
        vistos = getattr(webhook, "_vistos", {})
        # limpiar entradas viejas
        webhook._vistos = {k: v for k, v in vistos.items() if ahora - v < 300}
        if msg_id in webhook._vistos or webhook._vistos.get(f"{phone}:{text}", 0) > ahora - 30:
            return jsonify(ok=True, duplicado=True)
        webhook._vistos[msg_id] = ahora
        webhook._vistos[f"{phone}:{text}"] = ahora
    # FILA POR CLIENTE: si el cliente manda varios mensajes en ráfaga,
    # se juntan y MAX responde UNA sola vez a todo el paquete, en orden.
    with CONV_LOCK:
        PENDING.setdefault(phone, []).append(text)
        lock = PHONE_LOCKS.setdefault(phone, threading.Lock())

    def process():
        if not lock.acquire(blocking=False):
            return  # ya hay un hilo trabajando este número; él tomará el pendiente
        try:
            while True:
                with CONV_LOCK:
                    pendientes = PENDING.get(phone, [])
                    if not pendientes:
                        break
                    texto = "\n".join(pendientes)
                    PENDING[phone] = []
                try:
                    print(f"[MAX] Mensaje de {phone}: {texto[:200]}", flush=True)
                    # FAST-PATH de campañas: SOLO en el PRIMER mensaje de la
                    # conversación (así llegan los clics de anuncios).
                    nombre, campana = detectar_campana(texto)
                    historial = get_history(phone)
                    if campana and not historial:
                        print(f"[MAX] Campaña detectada: {nombre}", flush=True)
                        responder_campana(phone, texto, campana)
                        continue
                    reply = agent_reply(phone, texto)
                    print(f"[MAX] Respuesta a {phone}: {reply[:200]}", flush=True)
                    for i in range(0, len(reply), 900):
                        ok = wati_send_text(phone, reply[i:i+900])
                        if not ok:
                            print(f"[MAX-ERROR] Wati no aceptó el envío a {phone}", flush=True)
                except Exception:
                    import traceback
                    print(f"[MAX-ERROR] Excepción con {phone}:\n{traceback.format_exc()}", flush=True)
                    wati_send_text(phone, "Tuve un detalle técnico 🙏 Un asesor te contacta en breve.")
                    if HUMAN_HANDOFF:
                        wati_send_text(HUMAN_HANDOFF, f"⚠️ Error MAX con {phone}, revisar Logs en Render.")
        finally:
            lock.release()
    threading.Thread(target=process, daemon=True).start()
    return jsonify(ok=True)

@app.route("/health", methods=["GET"])
def health():
    """Para UptimeRobot (evita cold start de Render)."""
    return jsonify(status="ok", agente="MAX 2.0", ts=time.time())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
