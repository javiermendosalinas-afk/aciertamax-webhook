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
REGISTRADOS = {}  # phone -> (folio, timestamp): evita folios duplicados

def registrar_lead(phone, nombre="", interes="", operacion="", presupuesto="",
                   zona="", notas=""):
    # Candado: si este número ya se registró en las últimas 24h,
    # regresar el mismo folio en vez de crear otro.
    previo = REGISTRADOS.get(phone)
    if previo and time.time() - previo[1] < 86400:
        return {"registrado": True, "folio": previo[0],
                "nota": "ya estaba registrado; usa este folio, no lo registres de nuevo"}
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
        REGISTRADOS[phone] = (folio, time.time())
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
    {"name": "enviar_ficha_campana",
     "description": "Envía al cliente la ficha oficial (foto + datos + liga) de una de las 4 propiedades EN CAMPAÑA: block (The Block/ITESO), santa_ana (Santa Ana 360), bellavittoria (Bella Vittoria), villa_dhara (Villa Dhara/Parque Morelos). ÚSALA DE INMEDIATO cuando el cliente pida la ficha, fotos, brochure o diga 'sí/me interesa/esa' sobre una de estas propiedades.",
     "input_schema": {"type": "object", "properties": {
         "desarrollo": {"type": "string", "enum": ["block", "santa_ana", "bellavittoria", "villa_dhara"]}},
      "required": ["desarrollo"]}},
    {"name": "buscar_inventario_zmg",
     "description": "Busca en la BOLSA COMPLETA de la ZMG (~1,800 casas y departamentos en VENTA desde $3,000,000, propias y compartidas). Úsala cuando buscar_propiedades no tenga suficientes opciones, o directamente para búsquedas de compra desde $3M — pero SOLO después de haber hecho al menos una pregunta de calidad (uso, urgencia, o preferencia específica) como coach, no como reflejo automático al primer mensaje del cliente. Regresa título, precio, recámaras y liga.",
     "input_schema": {"type": "object", "properties": {
         "municipio": {"type": "string", "description": "Guadalajara, Zapopan, Tlaquepaque, Tonalá o Tlajomulco"},
         "precio_min": {"type": "number"}, "precio_max": {"type": "number"},
         "recamaras_min": {"type": "number"},
         "tipo": {"type": "string", "description": "casa o departamento"},
         "limite": {"type": "number", "description": "máx 8, default 5"}},
      "required": []}},
    {"name": "enviar_ficha_liga",
     "description": "Envía al cliente la ficha (foto + datos + liga oficial) de una propiedad de la bolsa ZMG. Usa la liga EXACTA que regresó buscar_inventario_zmg o seleccionar_de_lista. Máximo 3 por turno.",
     "input_schema": {"type": "object", "properties": {
         "liga": {"type": "string"}}, "required": ["liga"]}},
    {"name": "seleccionar_de_lista",
     "description": "Resuelve cuando el cliente se refiere a una opción de la ÚLTIMA lista que le mostraste por número o posición ('la 3', 'esa', 'la primera'). SIEMPRE úsala en ese caso en vez de adivinar o repetir de memoria — te regresa los datos reales y la liga exacta de esa posición.",
     "input_schema": {"type": "object", "properties": {
         "numero": {"type": "number", "description": "Posición en la última lista mostrada (1, 2, 3...)"}},
      "required": ["numero"]}},
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

SI EL CLIENTE QUIERE COMPRAR (o rentar para sí) — FLUJO COMPRADOR (eres su COACH, no un buscador — usa SPIN Compacto):
1. Dale acceso al catálogo completo: "Puedes ver todo nuestro inventario en https://www.aciertamax.com" (compártelo temprano, es transparencia).
2. Ofrece el diferenciador: "¿Prefieres explorar por tu cuenta, o te doy ATENCIÓN PERSONALIZADA aquí mismo? Puedo hacer contigo un COACHING INMOBILIARIO CON IA: te hago las preguntas correctas y busco exactamente lo que satisface tus necesidades."
3. SITUACIÓN: la cubre el modelo Querer-Poder-Cómo-Cuándo-Dónde (zona, presupuesto, recámaras, uso). No la repreguntes si el cliente ya la dio de golpe.
4. PROBLEMA — ANTES DE TU PRIMERA BÚSQUEDA: agradece los datos que ya diste, pero SIEMPRE agrega UNA pregunta de problema/calidad que no sea pura situación — la que más ayude a acotar: ¿qué es lo que más te ha costado encontrar hasta ahora?, ¿es para vivir o invertir?, ¿algo que no pueda faltar (amenidad, colonia exacta, planta baja)? Nunca dispares buscar_inventario_zmg de inmediato solo con precio+zona+recámaras: esos tres datos rara vez acotan lo suficiente en una bolsa de miles.
5. SI LA BÚSQUEDA REGRESA MUCHOS RESULTADOS (más de ~15): NO listes las más baratas. Di cuántas hay y pide UNA preferencia más para acotar antes de mostrar la lista. Mejor 5 opciones bien dirigidas que 5 arbitrarias.
6. SI LA BÚSQUEDA REGRESA POCOS O NINGÚN RESULTADO: dilo con honestidad y pregunta cuál criterio prefiere ceder (precio, zona vecina, recámaras) — no decidas tú solo.
7. PROBLEMA otra vez, tras cada reacción del cliente a una opción ("no me convence", "me gusta"): pregunta AL MENOS UNA VEZ el porqué antes de solo buscar más ("¿qué le faltó — tamaño, ubicación, algo más?"). Esto es lo que te distingue de un buscador.
8. IMPLICACIÓN — solo si el cliente YA reveló una urgencia real (renta que vence, familia creciendo, oferta que expira): amplifica con tacto, una sola vez, sin forzar: "y si no encuentras algo a tiempo, ¿qué pasaría con [lo que mencionó]?". Nunca la inventes ni la fuerces si no hay urgencia real en la conversación.
9. NECESIDAD-BENEFICIO — cuando por fin una opción encaje o esté cerca: en vez de enumerar tú las ventajas, pregunta para que el cliente las diga: "si esta cumple con eso, ¿qué te resolvería?" o "¿qué tanto se acerca a lo que buscabas?". Que lo diga él, no tú.
10. Registra el lead cuando tengas nombre + operación + interés, y avisar_humano cuando pida visita u oferta.

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
4. VILLA DHARA (Parque Morelos): loft ÚNICO de doble altura, 1 recámara, 1 baño, 74 m² + terraza privada de 55 m², amueblado, a estrenar (2025), piso 2. Frente al Parque Morelos, El Retiro, Guadalajara. RENTA $14,000/mes (mantenimiento $1,500) o VENTA $2,295,000 (acepta bancarios e INFONAVIT/COFINAVIT). Amenidades: gimnasio, biblioteca, salas de trabajo, ludoteca, huerto urbano, vigilancia 24/7. Cerca de Hospital Civil, Catedral, Línea 3. Ideal ejecutivos, médicos, nómadas digitales, Airbnb. Liga oficial: https://www.aciertamax.com/property/el-departamento-mas-exclusivo-de-villa-dhara-terraza-privada-74-m-amueblado?agent=javier373&lang=es
Para PARQUE MORELOS y el resto del inventario: usa buscar_propiedades.
REGLA CRÍTICA DE LAS PROPIEDADES EN CAMPAÑA: si el cliente pide la ficha, fotos o brochure de una de estas 4, o responde "sí / esa / me interesa" cuando se la ofreciste, usa INMEDIATAMENTE enviar_ficha_campana — NO hagas más preguntas antes, NO la describas de nuevo: mándala. Nota: estas 4 propiedades pueden NO aparecer en buscar_propiedades (el nombre de la zona no coincide); NUNCA digas "no aparece en el sistema": tú ya tienes sus datos aquí y su ficha en enviar_ficha_campana.

INVENTARIO — ORDEN DE BÚSQUEDA:
1. Propiedades en campaña (datos aquí arriba) y buscar_propiedades (inventario propio, venta y renta de todos los precios).
2. buscar_inventario_zmg: la BOLSA COMPLETA de la ZMG (~1,800 casas y deptos en VENTA desde $3M). Úsala siempre que el cliente compre desde $3M o cuando el inventario propio no alcance. ¡Con esta herramienta casi siempre HAY opciones: nunca digas "no tengo" sin consultarla!
3. Con propiedades de la bolsa: comparte SOLO los datos del registro (precio, recámaras, baños, m², municipio) + la liga con enviar_ficha_liga. NO inventes amenidades ni detalles: la ficha completa está en la liga. Máximo 3 fichas por turno.
4. CUANDO EL CLIENTE SE REFIERE A UNA OPCIÓN YA MOSTRADA ("la 3", "esa", "la primera", "la de Ciudad Granja"): usa SIEMPRE seleccionar_de_lista con el número de posición — NUNCA repitas datos de memoria ni adivines cuál era. Si el cliente nombra una zona/colonia que NUNCA apareció en tus resultados (tú no la mencionaste ni el cliente la vio en una lista tuya), es una zona NUEVA que el cliente está pidiendo: haz una NUEVA búsqueda con buscar_inventario_zmg filtrando por esa zona. Si esa nueva búsqueda no trae nada, di la verdad ("no tengo opciones en esa colonia exacta ahorita") y ofrece alternativas reales — jamás inventes un nombre de fraccionamiento o desarrollo que ninguna herramienta te dio.

REGLAS DE ORO:
- PROHIBIDO INVENTAR PROPIEDADES: cada nombre, precio, m² o característica que menciones debe venir literalmente de una respuesta de herramienta (buscar_propiedades, buscar_inventario_zmg, seleccionar_de_lista, o las fichas de campaña). Si el cliente insiste en un nombre que tú nunca dijiste y ninguna búsqueda lo confirma, jamás lo repitas como si existiera: aclara con calma que no tienes esa propiedad exacta disponible en este momento.
- DATOS 100% VERIFICADOS SOLAMENTE: al describir una propiedad, menciona ÚNICAMENTE atributos que las herramientas devolvieron para ESA propiedad específica, o que estén en su ficha de PROPIEDADES EN CAMPAÑA. NUNCA mezcles características de una propiedad con otra (ej. el estacionamiento techado es de Santa Ana 360, NO de Bella Vittoria). Ante CUALQUIER dato del que no estés seguro, no lo afirmes: di "déjame mandarte la ficha oficial con los detalles exactos" y usa enviar_ficha. Un dato inventado destruye la confianza del cliente y de Acierta Max.
- NUNCA pidas el teléfono del cliente: ya lo tienes (es este WhatsApp) y el sistema lo registra automáticamente. Solo pregunta si desea ser contactado en un número DIFERENTE.
- Registra a cada cliente UNA sola vez; si la herramienta te dice que ya estaba registrado, usa ese folio y no lo repitas.
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
    # SANITIZACIÓN: la API exige (1) primer turno = user, (2) sin
    # contenidos vacíos, (3) turnos alternados. Se limpia todo aquí.
    limpio = []
    for m in messages:
        c = m.get("content")
        if c is None or (isinstance(c, str) and not c.strip()) or (isinstance(c, list) and not c):
            continue  # descartar mensajes vacíos
        if not limpio and m["role"] != "user":
            continue  # el primer mensaje debe ser del usuario
        if limpio and limpio[-1]["role"] == m["role"] \
           and isinstance(limpio[-1]["content"], str) and isinstance(c, str):
            limpio[-1] = {"role": m["role"],
                          "content": limpio[-1]["content"] + "\n" + c}
        else:
            limpio.append({"role": m["role"], "content": c})
    if not limpio:
        limpio = [{"role": "user", "content": "Hola"}]
    for intento in (1, 2):  # un reintento automático ante fallas transitorias
        r = requests.post(ANTHROPIC_API, timeout=60, headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": CLAUDE_MODEL, "max_tokens": 1024,
            "system": SYSTEM_PROMPT, "tools": TOOLS, "messages": limpio,
        })
        if r.status_code == 200:
            return r.json()
        print(f"[MAX-ERROR] Claude API {r.status_code} (intento {intento}): {r.text[:500]}", flush=True)
        if r.status_code in (429, 500, 502, 503, 529) and intento == 1:
            time.sleep(2)
            continue
        r.raise_for_status()
    r.raise_for_status()

def run_tool(name, args, phone):
    print(f"[MAX] Herramienta: {name} {json.dumps(args, ensure_ascii=False)[:200]}", flush=True)
    try:
        if name == "buscar_propiedades":
            out = eb_buscar(**args)
        elif name == "enviar_ficha":
            out = enviar_ficha(phone, args.get("public_id", ""))
        elif name == "enviar_ficha_campana":
            out = enviar_ficha_campana(phone, args.get("desarrollo", ""))
        elif name == "buscar_inventario_zmg":
            out = buscar_inventario_zmg(phone, **args)
        elif name == "enviar_ficha_liga":
            out = enviar_ficha_liga(phone, args.get("liga", ""))
        elif name == "seleccionar_de_lista":
            out = seleccionar_de_lista(phone, args.get("numero"))
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
    "villa_dhara": {
        "claves": ["villa dhara", "dhara", "parque morelos"],
        "foto": "https://assets.easybroker.com/property_images/6057913/107125331/EB-WG7913.png",
        "caption": "🌿 VILLA DHARA — El loft con terraza privada frente al Parque Morelos\n📍 El Retiro, Centro de Guadalajara\n💰 RENTA $14,000/mes · o VENTA $2,295,000 MXN",
        "cuerpo": ("🛏 1 recámara · 🛁 1 baño completo · 📐 74 m² + TERRAZA PRIVADA de 55 m² · "
                   "a estrenar (2025) · totalmente AMUEBLADO · sala de doble altura\n\n"
                   "✨ Amenidades: gimnasio, biblioteca, salas de trabajo, ludoteca, huerto urbano, "
                   "terrazas panorámicas, elevador, vigilancia 24/7.\n"
                   "📍 A minutos caminando de Hospital Civil, Centro Médico, Catedral, "
                   "San Juan de Dios, Ciudad Creativa Digital y Línea 3 del Tren Ligero.\n"
                   "💳 En venta acepta créditos bancarios e INFONAVIT/COFINAVIT. Mantenimiento $1,500.\n\n"
                   "🔗 Ficha completa con las 11 fotos:\n"
                   "https://www.aciertamax.com/property/el-departamento-mas-exclusivo-de-villa-dhara-terraza-privada-74-m-amueblado?agent=javier373&lang=es\n\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "Este loft es único en el desarrollo: ¿te interesa para RENTARLO y vivirlo, o para COMPRARLO como inversión (ideal Airbnb)? 🙌",
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
    for nombre_c, c in CAMPANAS.items():
        if c is campana:
            FICHAS_ENVIADAS.setdefault(phone, set()).add(nombre_c)
            break
    append_history(phone, "user", texto)
    append_history(phone, "assistant",
        f"[Envié la ficha oficial de campaña con foto, datos y liga] {campana['caption']} "
        f"Y pregunté: {campana['seguimiento']}")

FICHAS_ENVIADAS = {}  # phone -> set de desarrollos ya enviados

def enviar_ficha_campana(phone, desarrollo):
    """Envía la ficha oficial de una propiedad EN CAMPAÑA (foto + cuerpo)
    en cualquier momento de la conversación."""
    c = CAMPANAS.get(desarrollo)
    if not c:
        return {"error": f"desarrollo desconocido: {desarrollo}"}
    if desarrollo in FICHAS_ENVIADAS.get(phone, set()):
        return {"enviada": False,
                "nota": "esta ficha YA se envió antes en esta conversación; NO la repitas, continúa la conversación respondiendo la duda del cliente"}
    ok_img = wati_send_image(phone, c["foto"], c["caption"])
    if not ok_img:
        wati_send_text(phone, c["caption"])
    wati_send_text(phone, c["cuerpo"])
    FICHAS_ENVIADAS.setdefault(phone, set()).add(desarrollo)
    return {"enviada": True, "desarrollo": desarrollo,
            "nota": "ficha con foto y liga ya enviada al cliente; continúa la conversación sin repetir estos datos"}

# ------------------------------------------------------------------
# INVENTARIO ZMG COMPARTIDO (bolsa completa leída de aciertamax.com)
# Archivo inventario_zmg.csv junto a app.py; se actualiza semanalmente
# corriendo inventario_zmg.py y resubiendo el CSV al repositorio.
# ------------------------------------------------------------------
INVENTARIO_ZMG = []
try:
    import csv as _csv
    with open("inventario_zmg.csv", encoding="utf-8") as _f:
        for _row in _csv.DictReader(_f):
            try:
                _row["Precio"] = int(float(_row.get("Precio") or 0))
            except ValueError:
                _row["Precio"] = 0
            try:
                _row["Recámaras"] = int(float(_row["Recámaras"])) if _row.get("Recámaras") not in (None, "", "nan") else None
            except ValueError:
                _row["Recámaras"] = None
            INVENTARIO_ZMG.append(_row)
    print(f"[MAX] Inventario ZMG cargado: {len(INVENTARIO_ZMG)} propiedades", flush=True)
except FileNotFoundError:
    print("[MAX] Sin inventario_zmg.csv: solo inventario propio disponible", flush=True)

ULTIMA_BUSQUEDA = {}  # phone -> lista de propiedades mostradas en el último resultado
                      # (permite resolver "la 3", "esa" sin adivinar ni inventar)

def buscar_inventario_zmg(phone, municipio=None, precio_min=None, precio_max=None,
                          recamaras_min=None, tipo=None, limite=5):
    """Busca en la bolsa compartida ZMG (venta >= $3M). Guarda el resultado
    exacto mostrado a ESTE cliente para poder resolver referencias como
    "la 3" con seleccionar_de_lista, sin inventar nada."""
    if not INVENTARIO_ZMG:
        return {"aviso": "inventario compartido no disponible; usa buscar_propiedades"}
    res = []
    muni_l = (municipio or "").lower()
    tipo_l = (tipo or "").lower()
    for p in INVENTARIO_ZMG:
        if muni_l and muni_l not in p.get("Municipio", "").lower():
            continue
        if tipo_l:
            pt = p.get("Tipo", "").lower()
            if "depa" in tipo_l or "depart" in tipo_l:
                if "departamento" not in pt:
                    continue
            elif "casa" in tipo_l and "casa" not in pt:
                continue
        precio = p.get("Precio") or 0
        if precio_min and precio < float(precio_min):
            continue
        if precio_max and precio > float(precio_max):
            continue
        if recamaras_min and (p.get("Recámaras") or 0) < int(recamaras_min):
            continue
        res.append(p)
    res.sort(key=lambda x: x.get("Precio") or 0)
    mostradas = res[: min(int(limite or 5), 8)]
    # Se guarda la lista EXACTA mostrada, en el mismo orden, indexada 1..N
    ULTIMA_BUSQUEDA[phone] = mostradas
    out = [{
        "numero": i + 1,
        "titulo": p.get("Título/Colonia"), "municipio": p.get("Municipio"),
        "tipo": p.get("Tipo"), "precio": p.get("Precio"),
        "recamaras": p.get("Recámaras"), "banos": p.get("Baños"),
        "m2": p.get("m²"), "liga": p.get("Liga"),
    } for i, p in enumerate(mostradas)]
    return {"total_coincidencias": len(res), "propiedades": out,
            "nota": "Guardado como la lista activa de este cliente. Si el cliente responde "
                    "'la 1/2/3...' usa seleccionar_de_lista con ese número — NUNCA inventes "
                    "un nombre de propiedad que no esté en esta lista."}

def seleccionar_de_lista(phone, numero):
    """Resuelve 'la 3', 'esa', etc. contra la ÚLTIMA lista real mostrada
    a este cliente. Si no hay coincidencia, dice la verdad: no inventa."""
    lista = ULTIMA_BUSQUEDA.get(phone) or []
    try:
        idx = int(numero) - 1
    except (TypeError, ValueError):
        return {"error": "número inválido"}
    if not lista or idx < 0 or idx >= len(lista):
        return {"error": "no tengo esa propiedad en la última lista que te mostré; "
                          "pide de nuevo la lista con buscar_inventario_zmg o pregunta "
                          "al cliente a cuál de las mostradas se refiere"}
    p = lista[idx]
    return {"titulo": p.get("Título/Colonia"), "municipio": p.get("Municipio"),
            "precio": p.get("Precio"), "recamaras": p.get("Recámaras"),
            "banos": p.get("Baños"), "m2": p.get("m²"), "liga": p.get("Liga")}

def enviar_ficha_liga(phone, liga):
    """Ficha de una propiedad de la bolsa: foto (og:image de la página)
    + datos del registro + liga oficial con código de agente."""
    p = next((x for x in INVENTARIO_ZMG if x.get("Liga") == liga), None)
    if not p:
        return {"error": "liga no encontrada en el inventario"}
    foto = None
    try:
        r = requests.get(liga, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        m = None
        if r.status_code == 200:
            import re as _re
            m = _re.search(r'property="og:image"\s+content="([^"]+)"', r.text) or \
                _re.search(r'content="([^"]+)"\s+property="og:image"', r.text)
        if m:
            foto = m.group(1).replace("&amp;", "&")
    except Exception:
        pass
    precio = p.get("Precio") or 0
    caption = (f"🏡 {p.get('Título/Colonia', 'Propiedad')}\n"
               f"📍 {p.get('Municipio', 'ZMG')}\n"
               f"💰 ${precio:,.0f} MXN en VENTA")
    partes = []
    if p.get("Recámaras"): partes.append(f"🛏 {p['Recámaras']} rec")
    if p.get("Baños") not in (None, "", "nan"): partes.append(f"🛁 {p['Baños']} baños")
    if p.get("m²") not in (None, "", "nan"): partes.append(f"📐 {p['m²']} m²")
    cuerpo = (" · ".join(partes) +
              f"\n\n🔗 Ficha completa con fotos y detalles:\n{liga}"
              f"\n\nAcierta Max — Socio AMPI, certificado ✅"
              f"\n\n🔵 _Propiedad compartida mediante colaboración inmobiliaria profesional. "
              f"Precio y disponibilidad sujetos a confirmación._")
    ok_img = wati_send_image(phone, foto, caption) if foto else False
    if not ok_img:
        wati_send_text(phone, caption)
    wati_send_text(phone, cuerpo)
    return {"enviada": True, "titulo": p.get("Título/Colonia"),
            "nota": "ficha enviada; continúa la conversación"}

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
