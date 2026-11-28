"""Agenda compartida de visitas a BellaVittoria y DÚO 24 (pedido por Javier, 27-nov-2026).

El mismo asesor de guardia atiende los dos desarrollos. Cada visita ocupa VISITA_MIN minutos
y, cuando la siguiente cita es en el otro desarrollo, se dejan TRASLADO_MIN minutos para llegar.
Cada cita nueva (o cancelada) se avisa a todos los coaches y a Javier con la agenda completa
del día, para que todos sepan quién va, a qué hora y a qué desarrollo. A las 8:30 sale la
agenda del día al equipo y un recordatorio a cada cliente.

Se conecta desde app.py con  agenda_visitas.registrar(globals()).
"""
import datetime as dt
import os
import re
import threading
import time
import unicodedata

G = {}
HOJA = "Agenda desarrollos"
COLS = ["FOLIO", "FECHA", "HORA", "DESARROLLO", "CLIENTE", "TELEFONO", "PERSONAS", "PAGO", "ESTATUS", "REGISTRO", "NOTAS"]
VISITA_MIN = int(os.environ.get("AGENDA_VISITA_MIN", "40"))
TRASLADO_MIN = int(os.environ.get("AGENDA_TRASLADO_MIN", "10"))
HORA_INI = os.environ.get("AGENDA_HORA_INI", "10:30")
HORA_FIN = os.environ.get("AGENDA_HORA_FIN", "17:00")      # la última visita termina a esta hora
ANTICIPACION_MIN = int(os.environ.get("AGENDA_ANTICIPACION_MIN", "60"))
PASO_MIN = 15
DESARROLLOS = {
    "BELLAVITTORIA": {"nombre": "BellaVittoria Residencial", "corto": "BellaVittoria",
                      "dir": "Cobre 4232, Lomas de la Victoria",
                      "mapa": "https://www.google.com/maps/search/?api=1&query=BellaVittoria%20Residencial%20Cobre%204232"},
    "DUO24": {"nombre": "DÚO 24 New Living", "corto": "DÚO 24",
              "dir": "Av. López Mateos Sur 4255, Loma Bonita, Zapopan",
              "mapa": "https://www.google.com/maps/search/?api=1&query=Duo%2024%20New%20Living%20Lopez%20Mateos%20Sur%204255%20Zapopan"},
}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_LOCK = threading.Lock()
_CACHE = {"t": 0, "filas": []}


def registrar(g):
    G.update(g)
    threading.Thread(target=_loop_diario, daemon=True).start()
    print(f"[MAX-AGENDA] Agenda de desarrollos lista (visita {VISITA_MIN} min, traslado {TRASLADO_MIN} min, {HORA_INI}-{HORA_FIN})", flush=True)


# ---------------------------------------------------------------- utilidades
def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def _ahora():
    return dt.datetime.utcnow() - dt.timedelta(hours=6)       # Guadalajara


def _clave_des(x):
    n = re.sub(r"[^a-z0-9]", "", _norm(x))
    if "duo" in n:
        return "DUO24"
    if "bella" in n or "vittoria" in n or "victoria" in n:
        return "BELLAVITTORIA"
    return ""


def _mins(hhmm):
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", _norm(hhmm))
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2) or 0)
    suf = (m.group(3) or "").replace(".", "")
    if suf == "pm" and h < 12:
        h += 12
    if suf == "am" and h == 12:
        h = 0
    if not suf and 1 <= h <= 7:            # "a las 3" en una agenda de 10:30 a 17:00 = 15:00
        h += 12
    return h * 60 + mm if 0 <= h < 24 and 0 <= mm < 60 else None


def _hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _fecha(x):
    """Acepta AAAA-MM-DD, DD/MM, 'hoy', 'mañana', 'pasado mañana' o un día de la semana."""
    hoy = _ahora().date()
    t = _norm(x)
    if not t or t == "hoy":
        return hoy
    if t in ("manana", "mañana"):
        return hoy + dt.timedelta(days=1)
    if "pasado" in t:
        return hoy + dt.timedelta(days=2)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[/-](\d{1,2})", t)
    if m:
        try:
            d = dt.date(hoy.year, int(m.group(2)), int(m.group(1)))
            return d if d >= hoy else dt.date(hoy.year + 1, d.month, d.day)
        except ValueError:
            return None
    for i, nombre in enumerate(DIAS):
        if _norm(nombre) in t:
            delta = (i - hoy.weekday()) % 7
            return hoy + dt.timedelta(days=delta or (7 if "proximo" in t or "siguiente" in t else 0))
    return None


def _fecha_txt(d):
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


# ---------------------------------------------------------------- hoja
def _hoja():
    libro, _ = G["_sheets_client"]()
    return G["_get_o_crear_hoja"](libro, HOJA, COLS) if libro else None


def _filas(fresco=False):
    if not fresco and time.time() - _CACHE["t"] < 30:
        return _CACHE["filas"]
    filas = []
    try:
        for i, r in enumerate(_hoja().get_all_values()[1:], start=2):
            if r and r[0]:
                d = dict(zip(COLS, r + [""] * (len(COLS) - len(r))))
                d["_fila"] = i
                filas.append(d)
        _CACHE.update(t=time.time(), filas=filas)
    except Exception as e:
        print(f"[MAX-AGENDA] No se pudo leer la agenda: {e}", flush=True)
    return _CACHE["filas"]


def _activas(fecha, fresco=False):
    f = fecha.isoformat()
    return sorted([c for c in _filas(fresco) if c["FECHA"] == f and c["ESTATUS"] not in ("CANCELADA",)],
                  key=lambda c: _mins(c["HORA"]) or 0)


def _choca(inicio, des, citas, ignorar_folio=""):
    """Devuelve la cita con la que choca, o None. Mismo asesor: no hay dos visitas a la vez y
    entre desarrollos distintos se suman los minutos de traslado."""
    for c in citas:
        if c["FOLIO"] == ignorar_folio:
            continue
        otra = _mins(c["HORA"])
        if otra is None:
            continue
        hueco = VISITA_MIN + (TRASLADO_MIN if c["DESARROLLO"] != des else 0)
        if abs(inicio - otra) < hueco:
            return c
    return None


def _minimo(fecha):
    if fecha == _ahora().date():
        a = _ahora()
        return a.hour * 60 + a.minute + ANTICIPACION_MIN
    return -1


def _en_ventana(inicio, fecha):
    return _mins(HORA_INI) <= inicio and inicio + VISITA_MIN <= _mins(HORA_FIN) and inicio >= _minimo(fecha)


def horarios_libres(des, fecha, citas=None):
    citas = _activas(fecha) if citas is None else citas
    libres = []
    m = _mins(HORA_INI)
    while m + VISITA_MIN <= _mins(HORA_FIN):
        if _en_ventana(m, fecha) and not _choca(m, des, citas):
            libres.append(_hhmm(m))
        m += PASO_MIN
    return libres


# ---------------------------------------------------------------- avisos
def texto_agenda(fecha, titulo="📅 AGENDA DE VISITAS"):
    citas = _activas(fecha, fresco=True)
    cab = f"{titulo} — {_fecha_txt(fecha)}\n(BellaVittoria y DÚO 24 · mismo asesor · {VISITA_MIN} min por visita + {TRASLADO_MIN} min de traslado)\n"
    if not citas:
        return cab + "\nSin visitas agendadas."
    lineas = []
    for c in citas:
        d = DESARROLLOS.get(c["DESARROLLO"], {})
        lineas.append(f"• {c['HORA']} · {d.get('corto', c['DESARROLLO'])} · {c['CLIENTE']}"
                      + (f" ({c['PERSONAS']} pers.)" if c["PERSONAS"] else "")
                      + f" · 📱 {c['TELEFONO'][-10:]}"
                      + (f" · {c['PAGO']}" if c["PAGO"] else "")
                      + f" · {c['FOLIO']}")
    return cab + "\n" + "\n".join(lineas)


def _equipo():
    tels = {}
    try:
        tels.update(G["_coaches_red"]())
    except Exception as e:
        print(f"[MAX-AGENDA] No se pudo leer la red de coaches: {e}", flush=True)
    tels[G["_ultimos10"](G["JAVIER_PHONE"])] = "Javier Mendoza"
    return tels


def avisar_equipo(texto, resumen):
    enviados = 0
    for tel10, nombre in _equipo().items():
        try:
            G["notificar_interno"]("521" + tel10, texto, resumen_para_plantilla=resumen[:900], template_name="seguimiento_lead")
            enviados += 1
            time.sleep(1.5)
        except Exception as e:
            print(f"[MAX-AGENDA] No se pudo avisar a {nombre}: {e}", flush=True)
    print(f"[MAX-AGENDA] Agenda enviada a {enviados} del equipo", flush=True)


def _avisar_en_hilo(texto, resumen):
    threading.Thread(target=avisar_equipo, args=(texto, resumen), daemon=True).start()


# ---------------------------------------------------------------- herramienta de MAX
def herramienta(phone, args):
    accion = _norm(args.get("accion") or "disponibilidad")
    des = _clave_des(args.get("desarrollo") or "")
    hoy = _ahora().date()
    base = {"hoy": _fecha_txt(hoy) + f" ({hoy.isoformat()})", "horario_guardia": f"{HORA_INI} a {HORA_FIN}, todos los días"}
    if accion.startswith("cancel"):
        return cancelar(phone, args.get("folio") or "")
    if not des:
        return dict(base, error="Indica el desarrollo: bellavittoria o duo24.")
    fecha = _fecha(args.get("fecha") or "hoy")
    if not fecha:
        return dict(base, error="No entendí la fecha; usa AAAA-MM-DD, 'hoy', 'mañana' o el día de la semana.")
    if fecha < hoy:
        return dict(base, error="Esa fecha ya pasó.")
    d = DESARROLLOS[des]
    if accion.startswith("disp"):
        libres = horarios_libres(des, fecha)
        sig = None
        if not libres:
            for k in range(1, 8):
                f2 = fecha + dt.timedelta(days=k)
                l2 = horarios_libres(des, f2)
                if l2:
                    sig = {"fecha": _fecha_txt(f2) + f" ({f2.isoformat()})", "horarios": l2[:6]}
                    break
        return dict(base, desarrollo=d["nombre"], direccion=d["dir"], fecha=_fecha_txt(fecha) + f" ({fecha.isoformat()})",
                    horarios_libres=libres[:12], siguiente_dia_con_lugar=sig,
                    nota="Propón al cliente dos de estos horarios; cuando confirme, usa accion='agendar'.")
    if accion.startswith("agend"):
        inicio = _mins(args.get("hora") or "")
        nombre = (args.get("nombre") or "").strip() or G["memoria_leer"](phone).get("NOMBRE", "")
        if inicio is None:
            return dict(base, agendada=False, error="Falta la hora (HH:MM).")
        if not nombre:
            return dict(base, agendada=False, error="Falta el nombre del cliente: pídeselo antes de agendar.")
        with _LOCK:
            citas = _activas(fecha, fresco=True)
            libres = horarios_libres(des, fecha, citas)
            otra = _choca(inicio, des, citas)
            if otra or not _en_ventana(inicio, fecha):
                motivo = ("Ese horario ya está ocupado por otra visita del asesor de guardia"
                          + (f" ({otra['HORA']} en {DESARROLLOS.get(otra['DESARROLLO'], {}).get('corto', '')})" if otra else "")
                          if otra else f"Fuera del horario de visitas ({HORA_INI} a {HORA_FIN}) o con menos de {ANTICIPACION_MIN} min de anticipación")
                return dict(base, agendada=False, motivo=motivo, horarios_libres=libres[:8])
            previas = [c for c in _filas() if G["_ultimos10"](c["TELEFONO"]) == G["_ultimos10"](phone)
                       and c["DESARROLLO"] == des and c["ESTATUS"] == "AGENDADA" and c["FECHA"] >= hoy.isoformat()]
            folio = f"AG-{len(_filas()) + 1:04d}"
            try:
                _hoja().append_row([folio, fecha.isoformat(), _hhmm(inicio), des, nombre[:80], G["_ultimos10"](phone),
                                    str(args.get("personas") or ""), (args.get("pago") or "")[:40], "AGENDADA",
                                    G["hora_gdl"](), (args.get("notas") or "")[:300]])
                _CACHE["t"] = 0
            except Exception as e:
                print(f"[MAX-AGENDA] No se pudo guardar la cita: {e}", flush=True)
                return dict(base, agendada=False, error="No se pudo guardar la cita; avisa al equipo con avisar_humano.")
            for c in previas:     # si ya tenía otra cita al mismo desarrollo, queda reagendada
                try:
                    _hoja().update_cell(c["_fila"], COLS.index("ESTATUS") + 1, "REAGENDADA")
                except Exception:
                    pass
            _CACHE["t"] = 0
        titulo = f"🆕 VISITA AGENDADA {folio}: {nombre} → {d['corto']} {_fecha_txt(fecha)} {_hhmm(inicio)}"
        texto = titulo + "\n\n" + texto_agenda(fecha)
        if fecha != hoy and _activas(hoy):
            texto += "\n\n" + texto_agenda(hoy, "📅 Y HOY")
        _avisar_en_hilo(texto, f"{titulo}. Agenda del día: " + texto_agenda(fecha).split("\n", 2)[-1].replace("\n", " "))
        print(f"[MAX-AGENDA] {folio} {des} {fecha} {_hhmm(inicio)} {nombre} ({G['_ultimos10'](phone)})", flush=True)
        return dict(base, agendada=True, folio=folio, desarrollo=d["nombre"], fecha=_fecha_txt(fecha), hora=_hhmm(inicio),
                    direccion=d["dir"], mapa=d["mapa"],
                    nota="Confirma al cliente día, hora, dirección y la liga del mapa; dile que el asesor de guardia lo espera y que el mismo día le llega un recordatorio.")
    return dict(base, error="accion debe ser disponibilidad, agendar o cancelar")


def cancelar(phone, folio=""):
    t10 = G["_ultimos10"](phone)
    hoy = _ahora().date().isoformat()
    citas = [c for c in _filas(fresco=True) if c["ESTATUS"] == "AGENDADA" and c["FECHA"] >= hoy
             and (c["FOLIO"] == folio.upper() if folio else G["_ultimos10"](c["TELEFONO"]) == t10)]
    if not citas:
        return {"cancelada": False, "error": "No encontré una cita vigente de este cliente."}
    c = citas[0]
    try:
        _hoja().update_cell(c["_fila"], COLS.index("ESTATUS") + 1, "CANCELADA")
        _CACHE["t"] = 0
    except Exception as e:
        return {"cancelada": False, "error": f"No se pudo cancelar: {e}"}
    f = dt.date.fromisoformat(c["FECHA"])
    titulo = f"❌ VISITA CANCELADA {c['FOLIO']}: {c['CLIENTE']} → {DESARROLLOS.get(c['DESARROLLO'], {}).get('corto', '')} {_fecha_txt(f)} {c['HORA']}"
    _avisar_en_hilo(titulo + "\n\n" + texto_agenda(f), titulo)
    return {"cancelada": True, "folio": c["FOLIO"], "fecha": _fecha_txt(f), "hora": c["HORA"]}


# ---------------------------------------------------------------- envío diario
def _loop_diario():
    time.sleep(90)
    enviado = ""
    while True:
        try:
            a = _ahora()
            hoy = a.date().isoformat()
            if a.hour * 60 + a.minute >= 8 * 60 + 30 and enviado != hoy:
                ya = G["memoria_leer"]("SISTEMA_AGENDA").get("ULTIMA_AGENDA_DIARIA", "")
                enviado = hoy
                if ya != hoy:
                    G["memoria_guardar"]("SISTEMA_AGENDA", ULTIMA_AGENDA_DIARIA=hoy)
                    citas = _activas(a.date(), fresco=True)
                    if citas:
                        avisar_equipo(texto_agenda(a.date(), "📅 AGENDA DE VISITAS DE HOY"),
                                      "Agenda de hoy: " + texto_agenda(a.date()).split("\n", 2)[-1].replace("\n", " "))
                        for c in citas:
                            d = DESARROLLOS.get(c["DESARROLLO"], {})
                            try:
                                G["wati_send_text"]("521" + G["_ultimos10"](c["TELEFONO"]),
                                                    f"¡Hola, {c['CLIENTE'].split()[0]}! 👋 Te recuerdo tu visita de hoy a {d.get('nombre', '')} a las {c['HORA']} "
                                                    f"en {d.get('dir', '')}. 🗺️ {d.get('mapa', '')}\nSi necesitas cambiar la hora, contéstame aquí.")
                            except Exception:
                                pass
        except Exception as e:
            print(f"[MAX-AGENDA] Error en el envío diario: {e}", flush=True)
        time.sleep(300)
