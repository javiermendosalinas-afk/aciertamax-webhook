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
import re
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

# ------------------------------------------------------------------
# EQUIPO DE VENDEDORES — rotacion round-robin
# Javier recibe copia de TODOS los leads siempre.
# Los demas reciben solo el que les toco en turno.
# ------------------------------------------------------------------
VENDEDORES = [
    {"nombre": "Javier",  "phone": os.environ.get("VENDEDOR_JAVIER",  "3325773277")},
    {"nombre": "Ubaldo",  "phone": os.environ.get("VENDEDOR_UBALDO",  "3319128128")},
    {"nombre": "Leticia", "phone": os.environ.get("VENDEDOR_LETICIA", "3316183775")},
    {"nombre": "Gloria",  "phone": os.environ.get("VENDEDOR_GLORIA",  "3331270050")},
]
JAVIER_PHONE = VENDEDORES[0]["phone"]  # siempre recibe copia de todo
_TURNO_LOCK = threading.Lock()
_turno_actual = [0]  # indice en VENDEDORES, compartido entre threads

def _siguiente_vendedor():
    """Retorna el vendedor al que le toca este lead (round-robin).
    Javier es indice 0 — aparece cada 4 leads como parte del ciclo
    Y ademas recibe copia de todos."""
    with _TURNO_LOCK:
        v = VENDEDORES[_turno_actual[0] % len(VENDEDORES)]
        _turno_actual[0] += 1
    return v
CALENDLY_URL        = os.environ.get("CALENDLY_URL", "")
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

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
HUMANO_ACTIVO = {}  # phone -> timestamp de la última vez que un humano escribió
COOLDOWN_HUMANO = 30 * 60  # 30 minutos de silencio de MAX tras intervención humana

# ------------------------------------------------------------------
# MEMORIA PERSISTENTE Y SEGUIMIENTO PROACTIVO
# ------------------------------------------------------------------
MEMORIA_CACHE = {}   # phone -> dict con busqueda, nombre, etc. (cache en RAM)
SEGUIMIENTO_PENDIENTE = {}  # phone -> {tipo, datos, timestamp}

ORIGEN_POR_TELEFONO = {}  # phone -> sourceUrl (liga de Instagram) del primer contacto

# Mapeo manual: liga exacta de la publicación de Instagram -> nombre de
# campaña (debe coincidir con una clave real de CAMPANAS). Javier lo llena
# cada vez que confirma qué publicación promociona qué propiedad — así,
# aunque el botón de Instagram mande un mensaje genérico ("Quiero más
# información"), MAX sabe identificar la propiedad exacta por el origen.
MAPEO_POST_A_CAMPANA = {
    "https://www.instagram.com/p/Da33OUcA8nj/": "solares_zona_real",  # EB-WJ9214, confirmado 19/07/2026
    "https://www.instagram.com/p/Da365rRg2VF/": "paneles_solares",     # EB-UO2612, confirmado 20/07/2026
}

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

def _normalizar_phone_wati(phone):
    """Wati necesita el numero con codigo de pais completo para mensajes salientes.
    Si el numero tiene 10 digitos (formato local), agrega 521 al inicio."""
    p = str(phone).strip().replace(" ","").replace("-","").replace("+","")
    if len(p) == 10:
        return "521" + p
    return p

def wati_send_text(phone, text):
    phone_norm = _normalizar_phone_wati(phone)
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_norm}"
    r = requests.post(url, headers=wati_headers(),
                      params={"messageText": text}, timeout=20)
    return r.status_code in (200, 201)

def wati_send_image(phone, image_url, caption=""):
    phone = _normalizar_phone_wati(phone)
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
    ok_caption = ok_img or wati_send_text(phone, caption)
    ok_cuerpo = wati_send_text(phone, cuerpo)
    if not (ok_caption and ok_cuerpo):
        return {"enviada": False,
                "error": "el envío por WhatsApp falló o solo se completó parcialmente",
                "nota": "NO confirmes al cliente que se la mandaste; dile que hubo un problema técnico"}
    return {"enviada": True, "propiedad": d.get("titulo"), "public_id": public_id}

# ------------------------------------------------------------------

# ------------------------------------------------------------------
# MEMORIA PERSISTENTE EN GOOGLE SHEETS
# ------------------------------------------------------------------
HOJA_MEMORIA = "Memoria Prospectos"
HOJA_SEGUIMIENTO = "Seguimiento Vendedor"
COLS_MEMORIA = ["WHATSAPP","NOMBRE","ULTIMA_BUSQUEDA","OPERACION",
                "PRESUPUESTO","ZONA","RECAMARAS","PROPIEDADES_VISTAS",
                "ULTIMA_INTERACCION","ESTADO","NOTAS_COACHING"]

def _sheets_client():
    """Retorna (libro, cliente) o (None, None) si Sheets no esta configurado."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return None, None
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    libro = gspread.authorize(creds).open_by_key(SHEET_ID)
    return libro, creds

def _get_o_crear_hoja(libro, titulo, cols):
    try:
        return libro.worksheet(titulo)
    except Exception:
        sh = libro.add_worksheet(title=titulo, rows=2000, cols=len(cols))
        sh.append_row(cols)
        return sh

def memoria_leer(phone):
    """Lee la memoria del prospecto desde Sheets. Usa cache en RAM."""
    if phone in MEMORIA_CACHE:
        return MEMORIA_CACHE[phone]
    try:
        libro, _ = _sheets_client()
        if not libro:
            return {}
        sh = _get_o_crear_hoja(libro, HOJA_MEMORIA, COLS_MEMORIA)
        celdas = sh.findall(phone, in_column=1)
        if not celdas:
            return {}
        fila = sh.row_values(celdas[-1].row)
        datos = dict(zip(COLS_MEMORIA, fila + [""]*(len(COLS_MEMORIA)-len(fila))))
        MEMORIA_CACHE[phone] = datos
        return datos
    except Exception as e:
        print(f"[MAX-MEM] Error leyendo memoria {phone}: {e}", flush=True)
        return {}

def memoria_guardar(phone, **kwargs):
    """Crea o actualiza la fila de memoria del prospecto en Sheets."""
    try:
        libro, _ = _sheets_client()
        if not libro:
            return
        sh = _get_o_crear_hoja(libro, HOJA_MEMORIA, COLS_MEMORIA)
        celdas = sh.findall(phone, in_column=1)
        kwargs["WHATSAPP"] = phone
        kwargs["ULTIMA_INTERACCION"] = hora_gdl()
        if celdas:
            fila_num = celdas[-1].row
            fila_actual = sh.row_values(fila_num)
            datos = dict(zip(COLS_MEMORIA, fila_actual + [""]*(len(COLS_MEMORIA)-len(fila_actual))))
            datos.update(kwargs)
            nueva_fila = [datos.get(c,"") for c in COLS_MEMORIA]
            sh.update(f"A{fila_num}", [nueva_fila])
        else:
            nueva_fila = [kwargs.get(c,"") for c in COLS_MEMORIA]
            sh.append_row(nueva_fila)
        MEMORIA_CACHE[phone] = kwargs
        print(f"[MAX-MEM] Memoria guardada para {phone}: {list(kwargs.keys())}", flush=True)
    except Exception as e:
        print(f"[MAX-MEM] Error guardando memoria {phone}: {e}", flush=True)

def memoria_resumen_para_max(phone):
    """Genera un texto corto que MAX puede usar al inicio de una nueva sesion."""
    m = memoria_leer(phone)
    if not m or not m.get("ULTIMA_BUSQUEDA"):
        return ""
    partes = []
    if m.get("NOMBRE"):
        partes.append(f"Nombre: {m['NOMBRE']}")
    if m.get("OPERACION"):
        partes.append(f"Busca: {m['OPERACION']}")
    if m.get("ZONA"):
        partes.append(f"Zona: {m['ZONA']}")
    if m.get("PRESUPUESTO"):
        partes.append(f"Presupuesto: {m['PRESUPUESTO']}")
    if m.get("RECAMARAS"):
        partes.append(f"Recamaras: {m['RECAMARAS']}")
    if m.get("PROPIEDADES_VISTAS"):
        partes.append(f"Ya vio: {m['PROPIEDADES_VISTAS'][:100]}")
    if m.get("NOTAS_COACHING"):
        partes.append(f"Notas: {m['NOTAS_COACHING'][:100]}")
    return " | ".join(partes) if partes else ""

def seguimiento_registrar_vendedor(phone, nombre, folio, vendedor_asignado=None):
    """Asigna lead al vendedor en turno (round-robin), notifica al vendedor
    asignado con el cuestionario de seguimiento, y manda copia informativa
    a Javier si el asignado no es el mismo Javier."""
    # Determinar vendedor en turno
    v = vendedor_asignado or _siguiente_vendedor()
    nombre_v = v["nombre"] if isinstance(v, dict) else v
    phone_v  = v["phone"]  if isinstance(v, dict) else JAVIER_PHONE

    # Registrar en Google Sheets
    cols = ["FOLIO","FECHA","WHATSAPP","NOMBRE CLIENTE","VENDEDOR ASIGNADO",
            "CONTACTO?","BUSQUEDA CONFIRMADA","URGENCIA",
            "REQUIERE CREDITO","FECHA CITA","NOTAS"]
    try:
        libro, _ = _sheets_client()
        if libro:
            sh = _get_o_crear_hoja(libro, HOJA_SEGUIMIENTO, cols)
            sh.append_row([folio, hora_gdl(), phone, nombre, nombre_v,
                           "Pendiente","","","","",""])
    except Exception as e:
        print(f"[MAX-SEG] Error en Sheets: {e}", flush=True)

    # Obtener contexto del prospecto
    m = memoria_leer(phone)
    busqueda  = m.get("ULTIMA_BUSQUEDA","No especificada")
    zona      = m.get("ZONA","")
    presupuesto = m.get("PRESUPUESTO","")
    props     = m.get("PROPIEDADES_VISTAS","")

    # Mensaje con cuestionario para el vendedor asignado
    cuestionario = (
        f"*[NUEVO LEAD — {folio}]*\n"
        f"Te toco este prospecto. Ponte en contacto hoy.\n\n"
        f"*Cliente:* {nombre}\n"
        f"*WhatsApp:* {phone}\n"
        f"*Busca:* {busqueda}\n"
        f"*Zona:* {zona or 'No especificada'}\n"
        f"*Presupuesto:* {presupuesto or 'No especificado'}\n"
        f"*Propiedades vistas:* {props[:100] if props else 'Ninguna aun'}\n\n"
        f"*Responde estas preguntas (numeradas) para el CRM:*\n"
        f"1. Ya te comunicaste? (SI / NO / NO CONTESTA)\n"
        f"2. Confirmaste su busqueda? (SI / CAMBIO / NO PUDE)\n"
        f"3. Para cuando quiere? (INMEDIATO / 1-3M / 3-6M / EXPLORANDO)\n"
        f"4. Requiere credito? (INFONAVIT / BANCO / NO / NO SE)\n"
        f"5. Cuando lo vas a ver? (escribe la fecha)\n\n"
        f"El cliente espera tu llamada. Folio: {folio}"
    )
    wati_send_text(phone_v, cuestionario)
    print(f"[MAX-SEG] Lead {folio} asignado a {nombre_v} ({phone_v})", flush=True)

    # Copia informativa a Javier (solo si el asignado no es Javier)
    if phone_v != JAVIER_PHONE:
        copia = (
            f"*[COPIA — {folio}]*\n"
            f"Lead asignado a *{nombre_v}*\n"
            f"Cliente: {nombre} | WA: {phone}\n"
            f"Busca: {busqueda}\n"
            f"Zona: {zona or '-'} | Presupuesto: {presupuesto or '-'}\n"
            f"Props vistas: {props[:80] if props else 'Ninguna'}\n"
            f"(Solo informativo — {nombre_v} tiene el cuestionario)"
        )
        wati_send_text(JAVIER_PHONE, copia)
        print(f"[MAX-SEG] Copia enviada a Javier", flush=True)

# GOOGLE SHEETS — registro de leads con folio ACIERTA-XXXX
# ------------------------------------------------------------------
REGISTRADOS = {}  # phone -> (folio, timestamp): evita folios duplicados
BITACORA_REGISTRADOS = set()  # phones ya anotados en la bitácora esta sesión

def hora_gdl():
    """Hora actual en Guadalajara (UTC-6) — evita la confusión de ver
    horas en UTC (servidor) en el Sheet cuando se compara con Wati."""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() - 6 * 3600))

def _actualizar_bitacora_con_lead(phone, folio, operacion, interes):
    """Cuando un contacto llega a folio en Leads MAX, regresa a su fila
    original en Bitácora Contactos y anota qué pidió realmente — cierra
    el círculo entre 'llegó' y 'qué quería', sin depender del primer
    mensaje crudo (casi siempre genérico: 'quiero más información')."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        libro = gspread.authorize(creds).open_by_key(SHEET_ID)
        sh = libro.worksheet("Bitácora Contactos")
        celdas = sh.findall(phone, in_column=2)  # columna B = WHATSAPP
        if not celdas:
            return
        fila = celdas[-1].row  # la más reciente si el número aparece varias veces
        resumen = f"{operacion or '?'}: {interes or ''}"[:200]
        sh.update_cell(fila, 5, f"Sí — {folio}")   # columna E: ¿LLEGÓ A LEAD MAX?
        sh.update_cell(fila, 6, resumen)            # columna F: NOTAS
    except Exception:
        import traceback
        print(f"[MAX-ERROR] No se pudo actualizar bitácora con lead {phone}:\n{traceback.format_exc()}", flush=True)

def registrar_contacto_bitacora(phone, primer_mensaje, detectado=""):
    """Anota TODO contacto nuevo desde su primer mensaje, sin filtrar ni
    esperar a que esté calificado. 'Leads MAX' sigue siendo solo los
    calificados (nombre+operación+interés); esta pestaña es el 100%."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return {"registrado": False, "motivo": "Sheets no configurado"}
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        libro = gspread.authorize(creds).open_by_key(SHEET_ID)
        try:
            sh = libro.worksheet("Bitácora Contactos")
        except Exception:
            sh = libro.add_worksheet(title="Bitácora Contactos", rows=5000, cols=6)
            sh.append_row(["FECHA Y HORA", "WHATSAPP", "PRIMER MENSAJE",
                           "CAMPAÑA/CÓDIGO DETECTADO", "¿LLEGÓ A LEAD MAX?", "NOTAS"])
        sh.append_row([hora_gdl(), phone, primer_mensaje[:300],
                       detectado, "", ""])
        return {"registrado": True}
    except Exception as e:
        return {"registrado": False, "motivo": str(e)[:200]}

def registrar_lead(phone, nombre="", interes="", operacion="", presupuesto="",
                   zona="", notas=""):
    # Candado: si este número ya se registró en las últimas 24h,
    # regresar el mismo folio en vez de crear otro.
    previo = REGISTRADOS.get(phone)
    if previo and time.time() - previo[1] < 86400:
        return {"registrado": False, "folio": previo[0],
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
        sh.append_row([folio, hora_gdl(), phone, nombre,
                       operacion, interes, presupuesto, zona, notas, "NUEVO"])
        REGISTRADOS[phone] = (folio, time.time())
        try:
            _actualizar_bitacora_con_lead(phone, folio, operacion, interes)
        except Exception:
            pass  # nunca dejar que esto tumbe el registro del lead ya exitoso
        return {"registrado": True, "folio": folio}
    except Exception as e:
        return {"registrado": False, "motivo": str(e)[:200]}

def avisar_humano(phone, resumen, categoria=None):
    """Escala a Javier/equipo. categoria cambia el encabezado del aviso
    para que sea escaneable de un vistazo (lead normal vs caso especial)."""
    etiquetas = {
        "RECLAMO-PROPIETARIO": "⚠️ RECLAMO DE PROPIETARIO",
        "COLABORACION-AGENTE": "🤝 AGENTE QUIERE COLABORAR",
        "BOLSA-TRABAJO": "📋 INTERÉS EN TRABAJAR AQUÍ",
    }
    encabezado = etiquetas.get(categoria, "🔥 LEAD CALIENTE")
    if HUMAN_HANDOFF:
        wati_send_text(HUMAN_HANDOFF,
            f"{encabezado}\nCliente: {phone}\n{resumen[:600]}")
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
         "desarrollo": {"type": "string", "enum": ["block", "santa_ana", "bellavittoria", "villa_dhara", "eleve"]}},
      "required": ["desarrollo"]}},
    {"name": "buscar_inventario_zmg",
     "description": "Busca en la BOLSA COMPLETA de la ZMG (venta desde $2,000,000 y renta desde $13,000/mes, propias y compartidas). Úsala cuando buscar_propiedades no tenga suficientes opciones, o directamente para búsquedas de compra desde $2M o renta desde $13,000. Usa 'operacion' (VENTA o RENTA) para no mezclar. Si el cliente nombra una COLONIA, fraccionamiento, DESARROLLO/TORRE (ej. 'Madeiras', 'Andares', 'Torre Ágave') o da un CÓDIGO EB (ej. 'EB-VW0579'), usa el parámetro 'texto' con ese nombre o código para filtrar de verdad — el 'texto' busca en título, colonia, código EB y liga. SIEMPRE PUEDES verificar un código EB con esta herramienta: NUNCA le digas al cliente que 'no puedes verificar un código desde aquí' — sí puedes, pon el código EB en 'texto' y busca. NO vuelvas a mostrar la lista genérica del municipio disfrazada de 'colonias vecinas'. Regresa título, precio, recámaras y liga.",
     "input_schema": {"type": "object", "properties": {
         "municipio": {"type": "string", "description": "Guadalajara, Zapopan, Tlaquepaque, Tonalá o Tlajomulco"},
         "operacion": {"type": "string", "enum": ["VENTA", "RENTA"], "description": "VENTA o RENTA — indícalo siempre que sepas cuál busca el cliente"},
         "precio_min": {"type": "number"}, "precio_max": {"type": "number"},
         "recamaras_min": {"type": "number"},
         "tipo": {"type": "string", "description": "casa o departamento"},
         "texto": {"type": "string", "description": "colonia(s), fraccionamiento(s) o palabra(s) clave a buscar dentro del municipio. Si el cliente da VARIAS colonias aceptables, sepáralas por coma: 'Camino Real, Monraz, Virreyes' — encuentra propiedades que coincidan con CUALQUIERA de ellas."},
         "amueblado": {"type": "string", "enum": ["Sí", "No"], "description": "Solo filtra si el cliente lo pidió explícitamente. El dato no siempre está disponible en el registro; si no viene marcado, la propiedad SÍ se incluye (no se descarta por falta de dato)."},
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
    {"name": "enviar_guia",
     "description": "Envía una guía de contenido educativo (AM-GUIA-XX) cuando el cliente escribe su código o pide explícitamente esa guía. Úsala de inmediato, no la resumas tú mismo — el texto oficial ya está aprobado.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string", "enum": ["renta"], "description": "Identificador interno de la guía"}},
      "required": ["nombre"]}},
    {
    "name": "precalificar_credito",
    "description": "Precalifica al prospecto para credito hipotecario y orienta sobre su capacidad real de compra. Usar cuando el cliente mencione credito, Infonavit, mensualidades, enganche, o cuando el presupuesto supere $1,500,000. Devuelve orientacion personalizada: tipo de credito viable, monto estimado, mensualidad aproximada, y si necesita asesor especializado.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ingreso_mensual": {
                "type": "number",
                "description": "Ingreso mensual neto del cliente en pesos MXN (preguntar si no lo sabes)"
            },
            "tiene_imss": {
                "type": "boolean",
                "description": "True si es empleado formal con IMSS activo"
            },
            "tiene_infonavit": {
                "type": "boolean",
                "description": "True si tiene Infonavit activo (subcuenta con saldo)"
            },
            "saldo_infonavit": {
                "type": "number",
                "description": "Saldo aproximado de subcuenta Infonavit en pesos (opcional)"
            },
            "enganche_disponible": {
                "type": "number",
                "description": "Monto de enganche disponible en pesos MXN"
            },
            "precio_objetivo": {
                "type": "number",
                "description": "Precio de la propiedad que le interesa"
            },
            "es_conyugal": {
                "type": "boolean",
                "description": "True si aplica con conyugue o segundo titular"
            }
        },
        "required": ["ingreso_mensual", "precio_objetivo"]
    }
},
{
    "name": "calcular_roi_inversion",
    "description": "Calcula el ROI (retorno de inversion) estimado para una propiedad de inversion. Usar cuando el cliente diga que es para invertir, rentar, o pregunte por rendimiento. Busca rentas similares en el inventario para estimar el ingreso mensual real y calcula ROI, flujo de caja y tiempo de recuperacion.",
    "input_schema": {
        "type": "object",
        "properties": {
            "precio_compra": {
                "type": "number",
                "description": "Precio de compra de la propiedad"
            },
            "municipio": {
                "type": "string",
                "description": "Municipio de la propiedad (Zapopan, Guadalajara, etc)"
            },
            "recamaras": {
                "type": "integer",
                "description": "Numero de recamaras de la propiedad"
            },
            "m2": {
                "type": "number",
                "description": "Metros cuadrados de la propiedad"
            },
            "tiene_amenidades": {
                "type": "boolean",
                "description": "True si tiene alberca, gimnasio u otras amenidades premium"
            },
            "con_credito": {
                "type": "boolean",
                "description": "True si el cliente va a comprar con credito hipotecario (afecta el flujo de caja)"
            },
            "tasa_anual": {
                "type": "number",
                "description": "Tasa anual del credito si aplica (ej. 10.75 para BBVA)"
            },
            "plazo_anos": {
                "type": "integer",
                "description": "Plazo del credito en anos si aplica"
            }
        },
        "required": ["precio_compra", "municipio"]
    }
},
{"name": "registrar_lead",
     "description": "Registra o actualiza el lead en el CRM cuando ya tengas al menos nombre + operación + interés. Úsala UNA vez por conversación cuando el prospecto esté calificado.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"}, "interes": {"type": "string"},
         "operacion": {"type": "string"}, "presupuesto": {"type": "string"},
         "zona": {"type": "string"}, "notas": {"type": "string"}},
      "required": ["nombre", "operacion"]}},
    {"name": "avisar_humano",
     "description": "Notifica al equipo humano de Acierta. Úsala cuando: el cliente pida hablar con una persona, quiera agendar visita, esté listo para ofertar, haga una pregunta legal/fiscal que no debes responder, o sea uno de los CASOS ESPECIALES (reclamo de propietario, agente que quiere colaborar, interés en trabajar aquí).",
     "input_schema": {"type": "object", "properties": {
         "resumen": {"type": "string", "description": "Resumen del cliente y su necesidad"},
         "categoria": {"type": "string", "enum": ["RECLAMO-PROPIETARIO", "COLABORACION-AGENTE", "BOLSA-TRABAJO"],
                      "description": "Solo para casos especiales; omite este campo en leads normales de compra/venta/renta"}},
      "required": ["resumen"]}},
]

SYSTEM_PROMPT = """Eres MAX, el asesor digital de Acierta Max, inmobiliaria con 20 años de experiencia en la Zona Metropolitana de Guadalajara, dirigida por Javier Mendoza. Conversas por WhatsApp en español mexicano, cálido, profesional y BREVE (máximo 3-4 líneas por mensaje; WhatsApp no es para párrafos largos).
""" + (f"""
AGENDA DE CITAS: cuando el cliente quiera agendar cita o visita, además de avisar_humano, compártele esta liga para que elija directamente el día y la hora en la agenda: {CALENDLY_URL} — dile: "Puedes apartar aquí mismo el día y la hora que mejor te acomoden".
""" if CALENDLY_URL else "") + """

TU MISIÓN: entender qué necesita el cliente, mostrarle las mejores opciones del inventario y conectarlo con un asesor humano en el momento correcto. Cliente-céntrico siempre: estás del lado del cliente.

SI EL CLIENTE QUIERE COMPRAR (o rentar para sí) — FLUJO COMPRADOR (eres su COACH, no un buscador — usa SPIN Compacto):

REGLA #0 — NOMBRE PRIMERO (MAXIMA PRIORIDAD, sin excepcion):
En tu SEGUNDO mensaje (despues del saludo inicial), SIEMPRE pregunta el nombre del cliente
si aun no lo sabes. Sin nombre no puedes registrar el lead ni dar seguimiento personalizado.
La forma natural es integrarla en tu respuesta, no como interrogatorio:
BIEN: "Con gusto te ayudo. Me dices tu nombre para darte atencion personalizada?"
BIEN: "Perfecto, busquemos opciones. Como te llamas?"
BIEN: "Claro! Antes de buscar, como te llamas?"
MAL: nunca hagas 3 preguntas juntas (nombre + zona + presupuesto) en el mismo mensaje
MAL: nunca esperes hasta el final de la conversacion para pedir el nombre

Si el cliente da su nombre EN CUALQUIER MOMENTO de la conversacion, llama INMEDIATAMENTE
registrar_lead con los datos que tengas hasta ese momento (aunque sean incompletos —
nombre + telefono ya es suficiente para registrar). Asi capturamos TODOS los prospectos,
incluso los que se van rapido.

EXCEPCION: si el cliente manda un codigo EB (fast-path) o es un mensaje muy corto de
primer contacto ("hola", "info", "opciones"), pregunta el nombre en ese mismo primer
intercambio antes de mostrar fichas o resultados.

SI EL CLIENTE QUIERE COMPRAR (o rentar para sí) — FLUJO COMPRADOR (eres su COACH, no un buscador — usa SPIN Compacto):
1. Dale acceso al catálogo completo: "Puedes ver todo nuestro inventario en https://www.aciertamax.com" (compártelo temprano, es transparencia).
2. Ofrece el diferenciador: "¿Prefieres explorar por tu cuenta, o te doy ATENCIÓN PERSONALIZADA aquí mismo? Puedo hacer contigo un COACHING INMOBILIARIO CON IA: te hago las preguntas correctas y busco exactamente lo que satisface tus necesidades."
3. SITUACIÓN: la cubre el modelo Querer-Poder-Cómo-Cuándo-Dónde (zona, presupuesto, recámaras, uso). No la repreguntes si el cliente ya la dio de golpe.
4. PROBLEMA — ANTES DE TU PRIMERA BÚSQUEDA: agradece los datos que ya diste, pero SIEMPRE agrega UNA pregunta de problema/calidad que no sea pura situación — la que más ayude a acotar: ¿qué es lo que más te ha costado encontrar hasta ahora?, ¿es para vivir o invertir?, ¿algo que no pueda faltar (amenidad, colonia exacta, planta baja)? Si es RENTA, pregunta también si lo busca amueblado o sin muebles (usa el parámetro 'amueblado'). Nunca dispares buscar_inventario_zmg de inmediato solo con precio+zona+recámaras: esos tres datos rara vez acotan lo suficiente en una bolsa de miles. ⚠️ EXCEPCIÓN QUE MANDA SOBRE TODO LO ANTERIOR: esta regla de "pregunta antes de buscar" NO aplica cuando el cliente nombra un DESARROLLO/TORRE/PROPIEDAD específica (ej. "Torre Ágave", "Bella Vittoria") o da un CÓDIGO EB (ej. "EB-VW0579", o cualquier "EB-" seguido de letras/números). En esos casos NO preguntes NADA primero: tu PRIMERA y ÚNICA acción es UNA sola llamada a buscar_inventario_zmg con el parámetro 'texto'=nombre-del-desarrollo (o el código EB exacto). NO hagas múltiples búsquedas paralelas ni secuenciales para el mismo nombre — una sola búsqueda por 'texto' es suficiente porque busca en título, código EB y liga simultáneamente. Si esa búsqueda no encuentra nada, di la verdad honestamente y pide el código EB si el cliente lo tiene — NUNCA hagas una segunda búsqueda diferente en ese mismo turno: genera respuestas contradictorias ("no tengo"... "¡sí hay!") que destruyen la confianza del cliente. Una búsqueda honesta vale más que dos contradictorias.
4e. SI EL CLIENTE DA UN PUNTO DE REFERENCIA en vez de colonia/municipio (ej. "cerca del ITESO", "por Andares", "junto a Plaza del Sol"): pregunta hasta dónde está dispuesto a buscar ("¿solo esa zona, o abrimos a colonias vecinas / todo el municipio?") antes de buscar — no inventes un radio en kilómetros, esa precisión no existe en los datos; usa 'texto' o 'municipio' según lo que el cliente prefiera ampliar.
4b. VÁLVULA DE ESCAPE — deja de preguntar en cuanto veas cualquiera de estas señales: el cliente ya nombró una colonia/propiedad específica y clara, repite algo que ya dijo, muestra señales de impaciencia (mensajes cortos, "ya te dije", "dámelo", signos de exasperación), o pide explícitamente ver la ficha. En ese momento actúa de inmediato (busca con el filtro 'texto' de la colonia que dio, o manda la ficha) — NO hagas otra pregunta de calidad, y NO vuelvas a mostrar una lista genérica que el cliente ya vio. Una pregunta de más en el momento equivocado cuesta la venta.
4d. NUNCA RE-OFREZCAS UNA ZONA O PROPIEDAD QUE EL CLIENTE YA RECHAZÓ EXPLÍCITAMENTE: si el cliente dijo "ya te dije que ahí no", "esa zona no", o similar, esa opción queda descartada por el resto de la conversación — no la vuelvas a sugerir ni con otras palabras. Si no tienes nada que cumpla lo que sí pide, dilo con honestidad ("no tengo opciones exactas en esa zona ahorita") y ofrece registrar su búsqueda o escalar a un asesor — NO insistas en la misma alternativa rechazada una y otra vez, eso agota al cliente más rápido que no tener inventario.
4c. Si el cliente nombra una colonia o fraccionamiento (ej. "Madeiras", "colonias vecinas a X"), usa buscar_inventario_zmg con el parámetro 'texto' para filtrar de verdad — nunca repitas la lista genérica del municipio con otro nombre.
4f. SI EL RESULTADO TRAE "aviso_fuera_de_rango": NUNCA digas "no tengo opciones" o "no hay nada" — di la verdad completa: SÍ hay propiedades en esa zona/colonia, pero fuera del presupuesto pedido, y menciona el precio más cercano. Pregúntale al cliente si quiere verlas de todos modos o prefiere ajustar su rango. Decir "no hay nada" cuando en realidad "hay pero más caro/barato" es un error grave que ya causó pérdida de confianza con un cliente real.
5. SI LA BÚSQUEDA REGRESA MUCHOS RESULTADOS (más de ~15): NO listes las más baratas. Di cuántas hay y pide UNA preferencia más para acotar antes de mostrar la lista. Mejor 5 opciones bien dirigidas que 5 arbitrarias.
6. SI LA BÚSQUEDA REGRESA POCOS O NINGÚN RESULTADO: dilo con honestidad y pregunta cuál criterio prefiere ceder (precio, zona vecina, recámaras) — no decidas tú solo.
7. PROBLEMA otra vez, tras cada reacción del cliente a una opción ("no me convence", "me gusta"): pregunta AL MENOS UNA VEZ el porqué antes de solo buscar más ("¿qué le faltó — tamaño, ubicación, algo más?"). Esto es lo que te distingue de un buscador.
8. IMPLICACIÓN — solo si el cliente YA reveló una urgencia real (renta que vence, familia creciendo, oferta que expira): amplifica con tacto, una sola vez, sin forzar: "y si no encuentras algo a tiempo, ¿qué pasaría con [lo que mencionó]?". Nunca la inventes ni la fuerces si no hay urgencia real en la conversación.
9. NECESIDAD-BENEFICIO — cuando por fin una opción encaje o esté cerca: en vez de enumerar tú las ventajas, pregunta para que el cliente las diga: "si esta cumple con eso, ¿qué te resolvería?" o "¿qué tanto se acerca a lo que buscabas?". Que lo diga él, no tú.
GUÍAS DE CONTENIDO EDUCATIVO (códigos AM-GUIA-XX): si el cliente pide una guía a media conversación (no en el primer mensaje), usa enviar_guia con el nombre correcto. Después de que el sistema ya envió una guía (verás en el historial "[Envié la guía...]"), tu siguiente mensaje debe usar la "pregunta_seguimiento" que trae para ofrecer las opciones y encaminar la conversación: "poner en renta" → flujo CAPTACIÓN-VENDEDOR; "buscar para rentar" → buscar_inventario_zmg con operacion="RENTA"; "ya tienes prospecto" o "administración" → avisar_humano (aún no hay flujo automatizado para estos, escálalos con honestidad). Nunca repitas ni resumas el texto de la guía con tus propias palabras — ya se envió completo y tal cual.

REFERENCIAS A "ESTA/ESE" PROPIEDAD SIN CONTEXTO CLARO: si el cliente dice algo como "de este depa", "de esta propiedad", "la que vi", "el anuncio que vi" — y TÚ no tienes ningún nombre, código EB, ni ficha ya mencionada en la conversación a la que eso pueda referirse — NUNCA lo ignores ni cambies de tema con un pitch genérico de la empresa. IMPORTANTE: casi siempre esto significa que el cliente quiere INFORMACIÓN de una propiedad que vio en un anuncio (quiere COMPRARLA o RENTARLA) — NO asumas que es dueño y quiere VENDERLA/rentarla él, ese es el error opuesto y también grave. El cliente cree que sabes de cuál depa habla (probablemente vio un anuncio específico en Instagram) y tú no. Responde con calidez reconociendo el hueco: "¡Claro! Para mandarte la info exacta, ¿me compartes el nombre de la propiedad, el código que viste en el anuncio (empieza con EB-), o me reenvías la publicación/liga que viste?" — Nunca finjas saber cuál es, ni des una descripción genérica de "un depa bonito", ni preguntes si quiere VENDER cuando lo más probable es que quiera COMPRAR/VER algo que ya vio anunciado.

CASOS ESPECIALES (no son leads normales — trátalos con tacto y escala siempre, con la categoría correcta en avisar_humano):
- "Esta propiedad es mía" / reclamo de propietario sobre un anuncio: discúlpate, NO discutas ni confirmes ni niegues nada tú mismo. Di algo como "Gracias por avisarnos, esto lo debe atender directamente nuestro equipo." Usa avisar_humano con categoria="RECLAMO-PROPIETARIO" y resumen claro (qué anuncio, qué dijo).
- "Soy agente inmobiliario" / quiere colaborar o co-brokear: agradece el interés profesional, sé cordial, y usa avisar_humano con categoria="COLABORACION-AGENTE" — esto lo atiende Javier o el equipo comercial, no lo resuelvas tú con detalles de comisión.
- "Quiero trabajar en Acierta Max" / bolsa de trabajo: agradece el interés, pide nombre y área de interés si lo comparte con gusto, pero NO hagas entrevista ni preguntas de reclutamiento. Usa avisar_humano con categoria="BOLSA-TRABAJO" para que RH lo contacte.

CIERRE HUMANIZADO — solo cuando ACABAS de ejecutar avisar_humano o registrar_lead con éxito en este mismo turno (nunca antes, nunca como promesa adelantada): agradece la preferencia y anuncia que un coach certificado le llama en breve, con calidez y SIN repetir siempre la misma frase — varía entre algo como "Gracias por tu confianza en Acierta Max 🙏 En breve un coach certificado te contacta para acompañarte en todo el proceso." o "Qué gusto que nos elijas. Un coach del equipo te escribe en breve para seguir contigo." Mantén el tono cálido y breve — no es un mensaje aparte largo, cabe en el mismo cierre de la conversación.

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

PROPIEDADES EN CAMPAÑA — LAS 5 SON EXCLUSIVAS DE ACIERTA MAX (no compartidas con otros asesores; el sistema ya envió la ficha oficial si el cliente la mencionó; tú continúa calificando y resolviendo dudas SOLO con estos datos). Por ser exclusivas, empuja con más confianza hacia la cita/visita — no hay competencia de otro asesor por la misma propiedad:
1. THE BLOCK EASY LIVING (también le dicen "el de ITESO"): depto en RENTA $18,000/mes + mant. $2,800. 1 recámara, 2 baños, 65 m², piso 4, amueblado disponible. Periférico Sur 8331, El Mante, Tlaquepaque, junto a ITESO. No aceptan mascotas. Liga oficial: https://www.aciertamax.com/property/iteso-amplio-departamento-nuevo-vista-panoramica-roof-garden-ubicacion-premium?agent=javier373&lang=es
2. SANTA ANA 360: depto en VENTA $1,820,000. 2 recámaras, 2 baños, 53 m², año 2022, estacionamiento techado. Santa Ana Tepetitlán, Zapopan, cerca de Bugambilias. Acepta crédito bancario, INFONAVIT y contado. Pet friendly. Liga oficial: https://www.aciertamax.com/property/departamento-equipado-de-2-recamaras-en-santa-ana-360-cerca-de-bugambilias?agent=javier373&lang=es
3. BELLA VITTORIA: deptos en VENTA desde $3,400,000, A ESTRENAR. 2 recámaras, 2 baños, 70-75 m², 1-2 cajones. Cobre 4232, Lomas de la Victoria, Tlaquepaque, a minutos de Plaza del Sol. Créditos bancarios e INFONAVIT/COFINAVIT, entrega inmediata, registrado ante PROFECO. Liga oficial: https://www.aciertamax.com/property/invierte-en-bella-vittoria-2-recamaras-con-excelente-ubicacion?agent=javier373&lang=es
4. VILLA DHARA (Parque Morelos): loft ÚNICO de doble altura, 1 recámara, 1 baño, 74 m² + terraza privada de 55 m², amueblado, a estrenar (2025), piso 2. Frente al Parque Morelos, El Retiro, Guadalajara. RENTA $14,000/mes (mantenimiento $1,500) o VENTA $2,295,000 (acepta bancarios e INFONAVIT/COFINAVIT). Amenidades: gimnasio, biblioteca, salas de trabajo, ludoteca, huerto urbano, vigilancia 24/7. Cerca de Hospital Civil, Catedral, Línea 3. Ideal ejecutivos, médicos, nómadas digitales, Airbnb. Liga oficial: https://www.aciertamax.com/property/el-departamento-mas-exclusivo-de-villa-dhara-terraza-privada-74-m-amueblado?agent=javier373&lang=es
5. ÉLEVÉ VALLE REAL (solo renta): depto de lujo, 3 recámaras (cada una con baño completo), 3 baños + medio baño, 247 m², 2 cajones + bodega, piso 6, a estrenar. RENTA $40,000/mes + mantenimiento $3,000. Vista al Campo de Golf Las Lomas, Valle Real, Zapopan. Torre de 15 niveles, amenidades: alberca, gimnasio, jacuzzi, salón de usos múltiples, seguridad 24h. Liga oficial: https://www.aciertamax.com/property/extraordinario-departamento-valle-real-torre-de-lujo-eleve-valle-real-zapopan?agent=javier373&lang=es
Para PARQUE MORELOS y el resto del inventario: usa buscar_propiedades.
REGLA CRÍTICA DE LAS PROPIEDADES EN CAMPAÑA: si el cliente pide la ficha, fotos o brochure de una de estas 5, o responde "sí / esa / me interesa" cuando se la ofreciste, usa INMEDIATAMENTE enviar_ficha_campana — NO hagas más preguntas antes, NO la describas de nuevo: mándala. Nota: estas 5 propiedades pueden NO aparecer en buscar_propiedades (el nombre de la zona no coincide); NUNCA digas "no aparece en el sistema": tú ya tienes sus datos aquí y su ficha en enviar_ficha_campana.

INVENTARIO — ORDEN DE BÚSQUEDA:
1. Propiedades en campaña (datos aquí arriba) y buscar_propiedades (inventario propio, venta y renta de todos los precios).
2. buscar_inventario_zmg: la BOLSA COMPLETA de la ZMG (venta desde $2M y renta desde $13,000/mes). Úsala siempre que el cliente compre desde $2M o rente desde $13,000, o cuando el inventario propio no alcance — especifica 'operacion' (VENTA/RENTA) para no mezclar. ¡Con esta herramienta casi siempre HAY opciones: nunca digas "no tengo" sin consultarla!
3. Con propiedades de la bolsa: comparte SOLO los datos del registro (precio, recámaras, baños, m², municipio) + la liga con enviar_ficha_liga. NO inventes amenidades ni detalles: la ficha completa está en la liga. Máximo 3 fichas por turno.
4. CUANDO EL CLIENTE SE REFIERE A UNA OPCIÓN YA MOSTRADA ("la 3", "esa", "la primera", "la de Ciudad Granja"): usa SIEMPRE seleccionar_de_lista con el número de posición — NUNCA repitas datos de memoria ni adivines cuál era. Si el cliente nombra una zona/colonia que NUNCA apareció en tus resultados (tú no la mencionaste ni el cliente la vio en una lista tuya), es una zona NUEVA que el cliente está pidiendo: haz una NUEVA búsqueda con buscar_inventario_zmg filtrando por esa zona. Si esa nueva búsqueda no trae nada, di la verdad ("no tengo opciones en esa colonia exacta ahorita") y ofrece alternativas reales — jamás inventes un nombre de fraccionamiento o desarrollo que ninguna herramienta te dio.

REGLAS DE ORO:
- PROHIBIDO CONFIRMAR ENVÍOS NO VERIFICADOS: NUNCA digas "ya te envie", "listo", "te mande la ficha", "ya va la ficha", "en camino", "ahi te llega" o CUALQUIER variante que dé a entender que una ficha se está mandando o ya se mandó, a menos que acabes de recibir en ESTE MISMO turno el resultado de enviar_ficha_liga o enviar_ficha_campana con "enviada": true, PARA CADA UNA de las fichas de las que hables. El orden correcto es: llama la herramienta PRIMERO, espera su resultado, y SOLO ENTONCES escribe tu mensaje de confirmación (o de disculpa si falló). Nunca redactes el texto de confirmación antes de tener el resultado real. Si vas a mandar 2 o 3 fichas, DEBES llamar la herramienta esa misma cantidad de veces antes de confirmar nada. Si el resultado trae error o "enviada": false, dilo con honestidad ("tuve un problema mandándola, dame un segundo") — jamás confirmes ni anuncies un envío que no verificaste. Afirmar una acción que no ocurrió es tan grave como inventar un dato: rompe la confianza al instante.
- SI PIDES VARIAS FICHAS EN UN TURNO, REVISA CADA RESULTADO POR SEPARADO antes de resumir: si de 2 fichas solo 1 regresó "enviada": true, NO digas "listo, las dos" — di exactamente cuál sí llegó y cuál no ("Te llegó la ficha de La Calma; la de Torre La Cantera tuve un problema, dame un segundo e inténtalo de nuevo"). Nunca generalices un éxito parcial como éxito total.
- NO auto-interpretes un "sí" ambiguo de un mensaje del cliente como consentimiento a una oferta que TÚ apenas estás haciendo en esa misma respuesta (ej. si preguntas "¿te mando las fichas?" y en la misma respuesta ya las diste por enviadas). Si no estás seguro de que el "sí" responde exactamente a tu oferta de fichas, pregunta o espera el siguiente turno del cliente antes de ejecutar el envío.

== FINANCIAMIENTO: CREDITOS HIPOTECARIOS, INFONAVIT Y ESCRITURACION ==
Cuando el cliente pregunte sobre creditos, financiamiento, Infonavit o escrituracion,
responde con estos datos actualizados a julio 2026. SIEMPRE en 3-4 lineas maximo
y SIEMPRE recomienda al final contactar directamente al banco o notario para cotizacion exacta.

CREDITOS HIPOTECARIOS BANCARIOS (julio 2026):
- Tasas fijas: desde 9.5% hasta 12.5% anual segun banco y perfil
- Bancos lideres: Banamex (tasa desde 8.25%, CAT 9.9% - el mas barato),
  Banorte (10.25%), Santander Hipoteca Ya (9.9%), BBVA (10.75%, CAT 14.5%)
- Enganche minimo: 10% a 20% del valor del inmueble
- Plazos: 5 a 20 anos (el mas comun: 15-20 anos)
- Requisitos generales: ingresos comprobables, historial crediticio limpio
  (sin atrasos ultimos 2 anos), 2+ anos de empleo formal
- Indicador clave: comparar el CAT (Costo Anual Total), no solo la tasa nominal.
  El CAT incluye seguros y comisiones — puede significar diferencia de $790,000+
  entre el banco mas barato y el mas caro en un credito de $1.8M a 20 anos
- Simulador oficial gratuito: condusef.gob.mx (compara todos los bancos)

INFONAVIT 2026 (trabajadores afiliados al IMSS):
- Credito tradicional individual: hasta $2,935,002 MXN
- Unamos Creditos (2 derechohabientes): hasta $5,870,000 MXN
- Cofinavit (Infonavit + banco): combina ambos creditos para mayor monto
- Tasa: 10.45% fija anual para todos los niveles salariales
- Plazo maximo: hasta 30 anos (edad + plazo no puede superar 70 anos hombres / 75 mujeres)
- Modelo T100 (nuevo 2026): solo 100 puntos para calificar (antes 1,080).
  Estar en Buro de Credito ya NO impide obtener el credito
- Aplica para vivienda nueva O usada, siempre que este libre de gravamenes
- Precalificacion: infonavit.org.mx (seccion Mi Cuenta Infonavit)
- Credito conyugal: puede combinarse con conyugue que cotice en Fovissste

ESCRITURACION EN JALISCO / GUADALAJARA (datos 2026):
- Costo total: entre 4.21% y 5.78% del valor de la propiedad
  (Jalisco es uno de los estados mas economicos del pais — CDMX cobra hasta 10%)
- Para una propiedad de $1,500,000: aprox $63,000 a $86,700 MXN en gastos
- Para una propiedad de $3,000,000: aprox $126,000 a $173,000 MXN
- Se compone de:
  * ITP/ISAI (Impuesto Traslado Dominio): 2.0% a 3.0% sobre valor catastral
    (el catastral es 40-70% del valor comercial — ventaja fiscal de Jalisco)
  * Honorarios notariales: 0.8% a 1.5% del valor
  * Registro Publico de la Propiedad: 1.5% del valor
  * Avaluo: $1,500 a $5,000 MXN
  * Certificados (libertad de gravamen, predial, agua): $500 a $3,000 MXN
- Quien paga: el COMPRADOR paga los gastos de escrituracion.
  El VENDEDOR paga ISR por su ganancia (si aplica)
- Tiempo estimado del proceso: 2 a 3 meses totales
  (1 semana avaluo, 2-3 semanas escritura, 4-8 semanas inscripcion en Registro)
- IVA: NO aplica en compraventa de vivienda
- Sin escritura inscrita en Registro Publico de la Propiedad, NO eres dueno legal

CUANDO TE PREGUNTEN DE MANTENIMIENTO O GASTOS ADICIONALES:
- Predial anual: generalmente 0.1% a 0.3% del valor catastral (muy bajo en Jalisco)
- Cuotas de mantenimiento (condominios): muy variables, tipicamente $500-$3,000/mes
  segun amenidades (alberca, gimnasio, seguridad 24h elevan la cuota)
- Seguro de casa: aprox 0.1% a 0.3% del valor asegurado por ano
- Siempre preguntar al desarrollo o administracion la cuota exacta antes de comprar

POSTURA DE MAX: MAX orienta y educa — NO es un asesor financiero ni notario.
Siempre recomienda cotizar directamente con el banco (simulador Condusef),
preguntar en Infonavit.org.mx, y consultar un notario para el costo exacto de escrituracion.
Para credito hipotecario, ofrecer conectar con el asesor humano de Acierta Max
que puede orientar segun el perfil especifico del cliente.


== PRECALIFICACION Y ROI — CUANDO Y COMO USAR ==
PRECALIFICAR_CREDITO: Usar cuando el cliente mencione credito, Infonavit, mensualidades,
enganche, o cuando su presupuesto supere $1,500,000. Antes de buscar propiedades, pregunta
de forma natural (maximo 3 preguntas en el mismo mensaje):
- Cuanto ganas aproximadamente al mes? (para saber tu capacidad de credito)
- Tienes Infonavit activo?
- Tienes enganche disponible? (cuanto aproximadamente)
Con esas 3 respuestas llama a precalificar_credito y oriento al cliente sobre su capacidad REAL
antes de mostrarle propiedades que no puede pagar. Nunca preguntes los 3 datos en mensajes separados.

CALCULAR_ROI_INVERSION: Usar SIEMPRE que el cliente diga "es para invertir", "quiero rentarlo",
"que rendimiento da", "cuanto me genera". Llama calcular_roi_inversion con los datos de la
propiedad que le interesa. Presenta el resultado de forma simple:
- Renta estimada mensual
- ROI anual en porcentaje
- Flujo libre mensual (si compra con credito)
- Tiempo de recuperacion
- Semaforo de viabilidad
NUNCA presentes el JSON crudo — convierte los numeros en una explicacion conversacional de
3-4 lineas maximo. Ejemplo: "Para ese depto de $3M, la renta estimada es $18,000/mes,
lo que da un ROI del 7.1% anual. Si compras con credito, tendrias un flujo libre de
$2,400/mes despues de pagar la hipoteca. Muy buen numero para inversion. Quieres que
exploremos los creditos disponibles?"

- REGLA DE ORO CONTRA LA FICHA FANTASMA: cuando el cliente pida una ficha ("ficha", "mándamela", "sí", "ficha técnica", "quiero verla"), tu PRIMERA acción es LLAMAR la herramienta enviar_ficha_liga (o enviar_ficha_campana) con la liga exacta. NUNCA respondas solo con texto diciendo que la enviaste: mencionar la ficha en palabras NO la envía — solo la herramienta la envía. Si te descubres a punto de escribir "ya te llego" sin haber llamado la herramienta en este turno, DETENTE y llama la herramienta. El sistema ahora verifica esto automáticamente: si afirmas un envío que la herramienta no confirmó, tu mensaje será reemplazado por uno honesto y quedará registrado como fallo. Hacerlo bien es simple: herramienta primero, resultado después, confirmación al final.
- PROHIBIDO INVENTAR PROPIEDADES: cada nombre, precio, m² o característica que menciones debe venir literalmente de una respuesta de herramienta (buscar_propiedades, buscar_inventario_zmg, seleccionar_de_lista, o las fichas de campaña). Si el cliente insiste en un nombre que tú nunca dijiste y ninguna búsqueda lo confirma, jamás lo repitas como si existiera: aclara con calma que no tienes esa propiedad exacta disponible en este momento.
- DATOS 100% VERIFICADOS SOLAMENTE: al describir una propiedad, menciona ÚNICAMENTE atributos que las herramientas devolvieron para ESA propiedad específica, o que estén en su ficha de PROPIEDADES EN CAMPAÑA. NUNCA mezcles características de una propiedad con otra (ej. el estacionamiento techado es de Santa Ana 360, NO de Bella Vittoria). Ante CUALQUIER dato del que no estés seguro, no lo afirmes: di "déjame mandarte la ficha oficial con los detalles exactos" y usa enviar_ficha. Un dato inventado destruye la confianza del cliente y de Acierta Max.
- NUNCA MARQUES "✅ CUMPLE" UN REQUISITO QUE TU HERRAMIENTA NO CONFIRMÓ: la bolsa ZMG (buscar_inventario_zmg) solo trae precio, recámaras, baños, m², colonia y amueblado — NO trae terraza, cochera con portón, cuarto de servicio, bodega, seguridad privada ni amenidades. Si el cliente pidió alguno de esos requisitos, NUNCA digas que una propiedad "los cumple" — di algo como "en tamaño y precio calza, pero terraza/cochera/etc. no lo tengo confirmado en el sistema — te mando la ficha oficial para que lo verifiques" y usa enviar_ficha_liga. Afirmar un cumplimiento no verificado es tan grave como inventar la propiedad misma.
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
            # Guardar contexto de busqueda en memoria
            if out.get("propiedades") or out.get("total_coincidencias"):
                props = out.get("propiedades",[])
                titulos = " | ".join(p.get("titulo","")[:40] for p in props[:3] if isinstance(p,dict))
                threading.Thread(target=memoria_guardar, kwargs=dict(
                    phone=phone,
                    OPERACION=args.get("operacion",""),
                    ZONA=args.get("municipio","") or args.get("texto",""),
                    PRESUPUESTO=str(args.get("precio_max","")) if args.get("precio_max") else "",
                    RECAMARAS=str(args.get("recamaras_min","")) if args.get("recamaras_min") else "",
                    ULTIMA_BUSQUEDA=f"{args.get('operacion','')} {args.get('municipio','')} {args.get('texto','')}".strip(),
                    PROPIEDADES_VISTAS=titulos,
                    ESTADO="Buscando"
                ), daemon=True).start()
        elif name == "enviar_ficha_liga":
            out = enviar_ficha_liga(phone, args.get("liga", ""))
        elif name == "seleccionar_de_lista":
            out = seleccionar_de_lista(phone, args.get("numero"))
        elif name == "enviar_guia":
            out = enviar_guia(phone, args.get("nombre", ""))
        elif name == "precalificar_credito":
            out = precalificar_credito(phone, **args)
            # Guardar en memoria que el cliente esta en proceso de credito
            if out.get("viable") is not None:
                threading.Thread(target=memoria_guardar, kwargs=dict(
                    phone=phone,
                    NOTAS_COACHING=f"Credito: {out.get('mejor_opcion','')} | Cap: ${out.get('capacidad_maxima',0):,}",
                    ESTADO="Precalificando"
                ), daemon=True).start()
        elif name == "calcular_roi_inversion":
            out = calcular_roi_inversion(phone, **args)
            # Guardar en memoria que el cliente es inversor
            threading.Thread(target=memoria_guardar, kwargs=dict(
                phone=phone,
                NOTAS_COACHING=f"Inversor | ROI estimado: {out.get('roi_bruto_anual_pct',0)}% | Renta: ${out.get('renta_estimada_mensual',0):,}/mes",
                ESTADO="Perfil-Inversor"
            ), daemon=True).start()
        elif name == "registrar_lead":
            out = registrar_lead(phone, **args)
            # Sincronizar con memoria persistente
            if out.get("registrado"):
                memoria_guardar(phone,
                    NOMBRE=args.get("nombre",""),
                    OPERACION=args.get("operacion",""),
                    PRESUPUESTO=args.get("presupuesto",""),
                    ZONA=args.get("zona",""),
                    ULTIMA_BUSQUEDA=args.get("interes",""),
                    NOTAS_COACHING=args.get("notas",""),
                    ESTADO="Lead-registrado")
                # Notificar al vendedor con cuestionario de seguimiento
                threading.Thread(
                    target=seguimiento_registrar_vendedor,
                    args=(phone, args.get("nombre",""), out.get("folio","")),
                    daemon=True).start()
        elif name == "avisar_humano":
            out = avisar_humano(phone, args.get("resumen", ""), args.get("categoria"))
        else:
            out = {"error": f"herramienta desconocida {name}"}
    except Exception as e:
        out = {"error": f"fallo en {name}: {str(e)[:200]}"}
    print(f"[MAX] Resultado {name}: {json.dumps(out, ensure_ascii=False)[:300]}", flush=True)
    return out

# Herramientas que REALMENTE mandan una ficha por WhatsApp. Si MAX afirma
# haber enviado una ficha pero ninguna de estas devolvió {"enviada": True}
# en el turno, el mensaje es una alucinación y hay que interceptarlo.
FICHA_TOOLS = ("enviar_ficha", "enviar_ficha_liga", "enviar_ficha_campana")

# Frases con las que MAX afirma (falsamente o no) que una ficha ya salio.
# Se usan para detectar confirmaciones de envio en el texto final.
_FRASES_ENVIO = (
    "ya te llego", "ya te llego", "ya te la mande", "ya te la mande",
    "ya te envie", "ya te envie", "te mande la ficha", "te mande la ficha",
    "te envie la ficha", "te envie la ficha", "ya va la ficha", "ahi te llega",
    "ahi te llega", "en camino", "te la acabo de mandar", "te la mando",
    "ya te mande", "ya te mande", "revisa tu whatsapp", "revisa tus mensajes",
    "ya te comparti la ficha", "ya te comparti la ficha", "ya salio la ficha",
    "ya salio la ficha",
)

def _afirma_envio_ficha(texto):
    t = (texto or "").lower()
    return any(f in t for f in _FRASES_ENVIO)

def agent_reply(phone, user_text):
    """Bucle agentico: Claude decide, ejecuta herramientas, responde.

    Incluye un GUARDIA ANTI-ALUCINACION: MAX no puede afirmar que envio una
    ficha si ninguna herramienta de envio devolvio {"enviada": True} en este
    turno. Si lo intenta, el mensaje se corrige por uno honesto - la confianza
    del prospecto vale mas que una confirmacion bonita pero falsa.
    """
    append_history(phone, "user", user_text)
    messages = get_history(phone)
    # Si es la primera respuesta de esta sesion (historial de 1 turno),
    # inyectar memoria previa del prospecto como contexto para MAX
    if len(messages) == 1:
        mem_resumen = memoria_resumen_para_max(phone)
        if mem_resumen:
            # Agregar como mensaje de sistema al inicio del historial
            messages = [{"role": "user",
                         "content": f"[CONTEXTO PREVIO DE ESTE PROSPECTO: {mem_resumen}] "
                                    f"Recuerda esta informacion para personalizar la atencion "
                                    f"sin repetir preguntas ya respondidas."},
                        {"role": "assistant",
                         "content": "Entendido, tengo el contexto de este prospecto y lo usare "
                                    "para darle atencion personalizada sin repetir preguntas."}
                       ] + messages
            print(f"[MAX-MEM] Contexto previo inyectado para {phone}: {mem_resumen[:80]}", flush=True)
    fichas_enviadas_ok = 0   # fichas realmente confirmadas (enviada=True) este turno
    fichas_intentadas = 0    # llamadas a herramientas de ficha, con o sin exito
    ultima_liga = None       # ultima liga vista, para recuperar el envio si hace falta
    for _ in range(6):  # máx 6 vueltas de herramientas
        resp = call_claude(messages)
        content = resp.get("content", [])
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if resp.get("stop_reason") != "tool_use":
            final = "\n".join(t for t in texts if t).strip() or "¿Me repites por favor? 🙂"
            # GUARDIA: si el mensaje afirma un envío de ficha que nunca ocurrió,
            # no dejamos pasar la mentira. Intentamos el envío real o damos la liga.
            if _afirma_envio_ficha(final) and fichas_enviadas_ok == 0:
                print(f"[MAX-GUARDIA] Confirmacion de ficha SIN envio real "
                      f"(intentadas={fichas_intentadas}, liga={bool(ultima_liga)}). "
                      f"Interceptando.", flush=True)
                recuperada = False
                if ultima_liga:
                    # Reintento real de envio antes de rendirnos.
                    try:
                        r = enviar_ficha_liga(phone, ultima_liga)
                        recuperada = bool(r.get("enviada"))
                    except Exception as e:
                        print(f"[MAX-GUARDIA] Reintento fallo: {e}", flush=True)
                if recuperada:
                    final = ("Listo! Ya te mande la ficha con foto y liga oficial.\n"
                             "Que te parece? La vemos con calma o te muestro otra opcion?")
                elif ultima_liga:
                    final = ("Disculpa, tuve un problema tecnico mandandote la ficha. "
                             "Para que no te quedes sin verla, te paso la liga oficial directa:\n"
                             f"{ultima_liga}\n\n"
                             "Le echas un ojo y me dices si te late o te busco otra?")
                else:
                    final = ("Disculpa, tuve un problema tecnico con la ficha. "
                             "Dame un segundo y te la comparto bien, o si prefieres, "
                             "un asesor certificado te la manda hoy mismo con todos los detalles. "
                             "Como prefieres?")
            append_history(phone, "assistant", final)
            return final
        # registrar el turno del asistente con sus tool_use
        messages.append({"role": "assistant", "content": content})
        results = []
        for tu in tool_uses:
            out = run_tool(tu["name"], tu.get("input", {}), phone)
            # Rastrear ligas vistas (busqueda / seleccion / envio) para poder
            # recuperar el envio si MAX alucina la confirmacion mas adelante.
            args = tu.get("input", {})
            if isinstance(args, dict) and args.get("liga"):
                ultima_liga = args["liga"]
            if isinstance(out, dict):
                liga_out = out.get("liga")
                if liga_out:
                    ultima_liga = liga_out
                props = out.get("propiedades")
                if isinstance(props, list) and props and isinstance(props[0], dict) and props[0].get("liga"):
                    ultima_liga = props[0]["liga"]
            # Contabilizar envios de ficha reales.
            if tu["name"] in FICHA_TOOLS:
                fichas_intentadas += 1
                if isinstance(out, dict) and out.get("enviada") is True:
                    fichas_enviadas_ok += 1
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
# ==================================================================
# GUÍAS DE CONTENIDO EDUCATIVO (códigos AM-GUIA-XX)
# Texto EXACTO aprobado por Javier — nunca parafrasear ni inventar.
# ==================================================================
GUIAS = {
    "renta": {
        "codigo": "AM-GUIA-07-RENTA",
        "claves": ["am-guia-07-renta", "guia-07-renta"],
        "texto": (
            "🏠 *RENTAR TU PROPIEDAD SIN UN BUEN PROCESO PUEDE COSTARTE MUCHO MÁS QUE UNA MENSUALIDAD.*\n\n"
            "Tener un inquilino no garantiza recibir la renta puntualmente, cuidar tu patrimonio o "
            "recuperar la propiedad en buenas condiciones.\n\n"
            "Antes de entregar las llaves, considera tres pasos fundamentales:\n"
            "1️⃣ Investiga identidad, ingresos y referencias con la autorización correspondiente.\n"
            "2️⃣ Utiliza un contrato adecuado y define claramente garantías, mantenimiento, servicios y obligaciones.\n"
            "3️⃣ Documenta el inventario, el estado de entrega, los pagos y toda la comunicación.\n\n"
            "En Acierta Max no sólo promovemos propiedades. Podemos ayudarte a investigar al prospecto, "
            "formalizar el arrendamiento, documentar la entrega y dar seguimiento a la administración de tu inmueble.\n\n"
            "🏠 ¿Quieres poner tu propiedad en renta?\n"
            "🔑 ¿Estás buscando una propiedad para rentar?\n"
            "📄 ¿Ya tienes un prospecto y necesitas apoyo?\n"
            "📊 ¿Buscas administración profesional?\n\n"
            "Para solicitar una llamada directa con un asesor, escribe: *\n\n"
            "🌐 www.aciertamax.com — más de 3,000 propiedades disponibles en la ZMG, sujetas a confirmación.\n\n"
            "_NO COMPRES, VENDAS O RENTES SIN TENER CERTEZA._\n"
            "_La investigación de prospectos debe realizarse con su autorización y conforme a las disposiciones "
            "aplicables en materia de privacidad y protección de datos personales._"
        ),
        "pregunta": "¿Cuál de las 4 describe mejor tu situación — poner en renta, buscar para rentar, ya tienes prospecto, o administración?",
    },
}

GUIAS_ENVIADAS = {}  # phone -> set de guías ya enviadas en esta conversación

def detectar_guia(texto):
    t = texto.lower()
    for nombre, g in GUIAS.items():
        if any(k in t for k in g["claves"]):
            return nombre, g
    return None, None

def enviar_guia(phone, nombre):
    g = GUIAS.get(nombre)
    if not g:
        return {"error": f"guía desconocida: {nombre}"}
    if nombre in GUIAS_ENVIADAS.get(phone, set()):
        return {"enviada": False, "nota": "esta guía ya se envió en esta conversación; no la repitas"}
    ok = wati_send_text(phone, g["texto"])
    if not ok:
        return {"enviada": False, "error": "el envío falló; no confirmes al cliente, avisa que hubo un problema"}
    GUIAS_ENVIADAS.setdefault(phone, set()).add(nombre)
    return {"enviada": True, "pregunta_seguimiento": g["pregunta"]}

CAMPANAS = {
    "block": {
        "claves": ["block", "iteso", "the block", "eb-wg7125"],
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
                   "Acierta Max — EXCLUSIVA · Socio AMPI, certificado ✅"),
        "seguimiento": "¿La buscas para ti o para alguien más? Si gustas te agendo una visita esta misma semana 🙌",
    },
    "santa_ana": {
        "claves": ["santa ana", "santaana", "santa ana 360", "eb-wl2602"],
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
                   "Acierta Max — EXCLUSIVA · Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo comprarías con crédito bancario, INFONAVIT o recursos propios? Con eso te digo el paso a paso y te agendo visita 🙌",
    },
    "bellavittoria": {
        "claves": ["bella", "vittoria", "bellavittoria", "eb-vi0277"],
        "foto": "https://assets.easybroker.com/property_images/5810277/101922700/EB-VI0277.png",
        "caption": "🏛 BELLA VITTORIA — Vive el estilo de vida que mereces\n📍 Cobre 4232, Lomas de la Victoria, Tlaquepaque (dentro de Periférico)\n💰 VENTA desde $3,400,000 MXN · 🔑 20 departamentos disponibles",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 70–75 m² · 🚗 1-2 cajones "
                   "(opción con preparación para auto eléctrico) · A ESTRENAR\n\n"
                   "✨ Tenemos *20 departamentos disponibles* — diferentes niveles y vistas, con "
                   "lobby tipo hotel, roof top panorámico equipado, terraza de eventos, "
                   "asadores, juegos infantiles, sala de juegos, seguridad 24h.\n"
                   "📍 A minutos de Plaza del Sol, dentro de Periférico.\n"
                   "💳 Créditos bancarios e INFONAVIT/COFINAVIT · Entrega inmediata · "
                   "Documentación 100% en regla, registrado ante PROFECO.\n\n"
                   "🔗 Ficha completa con fotos y video:\n"
                   "https://www.aciertamax.com/property/invierte-en-bella-vittoria-2-recamaras-con-excelente-ubicacion?agent=javier373&lang=es\n\n"
                   "Acierta Max — EXCLUSIVA · Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo buscas para vivir o como inversión? Hay unidades desde ese precio y te puedo agendar visita al desarrollo esta semana 🙌",
    },
    "villa_dhara": {
        "claves": ["villa dhara", "dhara", "parque morelos", "eb-wg7913"],
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
                   "Acierta Max — EXCLUSIVA · Socio AMPI, certificado ✅"),
        "seguimiento": "Este loft es único en el desarrollo: ¿te interesa para RENTARLO y vivirlo, o para COMPRARLO como inversión (ideal Airbnb)? 🙌",
    },
    "eleve": {
        "claves": ["eleve", "élevé", "valle real", "torre eleve", "eb-wm2996"],
        "foto": "https://assets.easybroker.com/property_images/6112996/108314367/EB-WM2996.jpg",
        "caption": "🏙 ÉLEVÉ VALLE REAL — Exclusiva de Acierta Max en renta\n📍 Valle Real, Zapopan\n💰 RENTA $40,000/mes + mantenimiento $3,000",
        "cuerpo": ("🛏 3 recámaras (cada una con baño completo) · 🛁 3 baños + 1 medio baño · "
                   "📐 247 m² de construcción · 🚗 2 cajones + bodega en sótano · piso 6 · A ESTRENAR\n\n"
                   "✨ Vista directa al Campo de Golf Las Lomas, ventanales de piso a techo, cocina "
                   "con barra de granito equipada, terraza integrada a sala-comedor. Torre de 15 niveles.\n"
                   "🏢 Amenidades: alberca, gimnasio, jacuzzi, salón de usos múltiples, seguridad 24h, "
                   "elevador, circuito cerrado, portero.\n"
                   "📍 Zona Valle Real, una de las más exclusivas de Zapopan.\n\n"
                   "🔗 Ficha completa con las 36 fotos:\n"
                   "https://www.aciertamax.com/property/extraordinario-departamento-valle-real-torre-de-lujo-eleve-valle-real-zapopan?agent=javier373&lang=es\n\n"
                   "Acierta Max — EXCLUSIVA · Socio AMPI, certificado ✅"),
        "seguimiento": "Es una de nuestras exclusivas de mayor nivel — ¿te gustaría agendar una visita esta semana? 🙌",
    },
    "cuarta500": {
        "claves": ["cuarta 500", "eb-tm8375"],
        "foto": None,
        "caption": "🏡 CASA EN VENTA EN CUARTA 500 — Zapopan\n📍 Jardines de Nuevo México, Zapopan\n💰 VENTA $3,200,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños completos + 1 medio baño · 📐 129 m² · 🚗 2 estacionamientos\n\n"
                   "✨ Casa en condominio, roof garden privado, excelente iluminación natural. "
                   "El condominio cuenta con alberca, terraza para eventos, áreas recreativas y seguridad 24/7.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-en-venta-en-cuarta-500?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita, o prefieres que te platique de opciones similares en la zona? 🙌",
    },
    "paneles_solares": {
        "claves": ["paneles solares", "capital norte casa", "eb-uo2612"],
        "foto": None,
        "caption": "⚡ CASA CON PANELES SOLARES — Capital Norte, Zapopan\n📍 Capital Norte, Zapopan\n💰 VENTA $4,000,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 4 baños · 📐 170 m² · 🚗 2 estacionamientos\n\n"
                   "✨ 8 paneles solares, cargador para vehículo eléctrico, elementos de automatización "
                   "para ahorro operativo. Zona residencial en crecimiento.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-con-paneles-solares-en-venta-a-super-precio?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y equipamiento sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita para conocerla? 🙌",
    },
    "coto_pamplona": {
        "claves": ["coto pamplona", "la moraleja", "eb-vf2094"],
        "foto": None,
        "caption": "🔑 CASA EN COTO PAMPLONA — La Moraleja, Zapopan\n📍 Coto Pamplona, La Moraleja, Zapopan\n💰 VENTA $2,990,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños completos + 1 medio baño · 📐 116 m² · 🚗 2 estacionamientos\n\n"
                   "✨ Casa reciente dentro de condominio, por menos de $3 millones. Buena opción para "
                   "primer patrimonio. El desarrollo ofrece alberca y seguridad.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casas-en-venta-en-coto-pamplona-la-moraleja?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿La buscas para vivir o como primer patrimonio? Te puedo mostrar más opciones similares 🙌",
    },
    "san_gonzalo": {
        "claves": ["bosques de san gonzalo", "san gonzalo", "eb-sh4027"],
        "foto": None,
        "caption": "🏠 CASA EN BOSQUES DE SAN GONZALO — Zapopan\n📍 Bosques de San Gonzalo, Zapopan\n💰 VENTA $2,950,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños completos + 1 medio baño · 📐 115 m² · 🚗 2 estacionamientos\n\n"
                   "✨ Casa dentro de coto privado, terraza, vigilancia privada, lista para habitar.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-en-venta-3537e4d3-bf08-4758-b871-57f91038d222?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "madeiras_casa": {
        "claves": ["madeiras", "valle imperial casa venta", "eb-vh7108"],
        "foto": None,
        "caption": "✨ CASA NUEVA EN MADEIRAS — Capital Norte, Zapopan\n📍 Madeiras, Capital Norte / Valle Imperial, Zapopan\n💰 VENTA $4,290,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños completos + 2 medios baños · 📐 133 m² · 🚗 2 estacionamientos\n\n"
                   "✨ A estrenar, con rooftop, cocina integral, dos áreas de TV, área de lavado. "
                   "Al norte de Zapopan, cerca de colegios y vialidades importantes, acceso controlado.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-nueva-en-venta-fraccionamiento-madeiras-capital-norte-zapopan-jalisco?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita esta semana? 🙌",
    },
    "americana_renta": {
        "claves": ["colonia americana renta", "americana depto", "eb-wd4269"],
        "foto": None,
        "caption": "🌆 DEPARTAMENTO AMUEBLADO EN RENTA — Colonia Americana\n📍 Colonia Americana, Guadalajara\n💰 RENTA $17,800/mes",
        "cuerpo": ("🛏 1 recámara · 🛁 1 baño · 📐 52 m² · 🚗 2 estacionamientos · ✅ Mantenimiento incluido\n\n"
                   "✨ Amueblado, cocina equipada, A/C, área de lavado, roof garden. No se aceptan mascotas. "
                   "Cerca de restaurantes, cafeterías y vida cultural.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-en-renta-col-americana-guadalajara-jal-americana?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿La buscas para ti o para alguien más? ¿Te agendo una visita? 🙌",
    },
    "tres_lagos": {
        "claves": ["tres lagos", "lomas de independencia", "eb-wi3326"],
        "foto": None,
        "caption": "🏊 DEPARTAMENTO AMUEBLADO EN RENTA — Tres Lagos\n📍 Tres Lagos, Lomas de Independencia, Guadalajara\n💰 RENTA $17,500/mes",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 70 m² · 🚗 1 estacionamiento techado · "
                   "✅ Mantenimiento e internet incluidos · piso 10\n\n"
                   "✨ Amenidades: alberca semiolímpica, gimnasio, casa club, terraza con asadores, "
                   "salón de eventos, ludoteca.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-amueblado-en-renta-en-el-desarrollo-tres-lagos?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿La buscas para vivir en pareja o familia pequeña? ¿Te agendo una visita? 🙌",
    },
    "soare_solares": {
        "claves": ["soaré solares", "soare solares", "eb-wc2454"],
        "foto": None,
        "caption": "✨ DEPARTAMENTO NUEVO EN RENTA — Soaré Solares\n📍 Soaré Solares, Zapopan\n💰 RENTA $23,900/mes",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 77 m² · 🚗 2 estacionamientos subterráneos · "
                   "✅ Mantenimiento incluido · piso 4\n\n"
                   "✨ A/C, persianas. Torre con gimnasio, coworking, wine bar, pet park, terraza social, "
                   "juegos infantiles, seguridad 24/7.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-en-renta-soare-solares-solares?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita esta semana? 🙌",
    },
    "sendas_residencial": {
        "claves": ["sendas residencial", "eb-wl4728"],
        "foto": None,
        "caption": "🏡 CASA EN RENTA — Sendas Residencial, Capital Norte\n📍 Sendas Residencial, Capital Norte, Zapopan\n💰 RENTA $25,000/mes",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños completos + 1 medio baño · 📐 209 m² · 🚗 2 estacionamientos · "
                   "✅ Mantenimiento incluido\n\n"
                   "✨ Jardín, estudio, preparación para roof garden. Se renta sin amueblar. "
                   "Fraccionamiento con seguridad 24/7, casa club, alberca, gimnasio, terraza, áreas verdes y deportivas.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/la-casa-mas-linda-en-sendas-residencial-capital-norte?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "valle_imperial_casa": {
        "claves": ["imperio bizantino", "eb-wh8200"],
        "foto": None,
        "caption": "🌿 CASA EN RENTA — Valle Imperial\n📍 Coto Imperio Bizantino, Valle Imperial, Zapopan\n💰 RENTA $25,000/mes",
        "cuerpo": ("🛏 3 recámaras · 🛁 3 baños · 📐 240 m² · 🚗 2 estacionamientos · ✅ Mantenimiento incluido · 3 niveles\n\n"
                   "✨ Estudio adaptable (oficina, sala de TV o 4ta recámara), jardín privado, roof garden "
                   "con barra y pérgola, A/C, seguridad 24 horas.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-en-renta-en-valle-imperial-dentro-de-coto-valle-imperial-casa-en-condominio?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita esta semana? 🙌",
    },
    "del_fresno": {
        "claves": ["del fresno", "eb-sl4702"],
        "foto": None,
        "caption": "🏡 DEPARTAMENTO NUEVO EN DEL FRESNO — Guadalajara\n📍 Del Fresno, Guadalajara\n💰 VENTA $2,100,000 MXN",
        "cuerpo": ("🛏 2 recámaras · 🛁 1 baño · 📐 49 m²\n\n"
                   "✨ A estrenar, sala-comedor, cocina integral, conexión para centro de lavado, "
                   "clósets. Buen precio de entrada, cerca de vialidades importantes.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-nuevo-a-estrenar-en-colonia-del-fresno?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo buscas para primer patrimonio o para invertir? 🙌",
    },
    "zona_centro_oblatos": {
        "claves": ["zona centro", "oblatos", "eb-rr9019"],
        "foto": None,
        "caption": "🏙 DEPARTAMENTO NUEVO EN ZONA CENTRO — Guadalajara\n📍 Zona Centro-Oblatos, Guadalajara\n💰 VENTA $2,759,885 MXN",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 58.85 m²\n\n"
                   "✨ Entrega inmediata, estacionamiento subterráneo, elevadores, chapas digitales. "
                   "Amenidades: gimnasio, alberca, asoleaderos, asadores, coworking, seguridad.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamentos-en-zona-centro-de-guadalajara?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo buscas para vivir o como inversión de renta? 🙌",
    },
    "coto_sienna": {
        "claves": ["coto sienna", "sienna", "eb-vh4793"],
        "foto": None,
        "caption": "🏡 CASA NUEVA EN CAPITAL NORTE — Coto Sienna, Zapopan\n📍 Capital Norte, Zapopan\n💰 VENTA $4,450,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 3 baños · 📐 177 m²\n\n"
                   "✨ A estrenar dentro de coto, distribución funcional, estilo contemporáneo, "
                   "zona residencial con crecimiento y plusvalía.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-a-estrenar-en-venta-en-capital-norte-coto-sienna-capital-norte?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "lafayette": {
        "claves": ["lafayette", "eb-wa3089"],
        "foto": None,
        "caption": "🌆 DEPARTAMENTO EN AMERICANA LAFAYETTE — Guadalajara\n📍 Americana Lafayette, Guadalajara\n💰 VENTA $4,150,000 MXN",
        "cuerpo": ("🛏 2 recámaras · 🛁 1 baño · 📐 93 m²\n\n"
                   "✨ Espacios amplios, sala-comedor, cocina integral, balcón, clósets, estacionamiento. "
                   "Zona con gran actividad cultural, gastronómica y urbana.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/excelente-departamento-en-venta-en-colonia-americana-lafayette?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Lo buscas para vivir o para tu portafolio de inversión? 🙌",
    },
    "coto_avellana": {
        "claves": ["coto avellana", "avellana", "eb-oy3981"],
        "foto": None,
        "caption": "🏡 CASA EN COTO AVELLANA — Zapopan\n📍 Coto Avellana, Zapopan (a un costado de Bugambilias)\n💰 VENTA $4,900,000 MXN",
        "cuerpo": ("🛏 3 recámaras · 🛁 2 baños · 📐 200 m²\n\n"
                   "✨ Casa dentro de coto, buena relación precio-ubicación-superficie, "
                   "entorno residencial familiar.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-en-coto-avellana-zapopan?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Precio y disponibilidad sujetos a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "americana_28k": {
        "claves": ["eb-vw1515"],
        "foto": None,
        "caption": "✨ DEPARTAMENTO AMUEBLADO EN RENTA — Colonia Americana\n📍 Colonia Americana, Guadalajara\n💰 RENTA $28,000/mes",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 118.44 m²\n\n"
                   "✨ Completamente amueblado, amplios espacios, sala-comedor, cocina integral. "
                   "Ideal para ejecutivos, parejas o profesionistas.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-en-renta-amueblado-col-americana-guadalajara-jalisco?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "coto_encino": {
        "claves": ["coto encino", "eb-uu6717"],
        "foto": None,
        "caption": "🏡 CASA EN RENTA — Coto Encino, Valle Imperial\n📍 Coto Encino, Valle Imperial, Zapopan\n💰 RENTA $18,000/mes",
        "cuerpo": ("🛏 3 recámaras · 🛁 3 baños · 📐 151 m²\n\n"
                   "✨ Distribución funcional, comunidad residencial tranquila y segura.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/casa-en-renta-dentro-de-coto-encino-en-valle-imperial?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "loft_providencia": {
        "claves": ["loft providencia", "eb-vs9049"],
        "foto": None,
        "caption": "✨ LOFT EN RENTA — Providencia, Guadalajara\n📍 Providencia, Guadalajara\n💰 RENTA $25,500/mes",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 108 m²\n\n"
                   "✨ A estrenar, diseño contemporáneo, espacios amplios. Zona residencial y ejecutiva "
                   "de las más reconocidas de Guadalajara.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-en-renta-a-estrenar-en-providencia-tipo-loft-prados-de-providencia?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "solares_zona_real": {
        "claves": ["zona real", "eb-wj9214"],
        "foto": None,
        "caption": "🌟 DEPARTAMENTO AMUEBLADO EN RENTA — Solares, Zona Real\n📍 Solares, Zona Real, Zapopan\n💰 RENTA $27,000/mes",
        "cuerpo": ("🛏 2 recámaras · 🛁 2 baños · 📐 125 m²\n\n"
                   "✨ Completamente amueblado, espacios generosos, excelente presentación. "
                   "Ideal para ejecutivos, parejas o familias pequeñas.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-amueblado-en-renta-en-solares-zona-real?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
    "americana_18k": {
        "claves": ["eb-tu9644"],
        "foto": None,
        "caption": "🌆 DEPARTAMENTO AMUEBLADO EN RENTA — Colonia Americana\n📍 Colonia Americana, Guadalajara\n💰 RENTA $18,500/mes",
        "cuerpo": ("🛏 1 recámara · 🛁 1 baño · 📐 57 m²\n\n"
                   "✨ Amueblado, ideal para ejecutivo, profesionista o pareja. Cerca de servicios, "
                   "restaurantes y corredores importantes.\n\n"
                   "🔗 Ficha completa con fotos:\nhttps://www.aciertamax.com/property/departamento-amueblado-en-renta-en-la-americana-guadalajara?agent=javier373&lang=es\n\n"
                   "🔵 Propiedad compartida. Renta y disponibilidad sujetas a confirmación.\n"
                   "Acierta Max — Socio AMPI, certificado ✅"),
        "seguimiento": "¿Te gustaría agendar una visita? 🙌",
    },
}

# ------------------------------------------------------------------
# CAMPAÑAS SEMANALES (desde campanas_semana.json — actualizar_campanas.py)
# Se suman a las CAMPANAS curadas a mano arriba, sin pisarlas si el
# mismo código EB ya existe. Solo usa datos reales extraídos de cada
# ficha — nunca inventa amenidades ni descripciones.
# ------------------------------------------------------------------
def _cargar_campanas_semanales():
    try:
        with open("campanas_semana.json", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("[MAX] Sin campanas_semana.json: solo las campañas curadas a mano.", flush=True)
        return
    ebs_existentes = set()
    for c in CAMPANAS.values():
        for clave in c.get("claves", []):
            if clave.startswith("eb-"):
                ebs_existentes.add(clave)
    agregadas = 0
    for p in data.get("propiedades", []):
        eb = (p.get("codigo_eb") or "").lower()
        if not eb or eb in ebs_existentes:
            continue
        clave_interna = f"auto_{eb.replace('eb-', '')}"
        precio = p.get("precio")
        precio_fmt = f"${precio:,.0f} MXN" if precio else "consultar precio"
        unidad = "/mes" if p.get("operacion") == "RENTA" else ""
        partes = []
        if p.get("recamaras"): partes.append(f"🛏 {p['recamaras']} rec")
        if p.get("banos"): partes.append(f"🛁 {p['banos']} baños")
        if p.get("m2"): partes.append(f"📐 {p['m2']} m²")
        if p.get("estacionamientos"): partes.append(f"🚗 {p['estacionamientos']} estacionamientos")
        titulo = p.get("titulo") or f"Propiedad en {p.get('municipio', 'ZMG')}"
        ubicacion = p.get("colonia") or p.get("municipio", "ZMG")
        cp_txt = f" (CP {p['codigo_postal']})" if p.get("codigo_postal") else ""
        amenidades_txt = ""
        if p.get("amenidades"):
            amenidades_txt = f"\n\n✨ {', '.join(p['amenidades'][:8])}"
        CAMPANAS[clave_interna] = {
            "claves": [eb],
            "foto": p.get("foto"),
            "caption": f"🏡 {titulo}\n📍 {ubicacion}{cp_txt}\n💰 {precio_fmt}{unidad} en {p.get('operacion', '')}",
            "cuerpo": (" · ".join(partes) + amenidades_txt +
                      f"\n\n🔗 Ficha completa con fotos:\n{p.get('liga', '')}\n\n"
                      f"🔵 Propiedad compartida. Precio, disponibilidad y amenidades sujetos a confirmación en la ficha oficial.\n"
                      f"Acierta Max — Socio AMPI, certificado ✅"),
            "seguimiento": "¿Te gustaría agendar una visita, o buscamos opciones similares? 🙌",
            "colonia": p.get("colonia"), "codigo_postal": p.get("codigo_postal"),
        }
        agregadas += 1
    print(f"[MAX] Campañas semanales cargadas: {agregadas} nuevas desde campanas_semana.json "
          f"(generadas: {data.get('generado', '?')})", flush=True)

_cargar_campanas_semanales()

def detectar_campana(texto):
    t = texto.lower()
    for nombre, c in CAMPANAS.items():
        if any(k in t for k in c["claves"]):
            return nombre, c
    return None, None

def _resolver_foto(campana):
    """Si la campaña no trae foto fija, la busca en vivo (og:image) de su
    liga oficial — mismo mecanismo probado en enviar_ficha_liga."""
    if campana.get("foto"):
        return campana["foto"]
    liga = campana.get("cuerpo", "")
    m = re.search(r'https://www\.aciertamax\.com/property/\S+', liga)
    if not m:
        return None
    url = m.group(0).rstrip(".,)")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            mm = re.search(r'property="og:image"\s+content="([^"]+)"', r.text) or \
                 re.search(r'content="([^"]+)"\s+property="og:image"', r.text)
            if mm:
                return mm.group(1).replace("&amp;", "&")
    except Exception:
        pass
    return None

def responder_campana(phone, texto, campana):
    """Ficha inmediata (foto + cuerpo + seguimiento) y deja registro
    en la memoria para que el agente continúe con contexto."""
    foto = _resolver_foto(campana)
    ok_img = wati_send_image(phone, foto, campana["caption"]) if foto else False
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
    foto = _resolver_foto(c)
    ok_img = wati_send_image(phone, foto, c["caption"]) if foto else False
    ok_caption = ok_img or wati_send_text(phone, c["caption"])
    ok_cuerpo = wati_send_text(phone, c["cuerpo"])
    if not (ok_caption and ok_cuerpo):
        return {"enviada": False,
                "error": "el envío por WhatsApp falló o solo se completó parcialmente",
                "nota": "NO confirmes al cliente que se la mandaste; dile que hubo un problema técnico"}
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
                          recamaras_min=None, tipo=None, texto=None, operacion=None,
                          amueblado=None, limite=5):
    """Busca en la bolsa compartida ZMG (venta desde $2M, renta desde $13,000/mes).
    Guarda el resultado exacto mostrado a ESTE cliente para poder resolver
    referencias como "la 3" con seleccionar_de_lista, sin inventar nada."""
    if not INVENTARIO_ZMG:
        return {"aviso": "inventario compartido no disponible; usa buscar_propiedades"}
    res = []
    muni_l = (municipio or "").lower()
    tipo_l = (tipo or "").lower()
    texto_l = (texto or "").lower().strip()
    colonias = [c.strip() for c in texto_l.split(",") if c.strip()] if texto_l else []
    op_l = (operacion or "").upper().strip()
    for p in INVENTARIO_ZMG:
        if op_l and p.get("Operación", "").upper() != op_l:
            continue
        if muni_l and muni_l not in p.get("Municipio", "").lower():
            continue
        if tipo_l:
            pt = p.get("Tipo", "").lower()
            if "depa" in tipo_l or "depart" in tipo_l:
                if "departamento" not in pt:
                    continue
            elif "casa" in tipo_l and "casa" not in pt:
                continue
        if colonias:
            # Buscar el término no solo en el título/colonia, sino también en el
            # código EB y en la liga — así "EB-VW0579" o parte de la URL también
            # encuentran la propiedad. Antes solo miraba Título/Colonia, por eso
            # una búsqueda por código EB no hallaba nada aunque la propiedad existiera.
            campos_busqueda = (
                p.get("Título/Colonia", "").lower() + " " +
                (p.get("codigo_eb") or "").lower() + " " +
                (p.get("Liga") or "").lower()
            )
            if not any(c in campos_busqueda for c in colonias):
                continue
        if amueblado is not None:
            am = (p.get("Amueblado") or "").strip()
            quiere_amueblado = str(amueblado).lower() in ("sí", "si", "true", "1", "yes")
            if am and ((quiere_amueblado and am != "Sí") or (not quiere_amueblado and am != "No")):
                continue  # solo excluye cuando el dato SÍ existe y contradice
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
        "titulo": p.get("Título/Colonia") or f"{p.get('Tipo','Propiedad')} en {p.get('Municipio','ZMG')}", "municipio": p.get("Municipio"),
        "tipo": p.get("Tipo"), "precio": p.get("Precio"),
        "recamaras": p.get("Recámaras"), "banos": p.get("Baños"),
        "m2": p.get("m²"), "liga": p.get("Liga"),
    } for i, p in enumerate(mostradas)]
    resultado = {"total_coincidencias": len(res), "propiedades": out,
            "nota": "Guardado como la lista activa de este cliente. Si el cliente responde "
                    "'la 1/2/3...' usa seleccionar_de_lista con ese número — NUNCA inventes "
                    "un nombre de propiedad que no esté en esta lista."}
    # HONESTIDAD DE RANGO: si con el precio no hubo NADA pero la colonia/zona
    # sí tiene inventario fuera de ese rango, decirlo — nunca "no hay nada"
    # cuando en realidad "hay, pero más caro/barato de lo pedido".
    if not res and (precio_min or precio_max) and (colonias or muni_l):
        sin_precio = []
        for p in INVENTARIO_ZMG:
            if op_l and p.get("Operación", "").upper() != op_l:
                continue
            if muni_l and muni_l not in p.get("Municipio", "").lower():
                continue
            if colonias:
                campos_busqueda = (
                    p.get("Título/Colonia", "").lower() + " " +
                    (p.get("codigo_eb") or "").lower() + " " +
                    (p.get("Liga") or "").lower()
                )
                if not any(c in campos_busqueda for c in colonias):
                    continue
            sin_precio.append(p)
        if sin_precio:
            sin_precio.sort(key=lambda x: x.get("Precio") or 0)
            resultado["aviso_fuera_de_rango"] = (
                f"Hay {len(sin_precio)} propiedad(es) que coinciden en zona/colonia, pero "
                f"NINGUNA en el rango de precio pedido. La más cercana: "
                f"{sin_precio[0].get('Título/Colonia')} a ${sin_precio[0].get('Precio'):,.0f}. "
                f"NUNCA digas 'no hay nada' en este caso — dile al cliente que sí hay pero "
                f"fuera de su presupuesto, y pregúntale si quiere verlas o ajustar el rango."
            )
    return resultado

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
    return {"titulo": p.get("Título/Colonia") or f"{p.get('Tipo','Propiedad')} en {p.get('Municipio','ZMG')}", "municipio": p.get("Municipio"),
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
    op_real = p.get("Operación", "VENTA")
    unidad = "/mes" if op_real == "RENTA" else ""
    titulo_prop = (p.get("Título/Colonia") or "").strip() or f"{p.get('Tipo', 'Propiedad')} en {p.get('Municipio', 'ZMG')}"
    caption = (f"🏡 {titulo_prop}\n"
               f"📍 {p.get('Municipio', 'ZMG')}\n"
               f"💰 ${precio:,.0f} MXN{unidad} en {op_real}")
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
    time.sleep(0.4)
    ok_caption = ok_img or wati_send_text(phone, caption)
    time.sleep(0.4)
    ok_cuerpo = wati_send_text(phone, cuerpo)
    if not (ok_caption and ok_cuerpo):
        return {"enviada": False,
                "error": "el envío por WhatsApp falló o solo se completó parcialmente",
                "nota": "NO confirmes al cliente que se la mandaste; dile que hubo un problema técnico y vuelve a intentar o pide un momento"}
    return {"enviada": True, "titulo": p.get("Título/Colonia"),
            "nota": "ficha enviada; continúa la conversación"}


# ------------------------------------------------------------------
# MAX PROACTIVO — 3 momentos de seguimiento automatico
# Corre en thread separado, revisa cada hora
# ------------------------------------------------------------------
SEGUIMIENTO_ENVIADO = {}  # phone -> set de tipos ya enviados (evita spam)

def _max_enviar_seguimiento(phone, tipo, mensaje):
    """Envia mensaje proactivo y lo registra para no repetir."""
    enviados = SEGUIMIENTO_ENVIADO.get(phone, set())
    if tipo in enviados:
        return  # ya se envio este tipo de seguimiento
    try:
        ok = wati_send_text(phone, mensaje)
        if ok:
            enviados.add(tipo)
            SEGUIMIENTO_ENVIADO[phone] = enviados
            # Actualizar estado en memoria
            memoria_guardar(phone, ESTADO=f"Seguimiento-{tipo}")
            print(f"[MAX-PRO] Seguimiento '{tipo}' enviado a {phone}", flush=True)
    except Exception as e:
        print(f"[MAX-PRO] Error enviando seguimiento a {phone}: {e}", flush=True)

def _revisar_seguimientos():
    """Revisa todos los prospectos en memoria y dispara seguimientos."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return
    try:
        libro, _ = _sheets_client()
        if not libro:
            return
        sh = _get_o_crear_hoja(libro, HOJA_MEMORIA, COLS_MEMORIA)
        filas = sh.get_all_records()
        ahora = time.time()

        for fila in filas:
            phone = fila.get("WHATSAPP","").strip()
            if not phone:
                continue
            estado = fila.get("ESTADO","").strip()
            if estado in ("Cerrado","No-contactar","Compro","Rento"):
                continue  # no molestar a prospectos cerrados

            ultima = fila.get("ULTIMA_INTERACCION","")
            if not ultima:
                continue

            # Convertir ultima interaccion a timestamp
            # Sheets puede devolver int, float o string — normalizar primero
            try:
                import datetime
                ultima_str = str(ultima).strip() if ultima else ""
                if not ultima_str:
                    continue
                # Intentar formato "YYYY-MM-DD HH:MM"
                try:
                    dt = datetime.datetime.strptime(ultima_str, "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        dt = datetime.datetime.strptime(ultima_str[:16], "%Y-%m-%d %H:%M")
                    except ValueError:
                        continue
                ts_ultima = dt.timestamp()
            except Exception:
                continue

            horas_sin_contacto = (ahora - ts_ultima) / 3600
            nombre = fila.get("NOMBRE","") or "amigo"
            busqueda = fila.get("ULTIMA_BUSQUEDA","")
            zona = fila.get("ZONA","")
            props_vistas = fila.get("PROPIEDADES_VISTAS","")
            operacion = fila.get("OPERACION","")

            # MOMENTO 1: 24h sin respuesta tras una busqueda activa
            if (24 <= horas_sin_contacto < 48
                    and busqueda
                    and estado not in ("Seguimiento-24h",)):
                msg = (
                    f"Hola {nombre}! Soy MAX de Acierta Max. "
                    f"Quedé pensando en tu busqueda de {busqueda or 'propiedad'}"
                    f"{' en ' + zona if zona else ''}. "
                    f"Han entrado propiedades nuevas al inventario — "
                    f"quieres que te muestre opciones frescas? "
                    f"O si prefieres hablar con un asesor, solo escribe *"
                )
                _max_enviar_seguimiento(phone, "24h", msg)

            # MOMENTO 2: vio propiedades pero no pidio ficha (48h)
            elif (48 <= horas_sin_contacto < 96
                    and props_vistas
                    and "ficha" not in estado.lower()
                    and estado not in ("Seguimiento-48h",)):
                prop_preview = props_vistas.split("|")[0].strip()[:60] if props_vistas else ""
                msg = (
                    f"Hola {nombre}! Te escribo de Acierta Max. "
                    f"Vi que estuviste viendo opciones"
                    f"{' como ' + prop_preview if prop_preview else ''}. "
                    f"Quieres que te mande la ficha completa con fotos y detalles? "
                    f"Solo dime cual te llamo la atencion. "
                    f"Tenemos mas de 3,000 propiedades — seguro encontramos la ideal!"
                )
                _max_enviar_seguimiento(phone, "48h", msg)

            # MOMENTO 3: pidio visita pero no confirmo (72h)
            elif (horas_sin_contacto >= 72
                    and "visita" in estado.lower()
                    and estado not in ("Seguimiento-visita",)):
                msg = (
                    f"Hola {nombre}! MAX de Acierta Max. "
                    f"Quedamos en organizar una visita — "
                    f"como te va con los tiempos? "
                    f"Podemos agendar cuando te acomode: "
                    f"escribe * y te conecto con un asesor "
                    f"o usa este link para apartar fecha: "
                    f"{CALENDLY_URL if CALENDLY_URL else 'aciertamax.com'}"
                )
                _max_enviar_seguimiento(phone, "visita", msg)

    except Exception as e:
        print(f"[MAX-PRO] Error en revision de seguimientos: {e}", flush=True)

def _loop_proactivo():
    """Thread que revisa seguimientos cada hora."""
    while True:
        time.sleep(3600)  # esperar 1 hora
        print("[MAX-PRO] Revisando seguimientos proactivos...", flush=True)
        _revisar_seguimientos()

# Arrancar el thread proactivo al iniciar
_thread_proactivo = threading.Thread(target=_loop_proactivo, daemon=True)
_thread_proactivo.start()
print("[MAX-PRO] Thread proactivo iniciado (revisa cada hora)", flush=True)

# ------------------------------------------------------------------
# PRECALIFICACION CREDITICIA
# ------------------------------------------------------------------
def precalificar_credito(phone, ingreso_mensual, precio_objetivo,
                          tiene_imss=False, tiene_infonavit=False,
                          saldo_infonavit=0, enganche_disponible=0,
                          es_conyugal=False):
    """Calcula capacidad de credito hipotecario y orienta al prospecto."""
    ingreso = float(ingreso_mensual or 0)
    precio  = float(precio_objetivo or 0)
    enganche = float(enganche_disponible or 0)
    saldo_info = float(saldo_infonavit or 0)

    # Regla bancaria: mensualidad max = 30% del ingreso neto
    mensualidad_max = ingreso * 0.30
    # Con tasa promedio 10.75% a 20 anos, factor de pago ~$10.10 por cada $1,000
    FACTOR_PAGO = 10.10 / 1000  # mensualidad por peso de credito
    credito_banco_max = mensualidad_max / FACTOR_PAGO if mensualidad_max > 0 else 0

    # Credito maximo Infonavit 2026
    INFONAVIT_MAX_INDIVIDUAL = 2935002
    INFONAVIT_MAX_CONYUGAL   = 5870000
    infonavit_max = INFONAVIT_MAX_CONYUGAL if es_conyugal else INFONAVIT_MAX_INDIVIDUAL

    # Capacidad total segun escenario
    resultados = []
    viable = False

    # --- ESCENARIO 1: Solo banco ---
    if ingreso > 0:
        cap_banco = credito_banco_max + enganche
        pct_precio = (cap_banco / precio * 100) if precio > 0 else 0
        mens_estimada = (precio - enganche) * FACTOR_PAGO if precio > enganche else 0
        resultados.append({
            "tipo": "Credito bancario",
            "credito_max": round(credito_banco_max),
            "capacidad_total": round(cap_banco),
            "mensualidad": round(mens_estimada),
            "viable": cap_banco >= precio * 0.85,
            "nota": f"Enganche minimo requerido: ${precio*0.15:,.0f} (15%)"
        })
        if cap_banco >= precio * 0.85:
            viable = True

    # --- ESCENARIO 2: Infonavit (si aplica) ---
    if tiene_infonavit and tiene_imss:
        cap_info = infonavit_max + saldo_info + enganche
        resultados.append({
            "tipo": "Infonavit" + (" Unamos Creditos" if es_conyugal else ""),
            "credito_max": round(infonavit_max),
            "capacidad_total": round(cap_info),
            "mensualidad": round((min(precio, infonavit_max) * 0.01045 / 12) * 12 / 12),
            "viable": cap_info >= precio * 0.90,
            "nota": "Tasa fija 10.45% anual. Aplica para vivienda nueva o usada."
        })
        if cap_info >= precio * 0.90:
            viable = True

    # --- ESCENARIO 3: Cofinavit (banco + Infonavit) ---
    if tiene_infonavit and tiene_imss and ingreso > 0:
        cap_cofinavit = min(infonavit_max * 0.5, saldo_info + 500000) + credito_banco_max + enganche
        resultados.append({
            "tipo": "Cofinavit (Infonavit + Banco)",
            "credito_max": round(cap_cofinavit - enganche),
            "capacidad_total": round(cap_cofinavit),
            "mensualidad": round(mensualidad_max * 0.85),
            "viable": cap_cofinavit >= precio * 0.90,
            "nota": "Combina ambos creditos. Mayor poder de compra. Requiere aprobacion de ambas instituciones."
        })
        if cap_cofinavit >= precio * 0.90:
            viable = True

    # Construir respuesta
    brecha = precio - max((r["capacidad_total"] for r in resultados), default=0)
    mejor = max(resultados, key=lambda x: x["capacidad_total"]) if resultados else None

    return {
        "viable": viable,
        "precio_objetivo": precio,
        "ingreso_mensual": ingreso,
        "escenarios": resultados,
        "mejor_opcion": mejor["tipo"] if mejor else "Requiere mas informacion",
        "capacidad_maxima": round(mejor["capacidad_total"]) if mejor else 0,
        "brecha": round(max(brecha, 0)),
        "recomendacion": (
            "Con tu perfil, esta propiedad es viable. Te recomiendo cotizar en Condusef (condusef.gob.mx) para comparar bancos y elegir la mejor tasa. Un asesor de Acierta Max puede acompanarte en el proceso."
            if viable else
            f"Con tu perfil actual la propiedad de ${precio:,.0f} tiene una brecha de ${max(brecha,0):,.0f}. Te puedo mostrar opciones en tu rango real o explorar como ampliar tu capacidad (segundo titular, mayor enganche, o plazo mas largo)."
        ),
        "simulador_condusef": "https://simulador.condusef.gob.mx/credito-hipotecario/",
        "nota": "Esta es una orientacion inicial — no sustituye la evaluacion formal del banco o Infonavit."
    }

# ------------------------------------------------------------------
# CALCULADORA ROI INVERSION INMOBILIARIA
# ------------------------------------------------------------------
def calcular_roi_inversion(phone, precio_compra, municipio,
                            recamaras=2, m2=None, tiene_amenidades=False,
                            con_credito=False, tasa_anual=10.75, plazo_anos=20):
    """Estima ROI de inversion inmobiliaria buscando rentas similares en el inventario."""
    precio = float(precio_compra or 0)
    muni_l = (municipio or "").lower()
    rec    = int(recamaras or 2)
    m2_val = float(m2 or 0)
    if precio == 0:
        return {"error": "Precio de compra requerido"}

    # Buscar rentas similares en el inventario para estimar renta real de mercado
    rentas_similares = []
    for p in INVENTARIO_ZMG:
        if p.get("Operacion","").upper() != "RENTA":
            op = p.get("Operacion","") or p.get("Operación","")
            if op.upper() != "RENTA":
                continue
        muni_p = (p.get("Municipio","") or "").lower()
        if muni_l and muni_l not in muni_p:
            continue
        rec_p = p.get("Recamaras") or p.get("Recámaras")
        try:
            rec_p = int(float(str(rec_p)))
        except Exception:
            rec_p = 0
        if abs(rec_p - rec) > 1:
            continue
        precio_r = p.get("Precio",0)
        try:
            precio_r = float(str(precio_r).replace(",",""))
        except Exception:
            continue
        if precio_r > 0:
            rentas_similares.append(precio_r)

    # Calcular renta estimada
    if rentas_similares:
        rentas_similares.sort()
        # Usar percentil 50 (mediana) para ser conservador
        n = len(rentas_similares)
        renta_estimada = rentas_similares[n // 2]
        fuente = f"mediana de {n} rentas similares en {municipio}"
    else:
        # Estimacion por m² si no hay datos (tipico ZMG: $180-$220/m²/mes)
        if m2_val > 0:
            renta_estimada = m2_val * (220 if tiene_amenidades else 180)
        else:
            renta_estimada = precio * 0.006  # regla empirica: 0.6% mensual
        fuente = "estimacion por metro cuadrado (sin rentas similares en inventario)"

    # Ajuste por amenidades premium
    if tiene_amenidades:
        renta_estimada *= 1.10  # 10% premium por amenidades

    renta_estimada = round(renta_estimada)
    renta_anual = renta_estimada * 12

    # Gastos operativos anuales (conservador)
    gastos_admin       = renta_anual * 0.08   # administracion/comision 8%
    gastos_mantto      = precio * 0.005       # mantenimiento 0.5% valor
    predial            = precio * 0.002       # predial aprox
    vacancia           = renta_anual * 0.08   # 1 mes sin rentar = 8%
    gastos_totales     = gastos_admin + gastos_mantto + predial + vacancia
    flujo_neto_anual   = renta_anual - gastos_totales

    # ROI sobre capital propio
    if con_credito:
        enganche = precio * 0.20  # 20% enganche tipico
        tasa     = float(tasa_anual or 10.75) / 100
        plazo    = int(plazo_anos or 20)
        # Mensualidad hipotecaria
        credito  = precio - enganche
        tasa_m   = tasa / 12
        n_pagos  = plazo * 12
        if tasa_m > 0:
            mensualidad_hip = credito * (tasa_m * (1+tasa_m)**n_pagos) / ((1+tasa_m)**n_pagos - 1)
        else:
            mensualidad_hip = credito / n_pagos
        costo_credito_anual = mensualidad_hip * 12
        flujo_con_credito   = flujo_neto_anual - costo_credito_anual
        roi_capital         = (flujo_con_credito / enganche * 100) if enganche > 0 else 0
        roi_bruto           = (renta_anual / precio * 100)
        capital_invertido   = enganche
    else:
        roi_bruto     = (renta_anual / precio * 100)
        roi_capital   = (flujo_neto_anual / precio * 100)
        flujo_con_credito = flujo_neto_anual
        capital_invertido = precio
        mensualidad_hip = 0

    recuperacion_anos = (capital_invertido / flujo_neto_anual) if flujo_neto_anual > 0 else 99

    # Semaforo de viabilidad
    if roi_bruto >= 7:
        semaforo = "EXCELENTE — rendimiento superior al promedio ZMG"
    elif roi_bruto >= 5:
        semaforo = "BUENO — rendimiento competitivo para la zona"
    elif roi_bruto >= 3.5:
        semaforo = "REGULAR — considerar plusvalia a largo plazo"
    else:
        semaforo = "BAJO — revisar si la plusvalia justifica la inversion"

    return {
        "precio_compra": precio,
        "municipio": municipio,
        "recamaras": rec,
        "renta_estimada_mensual": renta_estimada,
        "fuente_renta": fuente,
        "renta_anual_bruta": round(renta_anual),
        "gastos_anuales_estimados": round(gastos_totales),
        "flujo_neto_anual": round(flujo_neto_anual),
        "roi_bruto_anual_pct": round(roi_bruto, 2),
        "roi_sobre_capital_pct": round(roi_capital, 2),
        "recuperacion_anos": round(recuperacion_anos, 1),
        "semaforo": semaforo,
        "con_credito": con_credito,
        "mensualidad_hipotecaria": round(mensualidad_hip) if con_credito else 0,
        "flujo_mensual_libre": round(flujo_con_credito / 12),
        "recomendacion": (
            f"Renta estimada ${renta_estimada:,}/mes basada en {fuente}. "
            f"ROI bruto {roi_bruto:.1f}% anual. "
            f"{'Flujo positivo de $' + f'{flujo_con_credito/12:,.0f}' + '/mes despues de hipoteca y gastos.' if flujo_con_credito > 0 else 'Flujo negativo con credito — considerar mayor enganche o propiedad de menor precio.'} "
            f"Recuperacion estimada en {recuperacion_anos:.0f} anos."
        ),
        "nota": "Estimacion orientativa con datos del inventario ZMG. Los resultados reales dependen de ocupacion, condiciones del mercado y gastos reales de la propiedad."
    }

# ------------------------------------------------------------------
# WEBHOOK WATI
# ------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    # Si un HUMANO (tú o tu equipo) escribió directo desde Wati, MAX debe
    # enterarse y quedarse en silencio un rato para ese cliente — para no
    # pisar una conversación que ya está siendo atendida en persona.
    if data.get("owner") is True:
        phone_humano = data.get("waId") or ""
        # DIAGNÓSTICO: aquí es donde de verdad hacía falta — ver qué manda
        # Wati cuando Javier interviene (asigna chat + responde), no solo
        # cuando el cliente escribe. Esto explica por qué el silencio de
        # ayer no se activó con este método de intervención.
        print(f"[MAX-DIAGNOSTICO-HUMANO] Evento owner=true completo: {json.dumps(data, ensure_ascii=False)[:1500]}", flush=True)
        if phone_humano:
            HUMANO_ACTIVO[phone_humano] = time.time()
            with CONV_LOCK:
                pendientes_descartados = PENDING.pop(phone_humano, [])
            if pendientes_descartados:
                print(f"[MAX] Se descartaron {len(pendientes_descartados)} mensaje(s) en cola "
                      f"de {phone_humano} por intervención humana.", flush=True)
            print(f"[MAX] Intervención humana detectada con {phone_humano} — "
                  f"pausando respuestas automáticas {COOLDOWN_HUMANO//60} min.", flush=True)
        return jsonify(ok=True)
    phone = data.get("waId") or ""
    text = (data.get("text") or "").strip()
    if not phone:
        return jsonify(ok=True)
    if not text:
        tipo_msg = (data.get("type") or "").lower()
        print(f"[MAX-DIAGNOSTICO-MEDIA] Mensaje sin texto de {phone}, "
              f"type={tipo_msg!r}, claves={list(data.keys())}", flush=True)
        # Si parece ser una imagen/audio/video/documento real (no un evento
        # de estado/entrega), responder con honestidad — NUNCA silencio
        # total, eso se siente como ser ignorado.
        if tipo_msg in ("image", "video", "audio", "document", "sticker", "photo"):
            wati_send_text(phone,
                "¡Hola! 👋 Veo que me compartiste algo (imagen o archivo), pero hoy no "
                "puedo leerlo directamente 🙏. ¿Me escribes el nombre de la propiedad, "
                "el código que empieza con EB-, o la liga del anuncio que viste? "
                "Así te ayudo al instante con la ficha oficial.")
        return jsonify(ok=True)
    # DIAGNÓSTICO TEMPORAL: ver qué campos manda Wati en el payload real,
    # para saber si trae source_url/source_id (el origen del anuncio de
    # Instagram) que hoy no estamos usando. Quitar una vez confirmado.
    print(f"[MAX-DIAGNOSTICO] Claves del payload de {phone}: {list(data.keys())}", flush=True)
    if any(k for k in data.keys() if "source" in k.lower()):
        print(f"[MAX-DIAGNOSTICO] Campos de origen encontrados: "
              f"{ {k: v for k, v in data.items() if 'source' in k.lower()} }", flush=True)
    # Guardar el origen (liga de Instagram) del PRIMER contacto — solo
    # viene en ese mensaje, luego Wati ya no lo repite. Si conocemos ese
    # post, podemos identificar la propiedad exacta que el cliente vio.
    src_url = data.get("sourceUrl")
    if src_url and phone not in ORIGEN_POR_TELEFONO:
        ORIGEN_POR_TELEFONO[phone] = src_url
        print(f"[MAX] Origen de Instagram guardado para {phone}: {src_url}", flush=True)
    # Si hubo intervención humana reciente, MAX se queda callado — un
    # asesor ya está en la conversación, no hay que competir con él.
    ultima_humana = HUMANO_ACTIVO.get(phone)
    if ultima_humana and (time.time() - ultima_humana) < COOLDOWN_HUMANO:
        print(f"[MAX] Silencio por intervención humana reciente con {phone} "
              f"(hace {(time.time()-ultima_humana)/60:.1f} min) — no respondo.", flush=True)
        return jsonify(ok=True, silencio="humano_activo")
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

    # REGLA DE CITA INSTANTÁNEA: si el cliente manda solo "*", tiene
    # prioridad sobre cualquier otro flujo (salvo alertas de fraude,
    # que el agente maneja aparte). Es determinístico, no depende del
    # modelo, y da la liga real de Calendly de una vez.
    if text.strip() == "*":
        def responder_cita():
            print(f"[MAX] Cita instantánea (*) solicitada por {phone}", flush=True)
            if CALENDLY_URL:
                wati_send_text(phone,
                    "Con gusto 🙌 Vamos a programar una llamada con Acierta Max.\n\n"
                    "Puedes apartar aquí mismo el día y la hora que mejor te acomoden:\n"
                    f"{CALENDLY_URL}")
            else:
                wati_send_text(phone,
                    "Con gusto 🙌 Un asesor certificado te contacta en breve para "
                    "programar tu llamada. ¿Cuál es tu nombre?")
            avisar_humano(phone, "Cliente solicitó cita directa con '*'")
            # Registro permanente en el Sheet — nunca depende de que el
            # aviso de WhatsApp se vea a tiempo; queda como historial.
            res = registrar_lead(phone, nombre="(sin nombre, pidió '*')",
                                 operacion="SOLICITÓ LLAMADA",
                                 interes="Pidió cita directa con '*'",
                                 notas="Atajo instantáneo — sin conversación previa")
            print(f"[MAX] Registro de cita '*': {res}", flush=True)
        threading.Thread(target=responder_cita, daemon=True).start()
        return jsonify(ok=True, atajo="cita_instantanea")

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
                    historial = get_history(phone)
                    # BITÁCORA UNIVERSAL: registra TODO contacto desde su
                    # primer mensaje, califique o no después. No depende
                    # del criterio del modelo — es determinístico.
                    if not historial and phone not in BITACORA_REGISTRADOS:
                        try:
                            nombre_c, _c = detectar_campana(texto)
                            nombre_gu, _g = detectar_guia(texto)
                            detectado = nombre_c or (f"guia:{nombre_gu}" if nombre_gu else "")
                            if not detectado:
                                # El texto no reveló nada — al menos deja la
                                # liga de origen cruda, para que Javier pueda
                                # identificar manualmente la publicación
                                # mientras se completa el mapeo.
                                origen = ORIGEN_POR_TELEFONO.get(phone)
                                if origen:
                                    detectado = f"Sin código en texto — vino de: {origen}"
                            registrar_contacto_bitacora(phone, texto, detectado)
                            BITACORA_REGISTRADOS.add(phone)
                        except Exception:
                            import traceback
                            print(f"[MAX-ERROR] Bitácora falló para {phone} (no interrumpe la respuesta):\n{traceback.format_exc()}", flush=True)
                    # FAST-PATH de campañas: SOLO en el PRIMER mensaje de la
                    # conversación (así llegan los clics de anuncios).
                    nombre, campana = detectar_campana(texto)
                    # Respaldo: si el texto es genérico (botón default de
                    # Instagram) pero SÍ sabemos de qué publicación vino
                    # (ORIGEN_POR_TELEFONO), y esa publicación está mapeada
                    # a una campaña conocida, usarla igual.
                    if not campana and not historial:
                        origen = ORIGEN_POR_TELEFONO.get(phone)
                        nombre_mapeado = MAPEO_POST_A_CAMPANA.get(origen) if origen else None
                        if nombre_mapeado and nombre_mapeado in CAMPANAS:
                            nombre, campana = nombre_mapeado, CAMPANAS[nombre_mapeado]
                            print(f"[MAX] Campaña detectada por origen de Instagram: {nombre}", flush=True)
                    if campana and not historial:
                        print(f"[MAX] Campaña detectada: {nombre}", flush=True)
                        responder_campana(phone, texto, campana)
                        continue
                    # FAST-PATH de guías (AM-GUIA-XX): igual, solo primer mensaje.
                    nombre_g, guia = detectar_guia(texto)
                    if guia and not historial:
                        print(f"[MAX] Guía detectada: {nombre_g}", flush=True)
                        wati_send_text(phone, guia["texto"])
                        GUIAS_ENVIADAS.setdefault(phone, set()).add(nombre_g)
                        append_history(phone, "user", texto)
                        append_history(phone, "assistant",
                            f"[Envié la guía {guia['codigo']}] {guia['pregunta']}")
                        continue
                    # FAST-PATH ASTERISCO: si el cliente escribe * (o *humano, *asesor,
                    # *ayuda, etc.), conectamos de inmediato con Javier sin pasar por Claude.
                    _texto_strip = texto.strip()
                    _es_asterisco = (
                        _texto_strip == "*" or
                        _texto_strip.lower() in ("* ", "*humano", "*asesor", "*ayuda",
                                                  "*persona", "*javier", "* asesor",
                                                  "quiero hablar con alguien",
                                                  "hablar con humano", "hablar con persona",
                                                  "me comunicas con alguien")
                    )
                    if _es_asterisco:
                        print(f"[MAX] Fast-path ASTERISCO de {phone}", flush=True)
                        # Obtener resumen del historial para enviarlo a Javier
                        _hist = get_history(phone)
                        _resumen_hist = []
                        for _m in _hist[-10:]:  # ultimos 10 mensajes
                            _rol = "Cliente" if _m.get("role") == "user" else "MAX"
                            _txt = _m.get("content","")
                            if isinstance(_txt, list):
                                _txt = " ".join(t.get("text","") for t in _txt if isinstance(t,dict))
                            if _txt and not _txt.startswith("["):
                                _resumen_hist.append(f"{_rol}: {str(_txt)[:120]}")
                        _resumen = "\n".join(_resumen_hist) if _resumen_hist else "Sin historial previo"
                        # Avisar al vendedor en turno + copia a Javier
                        _v_ast = _siguiente_vendedor()
                        wati_send_text(_v_ast["phone"],
                                f"*[SOLICITUD DE ASESOR — te toco]*\n"
                                f"Cliente: {phone}\n"
                                f"Escribio: {_texto_strip}\n\n"
                                f"*Contexto:*\n{_resumen[:600]}")
                        if _v_ast["phone"] != JAVIER_PHONE:
                            wati_send_text(JAVIER_PHONE,
                                f"*[COPIA — solicitud de asesor]*\n"
                                f"Asignado a *{_v_ast['nombre']}*\n"
                                f"Cliente: {phone} | Escribio: {_texto_strip}")
                        # Responder al cliente
                        wati_send_text(phone,
                            "Perfecto! Ya le avise a un asesor certificado de Acierta Max. "
                            "Te contacta en breve para ayudarte personalmente. "
                            "Un momento por favor!")
                        append_history(phone, "user", texto)
                        append_history(phone, "assistant",
                            "[Fast-path *] Cliente pidio asesor humano. Se notifico a Javier con contexto.")
                        continue
                    # FIN FAST-PATH ASTERISCO

                    # FAST-PATH EB: si el mensaje trae un codigo EB (EB-XXXXXX),
                    # detectamos y mandamos la ficha de inmediato sin pasar por Claude.
                    # Caso de uso: prospecto llega de Instagram/TikTok, ve la clave EB
                    # en la ficha y la escribe al WhatsApp.
                    import re as _re
                    _eb_match = _re.search(r'\bEB-[A-Z0-9]{4,8}\b', texto.upper())
                    if _eb_match:
                        _eb_code = _eb_match.group(0)
                        print(f"[MAX] Fast-path EB: {_eb_code} de {phone}", flush=True)
                        _prop = next((p for p in INVENTARIO_ZMG
                                      if (_eb_code.lower() in (p.get('codigo_eb') or '').lower()
                                          or _eb_code.lower() in (p.get('Liga') or '').lower())), None)
                        if _prop:
                            _liga  = _prop.get('Liga','')
                            _tit   = _prop.get('Titulo/Colonia') or _prop.get('Titulo/Colonia','Propiedad')
                            _precio = _prop.get('Precio','')
                            _rec   = _prop.get('Recamaras') or _prop.get('Recamaras','')
                            _ban   = _prop.get('Banos') or _prop.get('Banos','')
                            _m2    = _prop.get('m2') or _prop.get('m2','')
                            _muni  = _prop.get('Municipio','')
                            _op    = _prop.get('Operacion') or _prop.get('Operacion','')
                            try:
                                _precio_fmt = f"${int(float(_precio)):,}"
                            except Exception:
                                _precio_fmt = str(_precio)
                            _saludo = (
                                f"Hola! Soy MAX de Acierta Max. Vi que te interesa el codigo {_eb_code}.\n\n"
                                f"Te mando la ficha completa ahora mismo!"
                            )
                            wati_send_text(phone, _saludo)
                            _res = enviar_ficha_liga(phone, _liga)
                            if not _res.get('enviada'):
                                wati_send_text(phone,
                                    f"Aqui tienes la ficha: {_liga}\n\n"
                                    f"Tenemos mas de 3,000 propiedades en la ZMG. "
                                    f"Cual es tu nombre y que buscas? Te ayudo!")
                            else:
                                wati_send_text(phone,
                                    f"Tenemos mas de 3,000 propiedades en la ZMG. "
                                    f"Si quieres ver mas opciones o tienes preguntas sobre creditos, "
                                    f"escrituracion o visitas, aqui estoy! Cual es tu nombre?")
                            append_history(phone, "user", texto)
                            append_history(phone, "assistant",
                                f"[Fast-path EB {_eb_code}] Mande saludo + ficha. {_op} {_precio_fmt} {_muni}.")
                            continue
                        else:
                            print(f"[MAX] Fast-path EB: {_eb_code} no en inventario, pasando a Claude", flush=True)
                    # FIN FAST-PATH EB
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
