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
import math
import json
import time
import random
import datetime
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
GMAIL_USER          = os.environ.get("GMAIL_USER", "")
GMAIL_PASS          = os.environ.get("GMAIL_PASS", "")

# ------------------------------------------------------------------
# EQUIPO DE VENDEDORES — rotacion round-robin
# Javier recibe copia de TODOS los leads siempre.
# Los demas reciben solo el que les toco en turno.
# ------------------------------------------------------------------
VENDEDORES = [
    {"nombre": "Javier Mendoza",  "phone": os.environ.get("VENDEDOR_JAVIER",  "3325773277")},
    {"nombre": "Ubaldo Mendez",   "phone": os.environ.get("VENDEDOR_UBALDO",  "3319128128")},
    {"nombre": "Leticia Enriquez","phone": os.environ.get("VENDEDOR_LETICIA", "3316183775")},
    {"nombre": "Gloria Navarro",  "phone": os.environ.get("VENDEDOR_GLORIA",  "3331270050")},
    {"nombre": "Paola Valencia",  "phone": os.environ.get("VENDEDOR_PAOLA",   "3338998750")},
]
JAVIER_PHONE = VENDEDORES[0]["phone"]  # siempre recibe copia de todo
_TURNO_LOCK = threading.Lock()
_turno_actual = [0]  # indice en VENDEDORES, compartido entre threads

def _siguiente_vendedor():
    """Retorna el vendedor al que le toca este lead (round-robin).
    IMPORTANTE: no usa un contador en memoria -- ese contador se reinicia
    a cero en cada redeploy de Render (bug real: durante una sesión con
    muchos redeploys, SIEMPRE volvía a tocarle a Javier). En vez de eso,
    lee el último vendedor asignado en el Sheet CRM AIDA y regresa el
    siguiente en la lista -- así el turno sobrevive a cualquier reinicio."""
    ultimo_nombre = None
    try:
        sh = _crm_sheet()
        valores = sh.get_all_values()
        if len(valores) > 1:
            idx_vendedor = CRM_COLUMNAS.index("VENDEDOR")
            # Recorre de abajo hacia arriba buscando la última fila con
            # un nombre de vendedor válido (por si la última fila está
            # incompleta o a medio escribir).
            for fila in reversed(valores[1:]):
                if idx_vendedor < len(fila) and fila[idx_vendedor].strip():
                    ultimo_nombre = fila[idx_vendedor].strip()
                    break
    except Exception as e:
        print(f"[MAX-ROTACION] No se pudo leer el último vendedor del Sheet: {e}", flush=True)

    if ultimo_nombre:
        nombres = [v["nombre"] for v in VENDEDORES]
        if ultimo_nombre in nombres:
            siguiente_idx = (nombres.index(ultimo_nombre) + 1) % len(VENDEDORES)
            return VENDEDORES[siguiente_idx]
    # Sin historial legible (arranque en limpio) -- empieza en el primero.
    return VENDEDORES[0]

# ------------------------------------------------------------------
# CRM AIDA — Atencion / Interes / Deseo / Accion
# Un solo Sheet ("CRM AIDA") es la fuente de verdad de en qué fase va
# cada cliente, quién lo lleva, y qué falta. Javier lo puede ver y
# editar directamente en cualquier momento -- esa ES su supervisión.
# ------------------------------------------------------------------
CRM_HOJA = "CRM AIDA"
CRM_COLUMNAS = ["FOLIO", "FASE", "TELEFONO_CLIENTE", "NOMBRE_CLIENTE",
                "VENDEDOR", "VENDEDOR_PHONE", "PROPIEDADES",
                "CONTACTO_CLIENTE", "CONTACTO_ORIGINADOR", "FECHA_HORA_CITA",
                "VISITA_RESULTADO", "OPERACION", "DOCUMENTACION",
                "CONCLUIDO", "CREADO", "ULTIMA_ACCION", "PROXIMO_SEGUIMIENTO_TS",
                "VISITA_ACTIVA", "ULTIMA_UBICACION_TS", "ALERTA_ENVIADA",
                "CARPETA_CLIENTE_URL", "INTENTOS_SEGUIMIENTO", "CHAT_COMPLETO",
                "ESCALADO_A_JAVIER", "ULTIMA_RESPUESTA_VENDEDOR",
                # Checklist de visita (originador) -- pedido por Javier 2026-09-29
                "ORIGINADOR_NOMBRE", "ORIGINADOR_TEL", "ORIGINADOR_COMISION",
                "ORIGINADOR_PUNTUAL", "ORIGINADOR_INFO_OK", "ORIGINADOR_FICHA_OK",
                "ORIGINADOR_EXTRANOS",
                # Checklist de visita (cliente) y acuerdos
                "CLIENTE_CAPACIDAD", "CLIENTE_OBLIGADO_SOLIDARIO", "CLIENTE_INTERES",
                "VISITA_ACUERDOS"]
INTENTOS_ANTES_DE_ESCALAR = 2  # ~5 horas de silencio (3h + 2h) antes de avisarte a ti
SEGURIDAD_HOJA = "Seguridad Vendedores"
SEGURIDAD_COLUMNAS = ["FOLIO_CRM", "VENDEDOR", "VENDEDOR_PHONE", "FECHA_HORA",
                      "LATITUD", "LONGITUD"]
MINUTOS_ENTRE_UBICACION = 30  # cadencia pedida al vendedor
MINUTOS_TOLERANCIA_ALERTA = 45  # margen antes de avisar a Javier (30 + colchón)

# ------------------------------------------------------------------
# REFERENCIAS A BETTY — responsable de crédito. Cuando un cliente va a
# necesitar crédito bancario y/o Infonavit, se le avisa que sus datos se
# comparten con Betty, se le pide a ella que lo contacte, y se da
# seguimiento a AMBOS lados (Betty y, si hace falta, recordar al cliente)
# hasta que quede contactado -- con copia a Javier siempre.
# ------------------------------------------------------------------
BETTY_HOJA = "Referencias Betty"
BETTY_COLUMNAS = ["FOLIO", "TELEFONO_CLIENTE", "NOMBRE_CLIENTE", "NECESIDAD",
                  "CONTACTO_BETTY", "CREADO", "ULTIMA_ACCION",
                  "PROXIMO_SEGUIMIENTO_TS", "CONCLUIDO", "INTENTOS_SEGUIMIENTO",
                  "ESCALADO_A_JAVIER"]

def _betty_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    libro = gspread.authorize(creds).open_by_key(SHEET_ID)
    try:
        return libro.worksheet(BETTY_HOJA)
    except Exception:
        sh = libro.add_worksheet(title=BETTY_HOJA, rows=2000, cols=len(BETTY_COLUMNAS))
        sh.append_row(BETTY_COLUMNAS)
        return sh

def referir_a_betty(phone_cliente, nombre_cliente, necesidad):
    """Registra la referencia y avisa a Betty (con copia a Javier).
    `necesidad` describe brevemente qué busca el cliente: 'crédito
    bancario', 'Infonavit', 'Cofinavit', etc."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return {"referido": False, "motivo": "Sheets no configurado"}
    try:
        sh = _betty_sheet()
        n = len(sh.get_all_values())
        folio_betty = f"BETTY-{n:04d}"
        proximo = time.time() + (3 * 3600)  # primer check-in a Betty: 3 horas
        sh.append_row([folio_betty, phone_cliente, nombre_cliente, necesidad,
                       "Pendiente", hora_gdl(), hora_gdl(), str(proximo), "No", "0", "No"])
        msg_betty = (
            f"🏦 NUEVO CLIENTE PARA CRÉDITO — {folio_betty}\n\n"
            f"Cliente: {nombre_cliente}\n"
            f"WhatsApp: {phone_cliente}\n"
            f"Necesita: {necesidad}\n\n"
            f"Por favor contáctalo. En unas horas te pregunto si ya lo lograste."
        )
        wati_send_text(BETTY_PHONE, msg_betty)
        if JAVIER_PERSONAL:
            wati_send_text(JAVIER_PERSONAL,
                f"📋 COPIA — Se refirió a {nombre_cliente} ({phone_cliente}) con Betty "
                f"para {necesidad}. Folio: {folio_betty}")
        return {"referido": True, "folio_betty": folio_betty}
    except Exception as e:
        return {"referido": False, "motivo": str(e)[:200]}

def _betty_buscar_pendiente(texto=""):
    """Igual que con vendedores: si Betty tiene un solo caso activo, es
    ese; si tiene varios, usa el folio si lo menciona; si no lo
    menciona, regresa ambiguo en vez de adivinar."""
    sh = _betty_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return None, None, []
    headers = valores[0]
    activos = []
    for idx in range(len(valores) - 1, 0, -1):
        fila = dict(zip(headers, valores[idx] + [""] * (len(headers) - len(valores[idx]))))
        if fila.get("CONCLUIDO") != "Si":
            activos.append((idx + 1, fila))
    if not activos:
        return None, None, []
    m = re.search(r"BETTY-\d{4}", (texto or "").upper())
    if m:
        for fila_num, fila in activos:
            if fila.get("FOLIO") == m.group(0):
                return fila_num, fila, activos
    if len(activos) == 1:
        return activos[0][0], activos[0][1], activos
    return None, None, activos

def betty_procesar_respuesta(texto):
    """Parser determinístico de la respuesta de Betty a un check-in."""
    fila_num, registro, activos = _betty_buscar_pendiente(texto)
    if not activos:
        return False
    if not registro:
        lista = "\n".join(f"• {f.get('FOLIO')} — {f.get('NOMBRE_CLIENTE')}" for _, f in activos)
        wati_send_text(BETTY_PHONE,
            f"Tienes más de un caso pendiente, ¿a cuál te refieres? Contéstame con el folio:\n{lista}")
        return True
    sh = _betty_sheet()
    col = {h: i + 1 for i, h in enumerate(BETTY_COLUMNAS)}
    sh.update_cell(fila_num, col["INTENTOS_SEGUIMIENTO"], "0")
    sh.update_cell(fila_num, col["ESCALADO_A_JAVIER"], "No")
    t = texto.strip().lower()

    if any(p in t for p in ["si", "sí", "ya", "listo", "contactado", "lo contacté", "la contacté"]):
        sh.update_cell(fila_num, col["CONTACTO_BETTY"], "Si")
        sh.update_cell(fila_num, col["CONCLUIDO"], "Si")
        sh.update_cell(fila_num, col["ULTIMA_ACCION"], hora_gdl())
        wati_send_text(BETTY_PHONE, "Perfecto, gracias por confirmar 🙌")
        if JAVIER_PERSONAL:
            wati_send_text(JAVIER_PERSONAL,
                f"✅ Betty ya contactó a {registro.get('NOMBRE_CLIENTE')} ({registro.get('FOLIO')})")
        return True
    if any(p in t for p in ["no", "no he podido", "aún no", "todavía no"]):
        sh.update_cell(fila_num, col["CONTACTO_BETTY"], "No")
        sh.update_cell(fila_num, col["ULTIMA_ACCION"], hora_gdl())
        proximo = time.time() + (2 * 3600)
        sh.update_cell(fila_num, col["PROXIMO_SEGUIMIENTO_TS"], str(proximo))
        wati_send_text(BETTY_PHONE, "Entendido, te pregunto de nuevo en un rato. Gracias 👍")
        return True
    return False

def _betty_revisar_seguimientos():
    """Corre en el mismo ciclo de 1 hora que el resto del CRM."""
    sh = _betty_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return
    headers = valores[0]
    col = {h: i + 1 for i, h in enumerate(headers)}
    ahora = time.time()
    for idx in range(1, len(valores)):
        fila = dict(zip(headers, valores[idx] + [""] * (len(headers) - len(valores[idx]))))
        if fila.get("CONCLUIDO") == "Si":
            continue
        try:
            proximo = float(fila.get("PROXIMO_SEGUIMIENTO_TS") or 0)
        except ValueError:
            continue
        try:
            intentos = int(fila.get("INTENTOS_SEGUIMIENTO") or 0)
        except ValueError:
            intentos = 0
        if intentos >= INTENTOS_ANTES_DE_ESCALAR:
            if fila.get("ESCALADO_A_JAVIER") != "Si":  # solo se avisa UNA vez
                if JAVIER_PERSONAL:
                    notificar_interno(
                        JAVIER_PERSONAL,
                        f"⚠️ Betty no atendió sobre {fila.get('NOMBRE_CLIENTE')} ({fila.get('FOLIO')}). "
                        f"Por favor interven directamente.",
                        resumen_para_plantilla=(f"Betty no atendió | "
                            f"Cliente: {fila.get('NOMBRE_CLIENTE')} | Necesidad: {fila.get('NECESIDAD')} | "
                            f"Folio: {fila.get('FOLIO')}"),
                        template_name="seguimiento_lead")
                sh.update_cell(idx + 1, col["ESCALADO_A_JAVIER"], "Si")
            continue
        if proximo and ahora >= proximo:
            notificar_interno(
                BETTY_PHONE,
                f"Hola! Seguimiento {fila.get('FOLIO')} — ¿ya contactaste a "
                f"{fila.get('NOMBRE_CLIENTE')} para lo de crédito? (sí/no)",
                resumen_para_plantilla=(f"Cliente: {fila.get('NOMBRE_CLIENTE')} | "
                    f"Necesidad: {fila.get('NECESIDAD')} | Intento #{intentos + 1} | "
                    f"Folio: {fila.get('FOLIO')}"),
                template_name="seguimiento_lead")
            sh.update_cell(idx + 1, col["PROXIMO_SEGUIMIENTO_TS"], str(ahora + 2 * 3600))
            sh.update_cell(idx + 1, col["INTENTOS_SEGUIMIENTO"], str(intentos + 1))

def _crm_sheet():
    """Abre (o crea, con encabezados) la pestaña CRM AIDA. Si ya existía
    con menos columnas de las que ahora tiene CRM_COLUMNAS (columnas
    agregadas después), extiende el encabezado físico -- puramente
    cosmético para que se vea bien al revisar el Sheet a mano, ya que
    _crm_fila_a_dict ya no depende de este encabezado para funcionar."""
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    libro = gspread.authorize(creds).open_by_key(SHEET_ID)
    try:
        sh = libro.worksheet(CRM_HOJA)
        encabezado_actual = sh.row_values(1)
        if len(encabezado_actual) < len(CRM_COLUMNAS):
            sh.update(f"A1", [CRM_COLUMNAS])
        return sh
    except gspread.WorksheetNotFound:
        sh = libro.add_worksheet(title=CRM_HOJA, rows=2000, cols=len(CRM_COLUMNAS))
        sh.append_row(CRM_COLUMNAS)
        return sh

def _crm_fila_a_dict(headers, fila):
    """IMPORTANTE: ignora `headers` (la fila 1 real del Sheet) y usa
    siempre CRM_COLUMNAS -- el Sheet "CRM AIDA" ya existía antes de que
    se agregaran columnas nuevas (ESCALADO_A_JAVIER, etc.) al código, y
    su encabezado físico nunca se actualizó. Si se lee con el encabezado
    real, esas columnas nuevas simplemente no aparecen en el dict nunca
    (esto causó un bug real en producción: la alerta de "vendedor no
    atendió" se repetía cada hora sin parar, porque ESCALADO_A_JAVIER
    jamás se encontraba al leer, sin importar que sí se hubiera guardado
    "Si" en la celda). Se mantiene el parámetro `headers` sin usar para
    no tener que tocar las 4 llamadas existentes."""
    return {CRM_COLUMNAS[i]: (fila[i] if i < len(fila) else "")
            for i in range(len(CRM_COLUMNAS))}

def _resumen_propiedades_para_plantilla(propiedades, max_chars=250):
    """Texto compacto 'Título (EB-XXXX)' por propiedad, para meter en el
    único parámetro de la plantilla 'notificacion_lead' -- el vendedor
    necesita el código EB para buscar al originador, así que SIEMPRE debe
    ir aquí, no solo en el mensaje normal que puede fallar."""
    partes = []
    for p in propiedades:
        titulo = (p.get("titulo") or "Propiedad").strip()
        codigo = p.get("codigo_eb") or "sin código"
        partes.append(f"{titulo} ({codigo})")
    texto = "; ".join(partes)
    if len(texto) > max_chars:
        texto = texto[:max_chars - 3] + "..."
    return texto


def _intentar_asignar_vendedor_automatico(phone, operacion_hint=""):
    """Dispara la asignación real de vendedor + arranque del CRM AIDA
    SOLO cuando se cumplen las 3 condiciones que definió Javier:
      1. Ya se sabe el nombre del cliente
      2. Ya se tiene su teléfono (siempre lo tenemos, es WhatsApp)
      3. Ya se le mandó AL MENOS una ficha (puede ser la primera que preguntó)
    Es seguro llamarla varias veces (desde registrar_lead, desde
    enviar_ficha, desde enviar_ficha_liga) -- solo actúa la primera vez
    que las 3 condiciones ya se cumplen, gracias a la bandera CRM_INICIADO."""
    m = memoria_leer(phone)
    if m.get("CRM_INICIADO") == "Si":
        return  # ya se asignó antes, no se repite
    nombre = m.get("NOMBRE", "")
    ficha_codigo = m.get("PRIMERA_FICHA_CODIGO", "")
    ficha_liga = m.get("PRIMERA_FICHA_LIGA", "")
    if not (nombre and (ficha_codigo or ficha_liga)):
        return  # todavía falta el nombre o la primera ficha

    propiedades = [{
        "codigo_eb": ficha_codigo,
        "titulo": m.get("PRIMERA_FICHA_TITULO", ""),
        "liga": ficha_liga,
    }]
    resultado = crm_crear_registro(phone, nombre, propiedades,
                                   operacion=operacion_hint or m.get("OPERACION", ""))
    if resultado.get("creado"):
        memoria_guardar(phone, CRM_INICIADO="Si")
        # Aviso EXPLÍCITO a Javier de qué vendedor quedó asignado -- esto es
        # aparte de la copia general del chat que ya recibe de cada mensaje.
        if JAVIER_PERSONAL:
            estado_notif = ("Ya se le mandó el chat completo y la ficha al vendedor."
                            if resultado.get("notificacion_enviada")
                            else "⚠️ OJO: el mensaje al vendedor NO se pudo entregar "
                                 "(probablemente no tiene sesión abierta de WhatsApp con "
                                 "Acierta Max) -- avísale tú directamente.")
            notificar_interno(
                JAVIER_PERSONAL,
                f"✅ ASIGNACIÓN AUTOMÁTICA — {resultado.get('folio_crm')}\n\n"
                f"Cliente: {nombre} ({phone})\n"
                f"Vendedor asignado: {resultado.get('vendedor')}\n\n"
                f"{estado_notif}",
                resumen_para_plantilla=(f"Cliente: {nombre} | WA: {phone} | "
                    f"Vendedor: {resultado.get('vendedor')} | "
                    f"Ficha: {_resumen_propiedades_para_plantilla(propiedades)} | "
                    f"Folio: {resultado.get('folio_crm')}"))


def _crm_buscar_activo_por_cliente(phone_cliente):
    """A diferencia de _crm_buscar_por_vendedor_pendiente (busca por
    vendedor), esta busca por TELÉFONO DEL CLIENTE -- para saber si ya
    tiene un expediente abierto antes de crear uno nuevo y duplicarlo."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return None, None
    headers = valores[0]
    for idx in range(len(valores) - 1, 0, -1):
        fila = _crm_fila_a_dict(headers, valores[idx])
        if fila.get("TELEFONO_CLIENTE") == phone_cliente and fila.get("CONCLUIDO") != "Si":
            return idx + 1, fila
    return None, None


def _msg_vendedor_web(folio_crm, nombre, phone, perfil, carpeta_url=""):
    """Aviso al vendedor cuando el cliente llega por el camino web (acierta.pro).
    A diferencia del flujo por WhatsApp, aquí NO hay chat ni ficha: el perfil
    viene de lo que el cliente contestó en el formulario."""
    lineas = [f"🆕 NUEVO CLIENTE ASIGNADO (desde acierta.pro) — {folio_crm}", "",
              f"Cliente: {nombre} ({phone})"]
    if perfil.get("email"):
        lineas.append(f"Correo: {perfil['email']}")
    lineas.append(f"Quiere: {perfil.get('resumen', '')}")
    if perfil.get("zona"):
        lineas.append(f"Zona: {perfil['zona']}")
    if perfil.get("cuando"):
        lineas.append(f"Para cuándo: {perfil['cuando']}")
    lineas += ["", "📋 Por favor:",
               "1. Escríbele por WhatsApp hoy para presentarte (él/ella aceptó que Acierta Max lo contacte).",
               "2. Confirma zona, presupuesto y forma de pago.",
               "3. Registra tu avance aquí mismo."]
    if carpeta_url:
        lineas += ["", f"📁 Expediente del cliente: {carpeta_url}"]
    lineas += ["", "En 3 horas te voy a preguntar cómo vas. Cualquier duda, contesta aquí mismo."]
    return "\n".join(lineas)


def crm_crear_registro(phone_cliente, nombre_cliente, propiedades, operacion="", perfil_web=None, vendedor_preferido=None):
    """Arranca el expediente CRM AIDA de un cliente que ya calificó
    propiedades y quiere avanzar a visita. Reutiliza el MISMO vendedor
    que se le asignó desde el primer contacto (registrar_lead) -- nunca
    rifa uno distinto, para que sea una sola persona la que lleve al
    cliente de principio a fin. Le manda las fichas + claves EB, y
    programa el primer seguimiento a 3 horas. `propiedades` es una lista
    de dicts {codigo_eb, titulo, liga}.

    Si el cliente YA tiene un expediente activo (ej. se creó automático
    con su primera ficha, y ahora confirma visita a más propiedades),
    se ACTUALIZA ese mismo expediente en vez de crear uno duplicado."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return {"creado": False, "motivo": "Sheets no configurado"}
    try:
        sh = _crm_sheet()
        fila_existente, registro_existente = _crm_buscar_activo_por_cliente(phone_cliente)
        if fila_existente:
            col = {h: i + 1 for i, h in enumerate(CRM_COLUMNAS)}
            nuevas_propiedades = "; ".join(
                f"{p.get('codigo_eb','')}|{p.get('liga','')}" for p in propiedades)
            propiedades_actuales = registro_existente.get("PROPIEDADES", "")
            combinado = (propiedades_actuales + "; " + nuevas_propiedades).strip("; ")
            sh.update_cell(fila_existente, col["PROPIEDADES"], combinado)
            sh.update_cell(fila_existente, col["ULTIMA_ACCION"], hora_gdl())
            sh.update_cell(fila_existente, col["CHAT_COMPLETO"],
                           _formatear_chat_para_vendedor(phone_cliente))
            vendedor_phone = registro_existente.get("VENDEDOR_PHONE")
            vendedor_nombre = registro_existente.get("VENDEDOR")
            lista_texto = "\n".join(
                f"• {p.get('titulo','(sin título)')} — {p.get('codigo_eb','')} — {p.get('liga','')}"
                for p in propiedades)
            ok_msg = notificar_interno(
                vendedor_phone,
                f"➕ MÁS PROPIEDADES DE INTERÉS — {registro_existente.get('FOLIO')}\n\n"
                f"El cliente {nombre_cliente} también quiere ver:\n{lista_texto}",
                resumen_para_plantilla=(f"Cliente: {nombre_cliente} | WA: {phone_cliente} | "
                    f"Ficha: {_resumen_propiedades_para_plantilla(propiedades)} | "
                    f"Folio: {registro_existente.get('FOLIO')}"))
            for p in propiedades:
                if p.get("liga"):
                    try:
                        enviar_ficha_liga(vendedor_phone, p["liga"])
                    except Exception:
                        pass
            # Antes esta rama (la que toma iniciar_recorrido_crm cuando el
            # cliente confirma visita DESPUÉS de la asignación automática)
            # nunca avisaba a Javier de forma explícita -- solo la rama de
            # creación nueva lo hacía. Se pareja el comportamiento aquí.
            if JAVIER_PERSONAL:
                estado_notif = ("Ya se le avisó al vendedor." if ok_msg else
                                "⚠️ El aviso normal al vendedor falló, pero se mandó por "
                                "plantilla de respaldo si esa también estaba disponible.")
                notificar_interno(
                    JAVIER_PERSONAL,
                    f"➕ CLIENTE CONFIRMÓ VISITA — {registro_existente.get('FOLIO')}\n\n"
                    f"Cliente: {nombre_cliente} ({phone_cliente})\n"
                    f"Vendedor: {vendedor_nombre}\n"
                    f"Propiedades:\n{lista_texto}\n\n{estado_notif}",
                    resumen_para_plantilla=(f"Cliente: {nombre_cliente} | WA: {phone_cliente} | "
                        f"Vendedor: {vendedor_nombre} | "
                        f"Ficha: {_resumen_propiedades_para_plantilla(propiedades)} | "
                        f"Folio: {registro_existente.get('FOLIO')}"))
            return {"creado": True, "folio_crm": registro_existente.get("FOLIO"),
                    "vendedor": vendedor_nombre, "actualizado": True,
                    "notificacion_enviada": bool(ok_msg)}

        m = memoria_leer(phone_cliente)
        vendedor_phone_previo = m.get("VENDEDOR_ASIGNADO_PHONE")
        vendedor_nombre_previo = m.get("VENDEDOR_ASIGNADO")
        # Si el cliente dijo que ya conoce a un coach específico, se respeta
        # eso por encima de cualquier otra cosa (incluso si ya tenía uno
        # distinto asignado de antes) -- pidió a esa persona, se le da.
        preferido = next((v for v in VENDEDORES if vendedor_preferido and v["nombre"].lower() == vendedor_preferido.strip().lower()), None)
        if preferido:
            vendedor = preferido
            memoria_guardar(phone_cliente, VENDEDOR_ASIGNADO=vendedor["nombre"], VENDEDOR_ASIGNADO_PHONE=vendedor["phone"])
        elif vendedor_phone_previo and vendedor_nombre_previo:
            vendedor = {"nombre": vendedor_nombre_previo, "phone": vendedor_phone_previo}
        else:
            # No debería pasar (registrar_lead ya asigna a todo contacto),
            # pero si por alguna razón no hay uno guardado, se asigna aquí
            # como respaldo en vez de fallar.
            vendedor = _siguiente_vendedor()
            memoria_guardar(phone_cliente, VENDEDOR_ASIGNADO=vendedor["nombre"],
                            VENDEDOR_ASIGNADO_PHONE=vendedor["phone"])
        n = len(sh.get_all_values())
        folio_crm = f"CRM-{n:04d}"
        ahora = time.time()
        proximo = ahora + (3 * 3600)  # primer check-in: 3 horas

        lista_texto = "\n".join(
            f"• {p.get('titulo','(sin título)')} — {p.get('codigo_eb','')} — {p.get('liga','')}"
            for p in propiedades
        )
        codigos = ", ".join(p.get("codigo_eb", "") for p in propiedades if p.get("codigo_eb"))

        # EXPEDIENTE DIGITAL: crea (o reutiliza) la carpeta definitiva del
        # cliente, y si ya había una identificación guardada en la carpeta
        # temporal por teléfono, la reubica aquí.
        carpeta_url = ""
        try:
            carpeta_id, carpeta_url = _drive_carpeta_cliente(folio_crm, nombre_cliente)
            m = memoria_leer(phone_cliente)
            doc_url_previo = m.get("ULTIMO_DOCUMENTO_URL", "")
            if doc_url_previo:
                file_id = _drive_extraer_file_id(doc_url_previo)
                if file_id:
                    _drive_mover_archivo(file_id, carpeta_id)
        except Exception as e:
            print(f"[MAX-EXPEDIENTE] Error preparando carpeta de {nombre_cliente}: {e}", flush=True)

        sh.append_row([
            folio_crm, "Atencion", phone_cliente, nombre_cliente,
            vendedor["nombre"], vendedor["phone"],
            "; ".join(f"{p.get('codigo_eb','')}|{p.get('liga','')}" for p in propiedades),
            "Pendiente", "Pendiente", "", "", operacion, "", "No",
            hora_gdl(), hora_gdl(), str(proximo),
            "No", "", "No",  # VISITA_ACTIVA, ULTIMA_UBICACION_TS, ALERTA_ENVIADA
            carpeta_url, "0", _formatear_chat_para_vendedor(phone_cliente),
            "No", "",  # ESCALADO_A_JAVIER, ULTIMA_RESPUESTA_VENDEDOR
        ])

        msg = (
            f"🆕 NUEVO CLIENTE ASIGNADO — {folio_crm}\n\n"
            f"Cliente: {nombre_cliente} ({phone_cliente})\n"
            f"Le interesan estas propiedades:\n{lista_texto}\n\n"
            f"📋 Por favor:\n"
            f"1. Busca estas claves en tu EasyBroker para identificar al originador: {codigos}\n"
            f"2. Contacta al cliente para agendar visita.\n"
            f"3. Contacta al originador de cada propiedad para confirmar disponibilidad.\n\n"
            + (f"📁 Expediente del cliente (identificación y documentos): {carpeta_url}\n\n" if carpeta_url else "")
            + f"En 3 horas te voy a preguntar cómo vas. Cualquier duda, contesta aquí mismo.\n\n"
            + f"👇 Te mando el chat completo actualizado en el siguiente mensaje."
        )
        if perfil_web:
            msg = _msg_vendedor_web(folio_crm, nombre_cliente, phone_cliente, perfil_web, carpeta_url)
        for p in propiedades:
            if p.get("liga"):
                try:
                    enviar_ficha_liga(vendedor["phone"], p["liga"])
                except Exception:
                    pass
        resumen_pl = (f"Cliente: {nombre_cliente} | WA: {phone_cliente} | "
                      f"Web: {(perfil_web or {}).get('resumen', '')[:140]} | Folio: {folio_crm}"
                      if perfil_web else
                      f"Cliente: {nombre_cliente} | WA: {phone_cliente} | "
                      f"Ficha: {_resumen_propiedades_para_plantilla(propiedades)} | Folio: {folio_crm}")
        ok_msg = notificar_interno(vendedor["phone"], msg, resumen_para_plantilla=resumen_pl)
        chat_completo = _formatear_chat_para_vendedor(phone_cliente)
        # El chat completo NO cabe en la plantilla de una sola variable --
        # si el mensaje normal ya falló, no tiene caso reintentarlo con la
        # plantilla (el contenido no encaja). Se manda tal cual, sabiendo
        # que puede no llegar si el ticket sigue cerrado.
        # Un lead web todavía no tiene chat: no se manda un "chat completo" vacío.
        ok_chat = True if perfil_web else wati_send_text(vendedor["phone"], f"💬 CHAT COMPLETO — {folio_crm}\n\n{chat_completo}")
        if not (ok_msg and ok_chat):
            print(f"[MAX-CRM-ALERTA] Envío a {vendedor['nombre']} ({vendedor['phone']}) "
                  f"falló (ok_msg={ok_msg}, ok_chat={ok_chat}).", flush=True)
            if JAVIER_PERSONAL:
                notificar_interno(
                    JAVIER_PERSONAL,
                    f"⚠️ No se pudo notificar a {vendedor['nombre']} sobre {folio_crm} "
                    f"(cliente {nombre_cliente}, {phone_cliente}). Probablemente {vendedor['nombre']} "
                    f"no tiene una conversación reciente abierta con el número de WhatsApp de "
                    f"Acierta Max -- contáctalo tú directamente para avisarle.",
                    resumen_para_plantilla=(f"⚠️ No se notificó a {vendedor['nombre']} | "
                        f"Cliente: {nombre_cliente} | WA: {phone_cliente} | "
                        f"{('Web: ' + perfil_web.get('resumen', '')[:120]) if perfil_web else 'Ficha: ' + _resumen_propiedades_para_plantilla(propiedades)} | "
                        f"Folio: {folio_crm}"))
        return {"creado": True, "folio_crm": folio_crm, "vendedor": vendedor["nombre"],
                "vendedor_phone": vendedor["phone"],
                "carpeta_cliente": carpeta_url, "notificacion_enviada": bool(ok_msg and ok_chat)}
    except Exception as e:
        return {"creado": False, "motivo": str(e)[:200]}

def crm_registrar_ubicacion(vendedor_phone, lat, lon):
    """Registra un ping de ubicación de un vendedor en visita activa:
    actualiza el reloj de 'última ubicación' (para que no se dispare una
    alerta falsa) y deja el punto en el log de Seguridad Vendedores.
    Si por alguna razón tiene más de un expediente con visita activa a
    la vez, actualiza todos -- su ubicación física real es una sola, así
    que aplica a cualquier expediente que la esté esperando."""
    _, _, activos = _crm_buscar_por_vendedor_pendiente(vendedor_phone)
    activos_en_visita = [(fn, f) for fn, f in activos if f.get("VISITA_ACTIVA") == "Si"]
    if not activos_en_visita:
        return False  # no hay visita activa registrada para este número
    sh = _crm_sheet()
    col = {h: i + 1 for i, h in enumerate(CRM_COLUMNAS)}
    for fila_num, registro in activos_en_visita:
        sh.update_cell(fila_num, col["ULTIMA_UBICACION_TS"], str(time.time()))
        sh.update_cell(fila_num, col["ALERTA_ENVIADA"], "")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        libro = gspread.authorize(creds).open_by_key(SHEET_ID)
        try:
            sh_seg = libro.worksheet(SEGURIDAD_HOJA)
        except Exception:
            sh_seg = libro.add_worksheet(title=SEGURIDAD_HOJA, rows=5000, cols=len(SEGURIDAD_COLUMNAS))
            sh_seg.append_row(SEGURIDAD_COLUMNAS)
        for _, registro in activos_en_visita:
            sh_seg.append_row([registro.get("FOLIO"), registro.get("VENDEDOR"),
                               vendedor_phone, hora_gdl(), lat, lon])
    except Exception as e:
        print(f"[MAX-SEGURIDAD] Error guardando log de ubicación: {e}", flush=True)
    return True


def _crm_revisar_seguridad_visitas():
    """Corre con más frecuencia que el resto del CRM (cada 15 min, no cada
    hora) porque un retraso largo en detectar 'el vendedor dejó de mandar
    ubicación' no es aceptable tratándose de seguridad personal.
    IMPORTANTE: la alerta se REPITE cada ~20 min mientras siga sin
    ubicación -- no basta con avisar una sola vez y quedarse callado si
    el vendedor sigue sin responder, eso sería justo el peor momento
    para que el sistema se quede en silencio."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return
    headers = valores[0]
    col = {h: i + 1 for i, h in enumerate(headers)}
    ahora = time.time()
    for idx in range(1, len(valores)):
        fila = _crm_fila_a_dict(headers, valores[idx])
        if fila.get("VISITA_ACTIVA") != "Si":
            continue
        try:
            ultima = float(fila.get("ULTIMA_UBICACION_TS") or 0)
        except ValueError:
            continue
        try:
            ultima_alerta = float(fila.get("ALERTA_ENVIADA") or 0)
        except ValueError:
            ultima_alerta = 0
        minutos_sin_ubicacion = (ahora - ultima) / 60
        minutos_desde_alerta = (ahora - ultima_alerta) / 60 if ultima_alerta else 999
        ya_hubo_alerta_antes = ultima_alerta > 0
        if minutos_sin_ubicacion >= MINUTOS_TOLERANCIA_ALERTA and minutos_desde_alerta >= 20:
            vendedor = fila.get("VENDEDOR")
            vendedor_phone = fila.get("VENDEDOR_PHONE")
            folio = fila.get("FOLIO")
            notificar_interno(
                vendedor_phone,
                f"📍 No he recibido tu ubicación en más de {MINUTOS_TOLERANCIA_ALERTA} min "
                f"({folio}). ¿Todo bien? Mándamela en cuanto puedas.",
                resumen_para_plantilla=(f"📍 Sin ubicación hace {int(minutos_sin_ubicacion)} min | "
                    f"Folio: {folio} | Confirma que todo está bien"),
                template_name="seguimiento_lead")
            if JAVIER_PERSONAL:
                if ya_hubo_alerta_antes:
                    notificar_interno(
                        JAVIER_PERSONAL,
                        f"🚨 SIGUE SIN RESPONDER: {vendedor} lleva {int(minutos_sin_ubicacion)} min "
                        f"sin mandar ubicación ({folio}) — ya se le insistió antes y sigue sin "
                        f"contestar. Por favor contáctalo directamente o considera medidas adicionales.",
                        resumen_para_plantilla=(f"🚨 SIGUE SIN RESPONDER — {vendedor} sin ubicación "
                            f"{int(minutos_sin_ubicacion)} min | Folio: {folio} | Contáctalo ya"),
                        template_name="seguimiento_lead")
                else:
                    notificar_interno(
                        JAVIER_PERSONAL,
                        f"⚠️ ALERTA DE SEGURIDAD: {vendedor} no ha mandado ubicación en más de "
                        f"{MINUTOS_TOLERANCIA_ALERTA} min durante una visita activa ({folio}). "
                        f"Se le pidió confirmar. Por favor da seguimiento directo.",
                        resumen_para_plantilla=(f"⚠️ ALERTA DE SEGURIDAD — {vendedor} sin ubicación "
                            f"{int(minutos_sin_ubicacion)} min | Folio: {folio}"),
                        template_name="seguimiento_lead")
            sh.update_cell(idx + 1, col["ALERTA_ENVIADA"], str(ahora))


def _crm_buscar_por_vendedor_pendiente(vendedor_phone, texto=""):
    """Busca a qué expediente aplica la respuesta de este vendedor.
    Si tiene un solo expediente activo, es ese. Si tiene varios y
    mencionó el folio (ej. 'CRM-0007'), usa ese. Si tiene varios y NO
    especificó folio, regresa ambiguo (fila_num=None) junto con la
    lista completa de candidatos, para que quien llame le pida que
    aclare -- adivinar aquí podría aplicar la respuesta al cliente
    equivocado."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return None, None, []
    headers = valores[0]
    activos = []
    for idx in range(len(valores) - 1, 0, -1):  # más reciente primero
        fila = _crm_fila_a_dict(headers, valores[idx])
        if fila.get("VENDEDOR_PHONE") == vendedor_phone and fila.get("CONCLUIDO") != "Si":
            activos.append((idx + 1, fila))
    if not activos:
        return None, None, []
    m = re.search(r"CRM-\d{4}", (texto or "").upper())
    if m:
        for fila_num, fila in activos:
            if fila.get("FOLIO") == m.group(0):
                return fila_num, fila, activos
    if len(activos) == 1:
        return activos[0][0], activos[0][1], activos
    return None, None, activos  # ambiguo: 2+ activos, sin folio especificado


# ------------------------------------------------------------------
# CHECKLIST DE VISITA -- pedido por Javier (2026-09-29): al terminar una
# visita, el coach reporta primero al ORIGINADOR (quien tiene la
# propiedad en exclusiva) y luego perfila al CLIENTE. Es un parser
# determinístico (como el resto del CRM): la secuencia de pasos vive
# aquí, en datos, no repartida en ifs sueltos.
#
# Cada paso: (id, campo_crm, pregunta, tipo)
#   tipo "texto"  -> se guarda tal cual (recortado a 300 caracteres)
#   tipo "sinoDudosa" -> normaliza a "Si" / "No" / "Dudosa"
#   tipo "sino"   -> normaliza a "Si" / "No"
#   tipo "interes" -> normaliza a "Autentico" / "Dudoso" / "Perdida de tiempo"
# ------------------------------------------------------------------
CHECKLIST_VISITA_COMUN_1 = [
    ("originador_nombre", "ORIGINADOR_NOMBRE", "1️⃣ Vamos con el checklist de la visita. Empecemos con el *originador* "
        "(quien tiene la propiedad en exclusiva). ¿Cuál es su nombre?", "texto"),
    ("originador_tel", "ORIGINADOR_TEL", "2️⃣ ¿Su teléfono o WhatsApp?", "texto"),
    ("originador_comision", "ORIGINADOR_COMISION", "3️⃣ ¿Qué comisión acordaron compartir con él? (ej. 50/50, o el % que sea)", "texto"),
    ("originador_puntual", "ORIGINADOR_PUNTUAL", "4️⃣ ¿Llegó a tiempo el originador a la visita? (Sí/No)", "sino"),
    ("originador_info_ok", "ORIGINADOR_INFO_OK", "5️⃣ ¿Ofreció bien la información de la propiedad? (Sí/No)", "sino"),
    ("originador_ficha_ok", "ORIGINADOR_FICHA_OK", "6️⃣ ¿La propiedad es tal cual la información de la ficha? (Sí/No)", "sino"),
    ("originador_extranos", "ORIGINADOR_EXTRANOS", "7️⃣ ¿Encontraste algo extraño? Descríbelo, o escribe \"ninguno\".", "texto"),
]
# El paso 8 cambia de pregunta según VENTA/RENTA (se arma en tiempo real).
CHECKLIST_VISITA_OBLIGADO = ("cliente_obligado", "CLIENTE_OBLIGADO_SOLIDARIO",
    "9️⃣ ¿Tiene obligado solidario? (Sí/No/Pendiente)", "sinoPendiente")
CHECKLIST_VISITA_COMUN_2 = [
    ("cliente_interes", "CLIENTE_INTERES",
        "🔟 En tu opinión, ¿el interés del cliente es auténtico, o sientes que te está "
        "haciendo perder el tiempo? (Auténtico/Dudoso/Pérdida de tiempo)", "interes"),
    ("recorrido", "VISITA_RESULTADO", "Cuéntame el recorrido completo de la visita (notas libres).", "texto"),
    ("acuerdos", "VISITA_ACUERDOS", "Por último, ¿qué acuerdos quedaron con el cliente?", "texto"),
]

def _checklist_visita_pasos(operacion):
    """Arma la secuencia completa de pasos según la operación del expediente."""
    op = (operacion or "").strip().lower()
    paso8 = ("cliente_capacidad", "CLIENTE_CAPACIDAD",
        "8️⃣ Ahora del cliente: ¿confirmaste que tiene capacidad de pago mensual? (Sí/No/Dudosa)"
        if "renta" in op else
        "8️⃣ Ahora del cliente: ¿confirmaste que tiene capacidad de compra? (Sí/No/Dudosa)", "sinoDudosa")
    pasos = list(CHECKLIST_VISITA_COMUN_1) + [paso8]
    if "renta" in op:
        pasos.append(CHECKLIST_VISITA_OBLIGADO)
    pasos += CHECKLIST_VISITA_COMUN_2
    return pasos

def _normalizar_checklist(texto, tipo):
    t = (texto or "").strip().lower()
    if tipo == "sino":
        if any(p in t for p in ("si", "sí", "yes", "ok", "claro")): return "Si"
        if "no" in t.split(): return "No"
        return None
    if tipo == "sinoDudosa":
        if "dud" in t: return "Dudosa"
        if any(p in t for p in ("si", "sí", "yes", "ok")): return "Si"
        if t.split() and t.split()[0] == "no": return "No"
        return None
    if tipo == "sinoPendiente":
        if "pendiente" in t or "dud" in t: return "Pendiente"
        if any(p in t for p in ("si", "sí", "yes", "ok")): return "Si"
        if t.split() and t.split()[0] == "no": return "No"
        return None
    if tipo == "interes":
        if "aut" in t: return "Autentico"
        if "perd" in t or "tiempo" in t: return "Perdida de tiempo"
        if "dud" in t: return "Dudoso"
        return None
    return texto.strip()[:300]  # "texto"

def iniciar_checklist_visita(vendedor_phone, folio, operacion):
    pasos = _checklist_visita_pasos(operacion)
    memoria_guardar(vendedor_phone, VISITA_CHECKLIST_FOLIO=folio, VISITA_CHECKLIST_PASO=pasos[0][0])
    wati_send_text(vendedor_phone, pasos[0][2])

def _checklist_visita_procesar(vendedor_phone, texto):
    """Si este vendedor tiene un checklist de visita en curso, procesa la
    respuesta como la siguiente pregunta de la secuencia. Regresa False
    si no había checklist activo (para que el llamador siga de largo)."""
    m = memoria_leer(vendedor_phone)
    paso_actual = m.get("VISITA_CHECKLIST_PASO", "")
    folio = m.get("VISITA_CHECKLIST_FOLIO", "")
    if not paso_actual or not folio:
        return False

    fila_num, registro, _ = _crm_buscar_por_vendedor_pendiente(vendedor_phone, folio)
    if not registro or registro.get("FOLIO") != folio:
        # El expediente ya no está activo (se concluyó por otra vía) -- se limpia el estado
        memoria_guardar(vendedor_phone, VISITA_CHECKLIST_FOLIO="", VISITA_CHECKLIST_PASO="")
        return False

    pasos = _checklist_visita_pasos(registro.get("OPERACION"))
    idx = next((i for i, p in enumerate(pasos) if p[0] == paso_actual), None)
    if idx is None:
        memoria_guardar(vendedor_phone, VISITA_CHECKLIST_FOLIO="", VISITA_CHECKLIST_PASO="")
        return False

    _id, campo, _pregunta, tipo = pasos[idx]
    valor = _normalizar_checklist(texto, tipo)
    if valor is None:
        reintento = ("Contéstame Sí, No o Dudosa 🙂" if tipo == "sinoDudosa"
                     else "Contéstame Sí, No o Pendiente 🙂" if tipo == "sinoPendiente"
                     else "Contéstame Sí o No 🙂" if tipo == "sino"
                     else "Contéstame Auténtico, Dudoso, o \"pérdida de tiempo\" 🙂")
        wati_send_text(vendedor_phone, reintento)
        return True

    memoria_guardar(vendedor_phone, **{f"VISITA_TMP_{campo}": valor})

    if idx + 1 < len(pasos):
        siguiente = pasos[idx + 1]
        memoria_guardar(vendedor_phone, VISITA_CHECKLIST_PASO=siguiente[0])
        wati_send_text(vendedor_phone, siguiente[2])
        return True

    # Último paso: se guardan TODOS los campos temporales en el CRM de una vez.
    sh = _crm_sheet()
    col = {h: i + 1 for i, h in enumerate(CRM_COLUMNAS)}
    m_final = memoria_leer(vendedor_phone)
    for _id2, campo2, _p2, _t2 in pasos:
        val = m_final.get(f"VISITA_TMP_{campo2}")
        if val is not None:
            sh.update_cell(fila_num, col[campo2], val)
    sh.update_cell(fila_num, col["FASE"], "Deseo")
    sh.update_cell(fila_num, col["ULTIMA_ACCION"], hora_gdl())

    limpiar = {f"VISITA_TMP_{c}": "" for _i, c, _p, _t in pasos}
    limpiar.update(VISITA_CHECKLIST_FOLIO="", VISITA_CHECKLIST_PASO="")
    memoria_guardar(vendedor_phone, **limpiar)

    op = (registro.get("OPERACION") or "").lower()
    doc_msg = ("Para avanzar con la renta, pídele al cliente: identificación oficial, "
               "comprobante de ingresos (3-4x la renta), aval con propiedad en Jalisco, "
               "y referencias." if "renta" in op else
               "Para avanzar con la compra, pídele al cliente: identificación oficial, "
               "comprobante de ingresos, y si es crédito, precalificación bancaria o "
               "Infonavit vigente.")
    interes_val = m_final.get("VISITA_TMP_CLIENTE_INTERES", "")
    aviso_interes = ("\n\n⚠️ Marcaste el interés como \"pérdida de tiempo\" -- avísame si prefieres "
                      "que lo concluyamos en vez de seguir invirtiendo tiempo en este cliente."
                      if interes_val == "Perdida de tiempo" else "")
    wati_send_text(vendedor_phone, f"Checklist completo, gracias 🙌. {doc_msg}{aviso_interes}")
    return True


# ------------------------------------------------------------------
# REPORTE DEL DÍA -- pedido por Javier (2026-09-29): en un día sin
# visita, el coach reporta redes sociales, networking, y seguimiento
# a cada cliente abierto que tenga. Se guarda para consulta; NO se le
# reenvía a Javier a diario (él solo pidió el reporte de los LUNES).
# ------------------------------------------------------------------
ACTIVIDAD_HOJA = "Actividad Diaria"
ACTIVIDAD_COLUMNAS = ["FECHA", "VENDEDOR_PHONE", "TIPO", "FOLIO", "DETALLE"]

def _actividad_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    libro = gspread.authorize(creds).open_by_key(SHEET_ID)
    try:
        return libro.worksheet(ACTIVIDAD_HOJA)
    except Exception:
        sh = libro.add_worksheet(title=ACTIVIDAD_HOJA, rows=3000, cols=len(ACTIVIDAD_COLUMNAS))
        sh.append_row(ACTIVIDAD_COLUMNAS)
        return sh

def _clientes_abiertos_de(vendedor_phone):
    """Ficha resumida de cada expediente activo (no concluido) de este
    vendedor: folio, nombre, fase del funnel y teléfono de contacto."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return []
    headers = valores[0]
    out = []
    for idx in range(1, len(valores)):
        fila = _crm_fila_a_dict(headers, valores[idx])
        if fila.get("VENDEDOR_PHONE") == vendedor_phone and fila.get("CONCLUIDO") != "Si":
            out.append({"folio": fila.get("FOLIO"), "nombre": fila.get("NOMBRE_CLIENTE") or "cliente",
                        "fase": fila.get("FASE") or "Atencion", "telefono": fila.get("TELEFONO_CLIENTE") or "sin registrar"})
    return out

# Antes exigía que el mensaje dijera SOLO "Reporte del día"; si traía algo más
# ("Reporte del día. Los atendió Ubaldo") no se reconocía y MAX lo contestaba
# como si fuera un cliente. Ahora basta con que EMPIECE así.
_REPORTE_DIA_TRIGGER = re.compile(r"^\s*(mi\s+)?reporte\s+(del?\s+)?d[ií]a\b", re.IGNORECASE)

def _reporte_dia_manejar(vendedor_phone, texto):
    """Reporte del día: primero un mensaje por cada cliente abierto (con
    su funnel y teléfono a la vista, para que el coach conteste con
    contexto), y al final redes sociales y networking. Regresa False si
    no aplica (ni es el disparador, ni hay uno en curso)."""
    m = memoria_leer(vendedor_phone)
    paso = m.get("REPORTE_DIA_PASO", "")

    if not paso:
        if not _REPORTE_DIA_TRIGGER.match(texto or ""):
            return False
        pendientes = [c["folio"] for c in _clientes_abiertos_de(vendedor_phone)]
        wati_send_text(vendedor_phone, "📋 Reporte del día")
        return _reporte_dia_siguiente_cliente(vendedor_phone, pendientes)

    def _guardar(tipo, folio, detalle):
        try:
            _actividad_sheet().append_row([hora_gdl(), vendedor_phone, tipo, folio, (detalle or "").strip()[:500]])
        except Exception as e:
            print(f"[MAX-REPORTE-DIA] Error guardando actividad: {e}", flush=True)

    if paso == "cliente":
        folio_actual = m.get("REPORTE_DIA_ACTUAL", "")
        _guardar("cliente", folio_actual, texto)
        pendientes = [f for f in m.get("REPORTE_DIA_PENDIENTES", "").split(",") if f]
        return _reporte_dia_siguiente_cliente(vendedor_phone, pendientes)

    if paso == "redes":
        _guardar("redes", "", texto)
        memoria_guardar(vendedor_phone, REPORTE_DIA_PASO="networking")
        wati_send_text(vendedor_phone,
            "Y de publicidad, ¿hiciste networking o investigaste alguna oportunidad? Cuéntame (o escribe \"no\").")
        return True

    if paso == "networking":
        _guardar("networking", "", texto)
        memoria_guardar(vendedor_phone, REPORTE_DIA_PASO="", REPORTE_DIA_PENDIENTES="", REPORTE_DIA_ACTUAL="")
        wati_send_text(vendedor_phone, "Reporte del día guardado, gracias 🙌")
        return True

    return False

def _reporte_dia_siguiente_cliente(vendedor_phone, pendientes):
    if not pendientes:
        # Ya no hay clientes pendientes -- ahora sí, al final, redes y networking.
        memoria_guardar(vendedor_phone, REPORTE_DIA_PASO="redes", REPORTE_DIA_PENDIENTES="", REPORTE_DIA_ACTUAL="")
        wati_send_text(vendedor_phone, "Ya viste a todos tus clientes abiertos. ¿Publicaste algo en redes sociales hoy? Cuéntame qué (o escribe \"no\").")
        return True
    folio = pendientes[0]
    ficha = next((c for c in _clientes_abiertos_de(vendedor_phone) if c["folio"] == folio), None)
    if not ficha:  # el expediente ya no existe/se concluyó entre tanto -- se salta
        return _reporte_dia_siguiente_cliente(vendedor_phone, pendientes[1:])
    memoria_guardar(vendedor_phone, REPORTE_DIA_PASO="cliente", REPORTE_DIA_ACTUAL=folio,
                     REPORTE_DIA_PENDIENTES=",".join(pendientes[1:]))
    wati_send_text(vendedor_phone,
        f"👤 {ficha['nombre']} ({folio})\nFase: {ficha['fase']}\nTeléfono: {ficha['telefono']}\n\n¿Qué hiciste hoy con él/ella?")
    return True

# ------------------------------------------------------------------
# REPORTE DE LOS LUNES -- pedido por Javier: cada lunes, cada vendedor
# con clientes abiertos recibe la lista y debe mandar su plan para no
# perderlos. La respuesta se reenvía a Javier tal cual (no se
# reinterpreta): es su reporte, él lo lee directo.
# ------------------------------------------------------------------
def _enviar_reportes_lunes():
    for v in VENDEDORES:
        phone = v["phone"]
        abiertos = _clientes_abiertos_de(phone)
        if not abiertos:
            continue
        lista = "\n".join(f"• {c['folio']} — {c['nombre']} ({c['fase']})" for c in abiertos)
        wati_send_text(phone,
            f"📅 Buen lunes {v.get('nombre', '')}. Aquí tus clientes abiertos:\n{lista}\n\n"
            f"Mándame tu plan para cada uno, para no perderlos (un solo mensaje está bien).")
        memoria_guardar(phone, REPORTE_LUNES_PENDIENTE="Si")

def _reporte_lunes_manejar(vendedor_phone, texto):
    """Si a este vendedor se le pidió su plan de los lunes, esta respuesta
    ES ese plan: se reenvía a Javier tal cual, sin reinterpretarla."""
    if memoria_leer(vendedor_phone).get("REPORTE_LUNES_PENDIENTE") != "Si":
        return False
    memoria_guardar(vendedor_phone, REPORTE_LUNES_PENDIENTE="")
    nombre_v = next((v.get("nombre", vendedor_phone) for v in VENDEDORES if v["phone"] == vendedor_phone), vendedor_phone)
    if JAVIER_PERSONAL:
        notificar_interno(JAVIER_PERSONAL,
            f"📅 PLAN DEL LUNES -- {nombre_v}:\n\n{texto.strip()}",
            resumen_para_plantilla=f"Plan lunes de {nombre_v}: {texto.strip()[:150]}")
    wati_send_text(vendedor_phone, "Recibido, gracias 🙌")
    return True

def crm_procesar_respuesta_vendedor(vendedor_phone, texto):
    """Parser determinístico (NO usa el modelo) de la respuesta de un
    vendedor a un check-in del CRM. Se mantiene simple y predecible a
    propósito -- esto mueve el pipeline de ventas real, no conviene
    dejarlo a interpretación libre de un LLM."""
    # El checklist de visita tiene prioridad: si está en curso, esta
    # respuesta es la siguiente pregunta de la secuencia, no una
    # respuesta normal de CRM.
    if _checklist_visita_procesar(vendedor_phone, texto):
        return True
    fila_num, registro, activos = _crm_buscar_por_vendedor_pendiente(vendedor_phone, texto)
    if not activos:
        return False  # no hay expediente activo de este vendedor; no es una respuesta de CRM
    if not registro:
        # Ambiguo: 2+ clientes activos y no dijo a cuál se refiere --
        # mejor preguntar que aplicarlo al cliente equivocado.
        lista = "\n".join(f"• {f.get('FOLIO')} — {f.get('NOMBRE_CLIENTE')}" for _, f in activos)
        wati_send_text(vendedor_phone,
            f"Tienes más de un cliente activo, ¿a cuál te refieres? Contéstame incluyendo el folio:\n{lista}")
        return True

    t = texto.strip().lower()
    sh = _crm_sheet()
    headers = CRM_COLUMNAS
    col = {h: i + 1 for i, h in enumerate(headers)}  # 1-indexed para gspread

    def actualizar(campo, valor):
        sh.update_cell(fila_num, col[campo], valor)

    # El vendedor SÍ respondió (llegamos hasta aquí, no fue ambiguo) --
    # se resetea el contador de intentos fallidos y la bandera de
    # escalación, y se guarda el texto real de su respuesta (para poder
    # construir después un cuestionario de seguimiento más completo).
    actualizar("INTENTOS_SEGUIMIENTO", "0")
    actualizar("ESCALADO_A_JAVIER", "No")
    actualizar("ULTIMA_RESPUESTA_VENDEDOR", texto.strip()[:300])

    fase = registro.get("FASE")

    if fase == "Atencion":
        cliente_ok = registro.get("CONTACTO_CLIENTE") == "Si"
        originador_ok = registro.get("CONTACTO_ORIGINADOR") == "Si"

        if "concluir" in t or "cancelar" in t or "no va a proceder" in t:
            actualizar("CONCLUIDO", "Si")
            actualizar("ULTIMA_ACCION", hora_gdl())
            wati_send_text(vendedor_phone, "Entendido, marco este caso como concluido. Gracias por avisar 👍")
            return True

        # Si ya se le preguntó fecha de cita y esto parece una fecha/hora,
        # tiene prioridad sobre el parseo de sí/no.
        if cliente_ok and originador_ok and not registro.get("FECHA_HORA_CITA"):
            actualizar("FECHA_HORA_CITA", texto.strip())
            actualizar("ULTIMA_ACCION", hora_gdl())
            proximo = time.time() + (2 * 3600)  # +2h después de la cita reportada, de forma simple
            actualizar("PROXIMO_SEGUIMIENTO_TS", str(proximo))
            # SEGURIDAD: activa el monitoreo de ubicación desde ahora. Es
            # intencional que arranque de inmediato (no exactamente a la
            # hora de la cita) -- más monitoreo nunca es un riesgo, menos sí.
            actualizar("VISITA_ACTIVA", "Si")
            actualizar("ULTIMA_UBICACION_TS", str(time.time()))
            actualizar("ALERTA_ENVIADA", "")
            wati_send_text(vendedor_phone,
                f"Perfecto, quedó agendada la cita. Te voy a preguntar cómo salió un rato después. Éxito 🙌\n\n"
                f"📍 Por tu seguridad: mándame tu ubicación (como mensaje de ubicación normal de "
                f"WhatsApp) cada {MINUTOS_ENTRE_UBICACION} minutos mientras estés en la visita. "
                f"Cuando termines, escríbeme 'terminé la visita'.")
            return True

        if "termine la visita" in t or "terminé la visita" in t or "termine visita" in t:
            actualizar("VISITA_ACTIVA", "No")
            actualizar("ULTIMA_ACCION", hora_gdl())
            wati_send_text(vendedor_phone, "Perfecto, ya no te voy a pedir ubicación. Vamos con el checklist 🙌")
            iniciar_checklist_visita(vendedor_phone, registro.get("FOLIO"), registro.get("OPERACION"))
            return True

        cambios = []
        if not cliente_ok and any(p in t for p in ["cliente si", "cliente sí", "ya contacte al cliente", "al cliente si", "al cliente sí"]):
            actualizar("CONTACTO_CLIENTE", "Si")
            cambios.append("cliente: contactado")
        elif not cliente_ok and any(p in t for p in ["cliente no", "al cliente no"]):
            actualizar("CONTACTO_CLIENTE", "No")

        if not originador_ok and any(p in t for p in ["originador si", "originador sí", "ya conteste al originador", "al originador si", "al originador sí"]):
            actualizar("CONTACTO_ORIGINADOR", "Si")
            cambios.append("originador: contactado")
        elif not originador_ok and any(p in t for p in ["originador no", "al originador no"]):
            actualizar("CONTACTO_ORIGINADOR", "No")

        # Respuesta simple "si"/"no" sin especificar a cuál -- se aplica a
        # lo que siga pendiente, preguntando explícito si hay ambigüedad.
        if not cambios and t in ("si", "sí", "ya", "listo"):
            if not cliente_ok:
                actualizar("CONTACTO_CLIENTE", "Si")
                cambios.append("cliente: contactado")
            elif not originador_ok:
                actualizar("CONTACTO_ORIGINADOR", "Si")
                cambios.append("originador: contactado")

        actualizar("ULTIMA_ACCION", hora_gdl())
        if cambios:
            wati_send_text(vendedor_phone, f"Anotado: {', '.join(cambios)}. Gracias 👍")
        else:
            wati_send_text(vendedor_phone,
                "Para anotarlo bien, contéstame así: 'cliente sí' / 'cliente no' / "
                "'originador sí' / 'originador no' (uno o los dos).")
        return True

    if fase == "Interes":
        actualizar("VISITA_RESULTADO", texto.strip()[:300])
        actualizar("FASE", "Deseo")
        actualizar("ULTIMA_ACCION", hora_gdl())
        op = (registro.get("OPERACION") or "").lower()
        if "renta" in op:
            doc_msg = ("Para avanzar con la renta, pídele al cliente: identificación oficial, "
                       "comprobante de ingresos (3-4x la renta), aval con propiedad en Jalisco, "
                       "y referencias.")
        else:
            doc_msg = ("Para avanzar con la compra, pídele al cliente: identificación oficial, "
                       "comprobante de ingresos, y si es crédito, precalificación bancaria o "
                       "Infonavit vigente.")
        wati_send_text(vendedor_phone, f"Gracias por el reporte. {doc_msg}")
        return True

    return False

def _crm_pendientes_de_seguimiento():
    """Regresa los registros CRM activos cuyo PROXIMO_SEGUIMIENTO_TS ya
    se cumplió -- para que el hilo proactivo les mande el check-in."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return []
    headers = valores[0]
    ahora = time.time()
    pendientes = []
    for idx in range(1, len(valores)):
        fila = _crm_fila_a_dict(headers, valores[idx])
        if fila.get("CONCLUIDO") == "Si":
            continue
        try:
            proximo = float(fila.get("PROXIMO_SEGUIMIENTO_TS") or 0)
        except ValueError:
            continue
        if proximo and ahora >= proximo:
            pendientes.append((idx + 1, fila))
    return pendientes

def _codigos_eb_de_propiedades(propiedades_texto):
    """Extrae los códigos EB del campo PROPIEDADES del CRM (formato
    'codigo|liga; codigo|liga'), para incluirlos en los antecedentes de
    las plantillas de respaldo."""
    codigos = []
    for parte in (propiedades_texto or "").split(";"):
        codigo = parte.split("|")[0].strip()
        if codigo:
            codigos.append(codigo)
    return ", ".join(codigos) if codigos else "sin código"


def _crm_revisar_seguimientos():
    """Manda el check-in correspondiente a cada registro vencido, y
    reprograma el siguiente en 2 horas (según la cadencia que pidió
    Javier: primer check a 3h, luego cada 2h hasta resolverse). Si el
    vendedor no responde tras varios intentos, escala directo a Javier
    en vez de seguir preguntando para siempre sin que nadie se entere."""
    sh = _crm_sheet()
    for fila_num, registro in _crm_pendientes_de_seguimiento():
        vendedor_phone = registro.get("VENDEDOR_PHONE")
        fase = registro.get("FASE")
        col_proximo = CRM_COLUMNAS.index("PROXIMO_SEGUIMIENTO_TS") + 1
        col_intentos = CRM_COLUMNAS.index("INTENTOS_SEGUIMIENTO") + 1
        try:
            intentos = int(registro.get("INTENTOS_SEGUIMIENTO") or 0)
        except ValueError:
            intentos = 0
        codigos_eb = _codigos_eb_de_propiedades(registro.get("PROPIEDADES"))

        if intentos >= INTENTOS_ANTES_DE_ESCALAR:
            col_escalado = CRM_COLUMNAS.index("ESCALADO_A_JAVIER") + 1
            if registro.get("ESCALADO_A_JAVIER") != "Si":  # solo se avisa UNA vez, no cada hora
                if JAVIER_PERSONAL:
                    notificar_interno(
                        JAVIER_PERSONAL,
                        f"⚠️ El vendedor {registro.get('VENDEDOR')} no atendió al cliente "
                        f"{registro.get('NOMBRE_CLIENTE')} ({registro.get('FOLIO')}). Revísalo "
                        f"en el CRM o habla con él directamente.",
                        resumen_para_plantilla=(f"Vendedor {registro.get('VENDEDOR')} no atendió | "
                            f"Cliente: {registro.get('NOMBRE_CLIENTE')} | "
                            f"Ficha: {codigos_eb} | Folio: {registro.get('FOLIO')}"),
                        template_name="seguimiento_lead")
                sh.update_cell(fila_num, col_escalado, "Si")
            # No se reprograma más -- queda esperando que Javier intervenga
            # o que el vendedor responda espontáneamente (lo que sí se
            # sigue procesando normal si escribe).
            continue

        if fase == "Atencion":
            cliente_ok = registro.get("CONTACTO_CLIENTE") == "Si"
            originador_ok = registro.get("CONTACTO_ORIGINADOR") == "Si"
            if cliente_ok and originador_ok and registro.get("FECHA_HORA_CITA"):
                # ya se agendó -- este check-in es el de "¿cómo salió la visita?"
                col_fase = CRM_COLUMNAS.index("FASE") + 1
                sh.update_cell(fila_num, col_fase, "Interes")
                notificar_interno(
                    vendedor_phone,
                    f"Hola! ¿Cómo salió la visita con {registro.get('NOMBRE_CLIENTE','el cliente')}? "
                    f"Cuéntame brevemente para dar seguimiento.",
                    resumen_para_plantilla=(f"¿Cómo salió la visita? | Cliente: "
                        f"{registro.get('NOMBRE_CLIENTE')} | Ficha: {codigos_eb} | "
                        f"Cita: {registro.get('FECHA_HORA_CITA')} | Folio: {registro.get('FOLIO')}"),
                    template_name="seguimiento_lead")
                sh.update_cell(fila_num, col_proximo, "")  # se reprograma solo si vuelve a fallar
                sh.update_cell(fila_num, col_intentos, str(intentos + 1))
                continue
            faltante = []
            if not cliente_ok: faltante.append("al cliente")
            if not originador_ok: faltante.append("al originador")
            notificar_interno(
                vendedor_phone,
                f"Hola! Seguimiento de {registro.get('FOLIO')} — {registro.get('NOMBRE_CLIENTE')}. "
                f"¿Ya contactaste {' y '.join(faltante)}?",
                resumen_para_plantilla=(f"Cliente: {registro.get('NOMBRE_CLIENTE')} | "
                    f"Ficha: {codigos_eb} | Pendiente: contactar {' y '.join(faltante)} | "
                    f"Intento #{intentos + 1} | Folio: {registro.get('FOLIO')}"),
                template_name="seguimiento_lead")
            sh.update_cell(fila_num, col_proximo, str(time.time() + 2 * 3600))
            sh.update_cell(fila_num, col_intentos, str(intentos + 1))

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
# Semilla inicial (por si el Sheet aún no tiene la pestaña, o Sheets no
# responde) -- estos ya estaban confirmados antes de mover esto a Sheets.
_MAPEO_POST_A_CAMPANA_SEMILLA = {
    "https://www.instagram.com/p/Da33OUcA8nj/": "solares_zona_real",  # EB-WJ9214, confirmado 19/07/2026
    "https://www.instagram.com/p/Da365rRg2VF/": "paneles_solares",     # EB-UO2612, confirmado 20/07/2026
    "https://www.instagram.com/p/DcAYjA8gdH2/": "bellavittoria",       # confirmado 05/09/2026 (caso Iván)
    # Da32rZ1AQIE (Coto Encino, EB-UU6717) quitado el 05/09/2026: la casa ya se rentó.
}
_MAPEO_POST_CACHE = {"datos": None, "hora": 0}
_MAPEO_POST_CACHE_SEGUNDOS = 600  # 10 minutos -- Javier puede editar el Sheet
                                   # y el cambio se refleja solo, sin redeploy

def obtener_mapeo_post_a_campana():
    """Lee la pestaña 'Mapeo Posts' del Sheet (LIGA_INSTAGRAM | CAMPANA | NOTAS).
    Javier agrega ahí cada post nuevo -- sin tocar código, sin redeploy.
    Se cachea 10 min para no golpear la API de Sheets en cada mensaje de
    WhatsApp. Si Sheets falla o la pestaña no existe aún, usa la semilla."""
    ahora = time.time()
    if _MAPEO_POST_CACHE["datos"] is not None and (ahora - _MAPEO_POST_CACHE["hora"]) < _MAPEO_POST_CACHE_SEGUNDOS:
        return _MAPEO_POST_CACHE["datos"]
    mapeo = dict(_MAPEO_POST_A_CAMPANA_SEMILLA)
    try:
        if GOOGLE_CREDS_JSON and SHEET_ID:
            import gspread
            from google.oauth2.service_account import Credentials
            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_CREDS_JSON),
                scopes=["https://www.googleapis.com/auth/spreadsheets"])
            libro = gspread.authorize(creds).open_by_key(SHEET_ID)
            try:
                sh = libro.worksheet("Mapeo Posts")
            except Exception:
                sh = libro.add_worksheet(title="Mapeo Posts", rows=200, cols=3)
                sh.append_row(["LIGA_INSTAGRAM", "CAMPANA", "NOTAS"])
                # Se siembra el Sheet con lo que ya sabíamos, para que Javier
                # vea el formato correcto y edite/agregue desde ahí en adelante.
                for liga, campana in _MAPEO_POST_A_CAMPANA_SEMILLA.items():
                    sh.append_row([liga, campana, ""])
            filas = sh.get_all_values()[1:]  # sin encabezado
            for fila in filas:
                if len(fila) >= 2 and fila[0].strip() and fila[1].strip():
                    mapeo[fila[0].strip()] = fila[1].strip()
            print(f"[MAX] Mapeo Posts cargado desde Sheet: {len(mapeo)} posts", flush=True)
    except Exception as e:
        print(f"[MAX-ERROR] No se pudo leer 'Mapeo Posts' de Sheets, usando semilla: {e}", flush=True)
    _MAPEO_POST_CACHE["datos"] = mapeo
    _MAPEO_POST_CACHE["hora"] = ahora
    return mapeo

# phone -> nombre de campaña activa detectada en esta conversación (por texto
# o por origen de Instagram). Se usa para darle contexto a Claude en turnos
# posteriores cuando el cliente pregunta algo vago ("la ubicación", "cuánto
# cuesta") sin repetir el código EB o la palabra clave.
CAMPANA_ACTIVA_POR_TELEFONO = {}

def get_history(phone):
    with CONV_LOCK:
        return list(CONVERSATIONS.get(phone, []))

def _formatear_chat_para_vendedor(phone, max_caracteres=3000):
    """Convierte el historial de la conversación en un texto legible tipo
    chat, para que el vendedor vea exactamente qué se habló -- no solo
    los datos ya extraídos (nombre, presupuesto, etc.)."""
    historial = get_history(phone)
    if not historial:
        return "(sin historial de conversación disponible)"
    lineas = []
    for turno in historial:
        etiqueta = "Cliente" if turno.get("role") == "user" else "MAX"
        contenido = turno.get("content", "")
        if not isinstance(contenido, str):
            continue  # se ignoran bloques no textuales (llamadas a herramientas, etc.)
        lineas.append(f"{etiqueta}: {contenido}")
    texto = "\n".join(lineas)
    if len(texto) > max_caracteres:
        # Se prioriza lo MÁS RECIENTE (más relevante para el vendedor que
        # el saludo inicial), avisando que se recortó el inicio.
        texto = "[...inicio de la conversación recortado...]\n" + texto[-max_caracteres:]
    return texto

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

# ------------------------------------------------------------------
# EXPEDIENTE DIGITAL DEL CLIENTE — INE y documentos organizados en
# Google Drive, una carpeta por cliente, enlazada desde el CRM AIDA.
# ------------------------------------------------------------------
DRIVE_CARPETA_RAIZ_NOMBRE = "Acierta Max — Expedientes de Clientes"
DRIVE_COMPARTIR_CON = os.environ.get("DRIVE_COMPARTIR_CON_EMAIL", "")  # tu correo de Gmail/Google Workspace
_drive_carpeta_raiz_id_cache = {"id": None}

def _drive_client():
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)

# ------------------------------------------------------------------
# IKONO ALTA DIRECCIÓN — agenda real de llamadas (Google Calendar) +
# investigación web del prospecto, para el lanzamiento de octubre.
# Usa el MISMO service account que Sheets/Drive -- Javier debe:
#   1. Habilitar "Google Calendar API" en el mismo proyecto de Google Cloud.
#   2. Compartir SU calendario con el correo del service account, con
#      permiso "Realizar cambios en los eventos".
# ------------------------------------------------------------------
IKONO_CALENDAR_ID = os.environ.get("IKONO_CALENDAR_ID", "")  # el email del calendario de Javier
IKONO_HORA_INICIO = 8   # 8am
IKONO_HORA_FIN = 10     # 10am
IKONO_DURACION_MIN = 20

def _calendar_client():
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds)


def _proximo_slot_ikono_y_agenda(nombre_cliente, telefono_cliente, resumen_situacion):
    """Busca el próximo horario libre de 20 min entre semana (L-V) de
    8-10am en el calendario de Javier, y AGENDA el evento ahí mismo.
    Regresa (fecha_hora_texto, exito, link_evento_o_error)."""
    if not (GOOGLE_CREDS_JSON and IKONO_CALENDAR_ID):
        return None, False, "IKONO_CALENDAR_ID no configurado"
    try:
        servicio = _calendar_client()
        import datetime as _dt
        zona = "America/Mexico_City"
        ahora = _dt.datetime.now()
        # Máximo 7 días NATURALES de anticipación (no hábiles) -- si caen
        # fin de semana en medio, se saltan igual, pero el límite total
        # de la ventana de búsqueda es de 7 días corridos desde hoy.
        for dias in range(0, 8):
            candidato = ahora + _dt.timedelta(days=dias)
            if candidato.weekday() >= 5:  # sábado=5, domingo=6
                continue
            dia_inicio = candidato.replace(hour=IKONO_HORA_INICIO, minute=0, second=0, microsecond=0)
            dia_fin = candidato.replace(hour=IKONO_HORA_FIN, minute=0, second=0, microsecond=0)
            if dia_fin < ahora:
                continue
            # Trae los eventos ya ocupados ese día en la ventana 8-10am
            eventos = servicio.events().list(
                calendarId=IKONO_CALENDAR_ID,
                timeMin=dia_inicio.isoformat() + "Z", timeMax=dia_fin.isoformat() + "Z",
                singleEvents=True, orderBy="startTime").execute().get("items", [])
            ocupados = []
            for ev in eventos:
                ini = ev["start"].get("dateTime")
                fin = ev["end"].get("dateTime")
                if ini and fin:
                    ocupados.append((_dt.datetime.fromisoformat(ini.replace("Z", "+00:00")),
                                     _dt.datetime.fromisoformat(fin.replace("Z", "+00:00"))))
            # Recorre la ventana en bloques de 20 min buscando uno libre
            cursor = max(dia_inicio, ahora + _dt.timedelta(minutes=30))  # colchón mínimo de 30 min
            while cursor + _dt.timedelta(minutes=IKONO_DURACION_MIN) <= dia_fin:
                fin_bloque = cursor + _dt.timedelta(minutes=IKONO_DURACION_MIN)
                choca = any(cursor < f and fin_bloque > i for i, f in ocupados)
                if not choca:
                    evento = servicio.events().insert(calendarId=IKONO_CALENDAR_ID, body={
                        "summary": f"IKONO — Llamada con {nombre_cliente}",
                        "description": f"Tel: {telefono_cliente}\nSituación: {resumen_situacion[:300]}",
                        "start": {"dateTime": cursor.isoformat(), "timeZone": zona},
                        "end": {"dateTime": fin_bloque.isoformat(), "timeZone": zona},
                    }).execute()
                    fecha_texto = cursor.strftime("%A %d de %B, %H:%M hrs")
                    return fecha_texto, True, evento.get("htmlLink", "")
                cursor += _dt.timedelta(minutes=IKONO_DURACION_MIN)
        return None, False, "No se encontró horario libre en los próximos 7 días"
    except Exception as e:
        return None, False, str(e)[:300]


def investigar_prospecto_ikono(nombre, industria, telefono):
    """Llamada APARTE a Claude (no la conversación principal), con la
    herramienta de búsqueda web habilitada, para investigar al prospecto
    de IKONO antes de la llamada de Javier. Se mantiene separada del
    flujo conversacional principal para no complicar el dispatcher de
    herramientas propias con las herramientas de servidor de Anthropic."""
    lada = telefono.strip()[-10:-8] if len(telefono.strip()) >= 10 else ""
    prompt = (
        f"Investiga en internet a esta persona para preparar una llamada de negocios:\n"
        f"Nombre: {nombre}\nIndustria/tipo de negocio: {industria}\n"
        f"Teléfono (para inferir ciudad por LADA si es útil): {telefono}\n\n"
        f"Busca su perfil de LinkedIn si existe, su rol/empresa actual, y cualquier dato "
        f"público relevante que correlacione con la industria que mencionó. "
        f"Responde en español, breve (máximo 5-6 líneas), factual -- si no encuentras nada "
        f"confiable, dilo directamente en vez de inventar. Si encuentras LinkedIn, incluye el link."
    )
    try:
        r = requests.post(ANTHROPIC_API, timeout=45, headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": CLAUDE_MODEL, "max_tokens": 600,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            "messages": [{"role": "user", "content": prompt}],
        })
        if r.status_code != 200:
            print(f"[MAX-IKONO] Error en investigación web: {r.status_code} {r.text[:300]}", flush=True)
            return "(no se pudo completar la investigación automática)"
        data = r.json()
        texto = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return texto.strip() or "(sin resultados relevantes)"
    except Exception as e:
        print(f"[MAX-IKONO] Excepción en investigación web: {e}", flush=True)
        return "(no se pudo completar la investigación automática)"

def _drive_obtener_o_crear_carpeta(nombre, carpeta_padre_id=None):
    servicio = _drive_client()
    query = (f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
             f"and trashed = false")
    if carpeta_padre_id:
        query += f" and '{carpeta_padre_id}' in parents"
    resultado = servicio.files().list(q=query, fields="files(id, name)").execute()
    archivos = resultado.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {"name": nombre, "mimeType": "application/vnd.google-apps.folder"}
    if carpeta_padre_id:
        metadata["parents"] = [carpeta_padre_id]
    carpeta = servicio.files().create(body=metadata, fields="id").execute()
    nueva_id = carpeta["id"]
    # CRÍTICO: una carpeta creada por el service account vive en SU propio
    # Drive -- sin compartirla, nadie más (ni Javier) puede abrirla aunque
    # tenga el link. Se comparte solo la carpeta RAÍZ (las subcarpetas de
    # cliente heredan el permiso automáticamente al estar adentro).
    if DRIVE_COMPARTIR_CON and not carpeta_padre_id:
        try:
            servicio.permissions().create(
                fileId=nueva_id,
                body={"type": "user", "role": "writer", "emailAddress": DRIVE_COMPARTIR_CON},
                sendNotificationEmail=False).execute()
        except Exception as e:
            print(f"[MAX-EXPEDIENTE] No se pudo compartir la carpeta raíz con {DRIVE_COMPARTIR_CON}: {e}", flush=True)
    return nueva_id

def _drive_carpeta_cliente(folio, nombre_cliente):
    """Regresa (carpeta_id, url) de la carpeta de este cliente, creándola
    si no existe: Acierta Max — Expedientes de Clientes / FOLIO_Nombre/"""
    if not _drive_carpeta_raiz_id_cache["id"]:
        _drive_carpeta_raiz_id_cache["id"] = _drive_obtener_o_crear_carpeta(DRIVE_CARPETA_RAIZ_NOMBRE)
    nombre_carpeta = f"{folio}_{nombre_cliente}"[:100].replace("/", "-")
    carpeta_id = _drive_obtener_o_crear_carpeta(nombre_carpeta, _drive_carpeta_raiz_id_cache["id"])
    url = f"https://drive.google.com/drive/folders/{carpeta_id}"
    return carpeta_id, url

def _wati_descargar_media(filename):
    """Descarga un archivo de media que el cliente mandó por WhatsApp,
    usando el endpoint getMedia de Wati. `filename` viene en el payload
    del webhook cuando el mensaje es imagen/documento/audio."""
    url = f"{WATI_BASE_URL}/api/v1/getMedia"
    r = requests.get(url, headers=wati_headers(), params={"fileName": filename}, timeout=30)
    if r.status_code != 200:
        raise Exception(f"getMedia respondió {r.status_code}: {r.text[:200]}")
    return r.content

def _drive_mover_archivo(file_id, carpeta_destino_id):
    """Mueve un archivo a otra carpeta (quita todos sus padres actuales,
    pone solo el nuevo) -- para reubicar la identificación de la carpeta
    temporal por teléfono a la carpeta definitiva FOLIO_Nombre."""
    servicio = _drive_client()
    archivo = servicio.files().get(fileId=file_id, fields="parents").execute()
    padres_actuales = ",".join(archivo.get("parents", []))
    servicio.files().update(fileId=file_id, addParents=carpeta_destino_id,
                            removeParents=padres_actuales, fields="id, parents").execute()

def _drive_extraer_file_id(url):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None

def guardar_documento_cliente(phone, filename, nombre_archivo_destino, folio=None, nombre_cliente=None, mimetype="image/jpeg"):
    """Descarga un documento que mandó el cliente (ej. su identificación)
    y lo sube a su carpeta en Drive. Si no hay folio/nombre todavía (el
    CRM AIDA aún no existe -- la ID normalmente llega ANTES de agendar),
    se guarda temporalmente y se resuelve la carpeta real cuando se
    crea el expediente CRM (ver crm_crear_registro)."""
    from googleapiclient.http import MediaInMemoryUpload
    contenido = _wati_descargar_media(filename)
    if not folio or not nombre_cliente:
        # Aún no existe folio -- se guarda en una carpeta temporal por
        # teléfono, y se mueve a la carpeta definitiva del cliente cuando
        # se llame iniciar_recorrido_crm.
        carpeta_id, url = _drive_carpeta_cliente("TEMP", phone)
    else:
        carpeta_id, url = _drive_carpeta_cliente(folio, nombre_cliente)
    servicio = _drive_client()
    media = MediaInMemoryUpload(contenido, mimetype=mimetype, resumable=False)
    archivo = servicio.files().create(
        body={"name": nombre_archivo_destino, "parents": [carpeta_id]},
        media_body=media, fields="id, webViewLink").execute()
    return {"file_id": archivo["id"], "carpeta_url": url,
            "file_url": archivo.get("webViewLink", url)}

def _normalizar_phone_wati(phone):
    """Wati necesita el numero con codigo de pais completo para mensajes salientes.
    Si el numero tiene 10 digitos (formato local), agrega 521 al inicio."""
    p = str(phone).strip().replace(" ","").replace("-","").replace("+","")
    if len(p) == 10:
        return "521" + p
    return p

def _ultimos10(phone):
    """Los 10 dígitos del número de celular, sin importar cómo venga el
    prefijo de país -- México manda a veces 521XXXXXXXXXX (13) y a veces
    52XXXXXXXXXX (12, sin el '1' extra); comparar solo esto evita que un
    vendedor deje de ser reconocido por esa inconsistencia."""
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-10:] if len(d) >= 10 else d

JAVIER_PERSONAL = os.environ.get("JAVIER_PERSONAL_NUMBER", "5213325773277")
BETTY_PHONE = os.environ.get("BETTY_PHONE_NUMBER", "3311964181")  # responsable de crédito

def _es_numero_interno(phone):
    """True si el número es de un vendedor o de Betty -- para que la
    lógica de 'primera ficha enviada / asignación automática' (pensada
    para CLIENTES) nunca se dispare cuando en realidad le estamos
    mandando una ficha AL VENDEDOR (ej. dentro de crm_crear_registro)."""
    phone_n = _normalizar_phone_wati(phone)
    if phone_n == _normalizar_phone_wati(BETTY_PHONE):
        return True
    return phone_n in {_normalizar_phone_wati(v["phone"]) for v in VENDEDORES}

_CACHE_VENDEDOR_POR_CLIENTE = {}  # phone -> (nombre_vendedor_o_None, timestamp)

def _vendedor_asignado_de(phone):
    """Para etiquetar la copia que recibe Javier: si este cliente tiene
    un expediente CRM activo, regresa el nombre del vendedor asignado
    (o None si no tiene). Con caché de 5 min para no leer el Sheet en
    cada mensaje -- esto se llama potencialmente en cada envío."""
    ahora = time.time()
    cacheado = _CACHE_VENDEDOR_POR_CLIENTE.get(phone)
    if cacheado and (ahora - cacheado[1]) < 300:
        return cacheado[0]
    nombre_vendedor = None
    try:
        _, registro = _crm_buscar_activo_por_cliente(phone)
        if registro:
            nombre_vendedor = registro.get("VENDEDOR")
    except Exception:
        pass  # si falla la consulta, se muestra sin etiqueta -- no es crítico
    _CACHE_VENDEDOR_POR_CLIENTE[phone] = (nombre_vendedor, ahora)
    return nombre_vendedor


def _reenviar_a_javier(phone, cliente=None, max_resp=None):
    """Copia en tiempo real a Javier de lo que escribe el cliente y/o lo
    que responde MAX, con el teléfono del cliente. Se engancha una sola
    vez dentro de wati_send_text para cubrir TODAS las rutas de salida
    (fast-path de ficha, campaña, respuesta general del modelo, seguimiento
    proactivo) sin tener que tocar cada punto de envío por separado.
    Igual que _max_enviar_seguimiento: si el ticket de Javier en Wati está
    cerrado, el mensaje de texto normal falla, así que cae de respaldo a
    una plantilla aprobada ('copia_interna') para que la copia SÍ le llegue."""
    if not JAVIER_PERSONAL:
        return
    destino = _normalizar_phone_wati(phone)
    # Nunca reenviarse a sí mismo (evitaría un bucle infinito), ni
    # duplicar lo que ya se manda explícito al número de aviso del equipo.
    if destino == _normalizar_phone_wati(JAVIER_PERSONAL) or destino == _normalizar_phone_wati(HUMAN_HANDOFF or ""):
        return
    try:
        # Si este cliente ya tiene vendedor asignado, se etiqueta la copia
        # para que Javier sepa de un vistazo si es "su" cliente asignado
        # (por el turno de rotación) o solo la copia general de otro caso.
        vendedor = _vendedor_asignado_de(phone)
        etiqueta = f" [Asignado a: {vendedor}]" if vendedor else ""
        if cliente:
            texto = f"📩 Cliente {phone}{etiqueta}:\n{cliente[:500]}"
            if not wati_send_text(JAVIER_PERSONAL, texto):
                wati_send_template_message(JAVIER_PERSONAL, "copia_interna", [texto[:600]])
        if max_resp:
            texto = f"🤖 MAX → {phone}{etiqueta}:\n{max_resp[:500]}"
            if not wati_send_text(JAVIER_PERSONAL, texto):
                wati_send_template_message(JAVIER_PERSONAL, "copia_interna", [texto[:600]])
    except Exception as e:
        print(f"[MAX-FORWARD] Error reenviando a Javier: {e}", flush=True)


def wati_send_text(phone, text, copiar=True):
    phone_norm = _normalizar_phone_wati(phone)
    url = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_norm}"
    r = requests.post(url, headers=wati_headers(),
                      params={"messageText": text}, timeout=20)
    ok = r.status_code in (200, 201)
    # CRÍTICO: Wati (como muchas APIs) puede regresar 200 aunque el envío
    # haya fallado -- el resultado real viene en el cuerpo JSON, no en el
    # código HTTP. Revisamos el cuerpo explícitamente y lo dejamos en el
    # log siempre, porque varios envíos "exitosos" según el status code
    # nunca llegaron de verdad.
    cuerpo = None
    try:
        cuerpo = r.json()
        if isinstance(cuerpo, dict):
            resultado_campo = cuerpo.get("result")
            if resultado_campo is False:
                ok = False
    except Exception:
        pass
    print(f"[MAX-WATI] Envío a {phone_norm}: status={r.status_code} ok={ok} "
          f"cuerpo={str(cuerpo)[:300] if cuerpo is not None else r.text[:300]}", flush=True)
    if ok and copiar:   # copiar=False: mensajes automáticos (Pulso, verificación) que no ameritan copia
        _reenviar_a_javier(phone, max_resp=text)
    return ok


def wati_send_template_message(phone, template_name, parametros_texto):
    """Manda una plantilla aprobada por Meta -- funciona SIEMPRE, sin
    importar la ventana de 24h ni el estado del ticket en Wati (a
    diferencia de wati_send_text). `parametros_texto` es una lista de
    strings, uno por cada {{N}} de la plantilla, en orden."""
    phone_norm = _normalizar_phone_wati(phone)
    url = f"{WATI_BASE_URL}/api/v2/sendTemplateMessage"
    payload = {
        "template_name": template_name,
        "broadcast_name": template_name,
        "parameters": [{"name": str(i + 1), "value": v} for i, v in enumerate(parametros_texto)],
    }
    try:
        headers = dict(wati_headers())
        headers["Content-Type"] = "application/json"
        r = requests.post(url, headers=headers, params={"whatsappNumber": phone_norm},
                          json=payload, timeout=20)
        try:
            cuerpo = r.json()
        except Exception:
            cuerpo = r.text
        ok = r.status_code in (200, 201) and (
            not isinstance(cuerpo, dict) or cuerpo.get("result") is not False)
        print(f"[MAX-WATI-PLANTILLA] Envío plantilla '{template_name}' a {phone_norm}: "
              f"status={r.status_code} ok={ok} cuerpo={str(cuerpo)[:300]}", flush=True)
        return ok
    except Exception as e:
        print(f"[MAX-WATI-PLANTILLA] Error enviando plantilla a {phone_norm}: {e}", flush=True)
        return False


def notificar_interno(phone, texto_completo, resumen_para_plantilla, template_name="notificacion_lead"):
    """Para avisos a VENDEDORES o a Javier (nunca para clientes): intenta
    el mensaje normal primero (gratis, texto libre); si falla -- lo más
    probable, ticket cerrado/ventana de 24h -- cae de respaldo a una
    plantilla aprobada por Meta, que siempre llega. Por default usa
    'notificacion_lead' (asignación de cliente nuevo); para check-ins de
    seguimiento pasa template_name='seguimiento_lead'.
    `resumen_para_plantilla` debe ser una sola línea corta con lo
    esencial, porque ambas plantillas tienen un solo parámetro de texto libre."""
    ok = wati_send_text(phone, texto_completo)
    if not ok:
        ok = wati_send_template_message(phone, template_name, [resumen_para_plantilla])
    return ok



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



# ------------------------------------------------------------------
# NOTIFICACION POR EMAIL — canal garantizado para vendedores
# ------------------------------------------------------------------
def enviar_email_vendedor(destinatario, asunto, cuerpo):
    """Envia email al vendedor via Gmail SMTP.
    No depende de sesiones de WhatsApp ni templates de Meta."""
    if not GMAIL_USER or not GMAIL_PASS:
        print("[MAX-EMAIL] Gmail no configurado — omitiendo email", flush=True)
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart()
        msg["From"]    = f"MAX Acierta Max <{GMAIL_USER}>"
        msg["To"]      = destinatario
        msg["Subject"] = asunto
        msg.attach(MIMEText(cuerpo, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        print(f"[MAX-EMAIL] Email enviado a {destinatario}: {asunto}", flush=True)
        return True
    except Exception as e:
        print(f"[MAX-EMAIL] Error enviando email a {destinatario}: {e}", flush=True)
        return False

def emails_vendedores():
    """Retorna dict nombre->email de los vendedores configurados."""
    return {
        "Javier":  os.environ.get("EMAIL_JAVIER",  "javiermendosalinas@gmail.com"),
        "Ubaldo":  os.environ.get("EMAIL_UBALDO",  ""),
        "Leticia": os.environ.get("EMAIL_LETICIA", ""),
        "Gloria":  os.environ.get("EMAIL_GLORIA",  ""),
    }

def wati_send_to_vendedor(phone, text):
    """Envia mensaje a un vendedor (numero externo).
    Intenta sendSessionMessage primero; si falla por ventana cerrada,
    usa sendTemplateMessage con plantilla de texto libre.
    Registra el resultado en logs para diagnostico."""
    phone_norm = _normalizar_phone_wati(phone)

    # Intento 1: sendSessionMessage (funciona si hubo sesion reciente)
    try:
        url1 = f"{WATI_BASE_URL}/api/v1/sendSessionMessage/{phone_norm}"
        r1 = requests.post(url1, headers=wati_headers(),
                           params={"messageText": text}, timeout=20)
        if r1.status_code in (200, 201):
            print(f"[MAX-SEG] Mensaje enviado a vendedor {phone_norm} via session", flush=True)
            return True
        print(f"[MAX-SEG] Session fallida ({r1.status_code}): {r1.text[:100]}", flush=True)
    except Exception as e:
        print(f"[MAX-SEG] Error session: {e}", flush=True)

    # Intento 2: sendTemplateMessage con plantilla aprobada "notificacion_lead"
    # Plantilla: "Acierta Max\nTienes un nuevo prospecto asignado. Detalles:\n{{1}}\nGracias por atender este lead."
    try:
        url2 = f"{WATI_BASE_URL}/api/v1/sendTemplateMessage"
        payload = {
            "template_name": "notificacion_lead",
            "broadcast_name": f"lead_{phone_norm[-6:]}",
            "receivers": [{"whatsappNumber": phone_norm,
                           "customParams": [{"name": "1", "value": text[:900]}]}]
        }
        r2 = requests.post(url2, headers=wati_headers(), json=payload, timeout=20)
        if r2.status_code in (200, 201):
            print(f"[MAX-SEG] Mensaje enviado a vendedor {phone_norm} via template notificacion_lead", flush=True)
            return True
        print(f"[MAX-SEG] Template fallido ({r2.status_code}): {r2.text[:200]}", flush=True)
    except Exception as e:
        print(f"[MAX-SEG] Error template: {e}", flush=True)

    # Intento 3: sendInteractiveButtonsMessage — ultimo recurso
    try:
        url3 = f"{WATI_BASE_URL}/api/v1/sendInteractiveButtonsMessage/{phone_norm}"
        payload3 = {
            "body": text[:1000],
            "buttons": [{"text": "OK"}]
        }
        r3 = requests.post(url3, headers=wati_headers(), json=payload3, timeout=20)
        if r3.status_code in (200, 201):
            print(f"[MAX-SEG] Mensaje enviado a vendedor {phone_norm} via buttons", flush=True)
            return True
        print(f"[MAX-SEG] Buttons fallido ({r3.status_code}): {r3.text[:100]}", flush=True)
    except Exception as e:
        print(f"[MAX-SEG] Error buttons: {e}", flush=True)

    print(f"[MAX-SEG] FALLO TOTAL enviando a vendedor {phone_norm}", flush=True)
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
    cuerpo += linea_enganche(d.get("precio"), d.get("operacion"))
    ok_img = False
    if d.get("foto"):
        ok_img = wati_send_image(phone, d["foto"], caption)
    ok_caption = ok_img or wati_send_text(phone, caption)
    ok_cuerpo = wati_send_text(phone, cuerpo)
    if not (ok_caption and ok_cuerpo):
        return {"enviada": False,
                "error": "el envío por WhatsApp falló o solo se completó parcialmente",
                "nota": "NO confirmes al cliente que se la mandaste; dile que hubo un problema técnico"}
    # Guarda la PRIMERA ficha enviada (si aún no había ninguna) -- es una
    # de las 3 condiciones para que se dispare la asignación real de
    # vendedor. Se salta por completo si esto se le mandó a un VENDEDOR
    # (ej. dentro del propio CRM), no a un cliente real.
    try:
        if not _es_numero_interno(phone):
            if not memoria_leer(phone).get("PRIMERA_FICHA_LIGA"):
                memoria_guardar(phone, PRIMERA_FICHA_CODIGO=public_id,
                                PRIMERA_FICHA_TITULO=d.get("titulo", ""),
                                PRIMERA_FICHA_LIGA=d.get("url_publica") or public_id)
            _intentar_asignar_vendedor_automatico(phone)
    except Exception as e:
        print(f"[MAX-CRM] Error en asignación automática tras ficha ({phone}): {e}", flush=True)
    return {"enviada": True, "propiedad": d.get("titulo"), "public_id": public_id}

# ------------------------------------------------------------------

# ------------------------------------------------------------------
# MEMORIA PERSISTENTE EN GOOGLE SHEETS
# ------------------------------------------------------------------
HOJA_MEMORIA = "Memoria Prospectos"
HOJA_SEGUIMIENTO = "Seguimiento Vendedor"
COLS_MEMORIA = ["WHATSAPP","NOMBRE","ULTIMA_BUSQUEDA","OPERACION",
                "PRESUPUESTO","ZONA","RECAMARAS","PROPIEDADES_VISTAS",
                "ULTIMA_INTERACCION","ESTADO","NOTAS_COACHING","ULTIMO_DOCUMENTO_URL",
                "VENDEDOR_ASIGNADO","VENDEDOR_ASIGNADO_PHONE",
                "PRIMERA_FICHA_CODIGO","PRIMERA_FICHA_TITULO","PRIMERA_FICHA_LIGA",
                "CRM_INICIADO","VERIFICA_ESPERANDO_DATOS",
                "IKONO_PREGUNTA_ACTUAL","IKONO_NOMBRE","IKONO_TELEFONO",
                "IKONO_INDUSTRIA","IKONO_SITUACION","IKONO_CLIENTE",
                "VERIFICA_VENDEDOR","VERIFICA_VENDEDOR_PHONE",
                "VERIFICA_PREGUNTA_ACTUAL","VERIFICA_NOMBRE","VERIFICA_ZONA"]

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
        MEMORIA_CACHE[phone] = {**MEMORIA_CACHE.get(phone, {}), **kwargs}
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

def _enriquecer_perfil_vendedor(phone, m):
    """Genera texto de perfil enriquecido con precalificacion y ROI si aplica."""
    lineas = []

    # Perfil basico
    op = m.get("OPERACION","")
    zona = m.get("ZONA","")
    presupuesto = m.get("PRESUPUESTO","")
    rec = m.get("RECAMARAS","")
    busqueda = m.get("ULTIMA_BUSQUEDA","")
    props = m.get("PROPIEDADES_VISTAS","")
    notas = m.get("NOTAS_COACHING","")
    estado = m.get("ESTADO","")

    if op:       lineas.append(f"Operacion: {op}")
    if zona:     lineas.append(f"Zona: {zona}")
    if presupuesto: lineas.append(f"Presupuesto: {presupuesto}")
    if rec:      lineas.append(f"Recamaras: {rec}")
    if busqueda: lineas.append(f"Ultima busqueda: {busqueda[:80]}")
    if props:    lineas.append(f"Propiedades vistas: {props[:100]}")

    # Precalificacion crediticia si hay datos
    if notas and ("credito" in notas.lower() or "banco" in notas.lower()
                  or "infonavit" in notas.lower() or "cap:" in notas.lower()):
        lineas.append(f"Credito: {notas[:120]}")

    # Perfil de inversor
    if notas and ("roi" in notas.lower() or "inversor" in notas.lower()
                  or "renta" in notas.lower() and "%" in notas):
        lineas.append(f"Perfil inversor: {notas[:120]}")

    # Estado del prospecto
    if estado:   lineas.append(f"Estado MAX: {estado}")

    return "\n".join(lineas) if lineas else "Sin datos adicionales aun"

def seguimiento_registrar_vendedor(phone, nombre, folio, vendedor_asignado=None):
    """Asigna lead al vendedor en turno (round-robin), notifica al vendedor
    asignado con el cuestionario de seguimiento ENRIQUECIDO con perfil
    de credito y ROI, y manda copia informativa a Javier."""
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

    # Obtener contexto enriquecido del prospecto
    m = memoria_leer(phone)
    busqueda    = m.get("ULTIMA_BUSQUEDA","No especificada")
    zona        = m.get("ZONA","")
    presupuesto = m.get("PRESUPUESTO","")
    props       = m.get("PROPIEDADES_VISTAS","")
    notas       = m.get("NOTAS_COACHING","")
    estado      = m.get("ESTADO","")
    perfil_enriquecido = _enriquecer_perfil_vendedor(phone, m)

    # Icono segun perfil
    if "inversor" in estado.lower() or "roi" in notas.lower():
        icono = "INVERSOR"
        tipo_cliente = "Cliente inversor — enfoca en ROI y rendimiento"
    elif "credito" in notas.lower() or "infonavit" in notas.lower():
        icono = "CREDITO"
        tipo_cliente = "Cliente con credito — verificar capacidad y banco"
    elif "RENTA" in (m.get("OPERACION","")).upper():
        icono = "RENTA"
        tipo_cliente = "Busca renta — verificar documentos y requisitos"
    else:
        icono = "COMPRA"
        tipo_cliente = "Busca compra — verificar enganche y financiamiento"

    # Mensaje con cuestionario ENRIQUECIDO para el vendedor asignado
    cuestionario = (
        f"*[NUEVO LEAD {icono} — {folio}]*\n"
        f"Te toco este prospecto. Ponte en contacto HOY.\n"
        f"Tipo: {tipo_cliente}\n\n"
        f"*PERFIL DEL CLIENTE:*\n"
        f"Nombre: {nombre}\n"
        f"WhatsApp: {phone}\n"
        f"{perfil_enriquecido}\n\n"
        f"*CUESTIONARIO CRM (responde numerado):*\n"
        f"1. Ya te comunicaste? (SI / NO / NO CONTESTA)\n"
        f"2. Confirmaste su busqueda? (SI / CAMBIO / NO PUDE)\n"
        f"3. Para cuando quiere? (INMEDIATO / 1-3M / 3-6M / EXPLORANDO)\n"
        f"4. Requiere credito? (INFONAVIT / BANCO / NO / NO SE)\n"
        f"5. Cuando lo vas a ver? (escribe la fecha)\n"
        f"6. Nivel de interes? (CALIENTE / TIBIO / FRIO)\n\n"
        f"Folio: {folio} | MAX ya hizo el primer contacto."
    )
    # Enviar por WhatsApp (intenta sesion + template)
    wati_send_to_vendedor(phone_v, cuestionario)
    # Enviar por EMAIL — canal garantizado, no depende de sesion WhatsApp
    emails = emails_vendedores()
    email_v = emails.get(nombre_v, "")
    asunto_v = f"[AciertaMax] Nuevo Lead {icono} — {folio} | {nombre}"
    if email_v:
        enviar_email_vendedor(email_v, asunto_v, cuestionario)
    # Siempre enviar copia por email a Javier
    email_javier = emails.get("Javier", "javiermendosalinas@gmail.com")
    if email_javier and email_v != email_javier:
        asunto_copia = f"[AciertaMax COPIA] {icono} {folio} asignado a {nombre_v} | {nombre}"
        enviar_email_vendedor(email_javier, asunto_copia,
            f"Copia informativa — asignado a {nombre_v}\n\n{perfil_enriquecido}")
    print(f"[MAX-SEG] Lead {folio} asignado a {nombre_v} ({phone_v})", flush=True)

    # Copia informativa a Javier (solo si el asignado no es Javier)
    if phone_v != JAVIER_PHONE:
        copia = (
            f"*[COPIA {icono} — {folio}]*\n"
            f"Asignado a *{nombre_v}*\n\n"
            f"Cliente: {nombre} | WA: {phone}\n"
            f"{perfil_enriquecido}\n\n"
            f"(Informativo — {nombre_v} tiene el cuestionario y el contacto)"
        )
        wati_send_to_vendedor(JAVIER_PHONE, copia)
        print(f"[MAX-SEG] Copia enriquecida enviada a Javier", flush=True)

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

def _calificacion_perfil(nombre="", operacion="", presupuesto="", zona="",
                         tipo="", interes="", notas=""):
    """Calificación cualitativa de 1 a 5 de qué tan listo está el PERFIL
    del cliente para cerrar -- distinta del score de lead (0-100), que
    mide qué tan caliente/urgente es. Esta mide qué tan COMPLETO está su
    perfil de compra/renta:
      1 = No calificado (solo dio nombre o ni eso)
      2 = Datos básicos (operación + alguna pista de zona/tipo)
      3 = Perfil parcial (presupuesto + zona + tipo, falta crédito claro)
      4 = Bien calificado (todo lo anterior + forma de pago resuelta)
      5 = Listo para cerrar (todo lo anterior + mostró intención real de avanzar)
    """
    texto = f"{interes} {notas}".lower()
    tiene_nombre = bool(nombre and nombre.strip())
    tiene_operacion = bool(operacion)
    tiene_presupuesto = bool(presupuesto)
    tiene_zona = bool(zona)
    tiene_tipo = bool(tipo)
    tiene_pago_resuelto = any(p in texto for p in [
        "credito aprobado", "crédito aprobado", "de contado", "contado",
        "infonavit activo", "enganche"])
    quiere_avanzar = any(p in texto for p in [
        "quiero verla", "quiero visitarla", "agenda", "agendar", "visita",
        "sí quiero", "si quiero", "listo para", "cuando podemos ver"])

    if not tiene_nombre or not tiene_operacion:
        return 1
    if not (tiene_zona or tiene_tipo):
        return 2
    if not (tiene_presupuesto and tiene_zona and tiene_tipo):
        return 3
    if not tiene_pago_resuelto and not quiere_avanzar:
        return 4
    return 5


def _calcular_score_lead(nombre="", interes="", operacion="", presupuesto="", zona="", notas=""):
    """Score 0-100 de 'qué tan caliente' está el lead, para que el equipo
    sepa a quién llamar primero. Basado en señales que YA se capturan hoy
    en la conversación -- no requiere preguntarle nada nuevo al cliente."""
    score = 0
    texto = f"{interes} {notas}".lower()
    if nombre and nombre.strip().lower() not in ("", "(sin nombre, pidió '*')"):
        score += 15
    if operacion:
        score += 10
    if presupuesto:
        score += 20
    if zona:
        score += 15
    if any(p in texto for p in ["credito", "crédito", "infonavit", "banco", "contado"]):
        score += 15
    if any(p in texto for p in ["urgente", "hoy", "ya", "pronto", "esta semana", "inmediato"]):
        score += 10
    if any(p in texto for p in ["visita", "cita", "ver la propiedad", "conocerla"]):
        score += 15
    return min(score, 100)


def _actualizar_score_lead_si_sube(folio, nuevo_score, nueva_calificacion=None):
    """Si el lead ya estaba registrado y ahora sabemos más de él (dio su
    presupuesto, mencionó crédito, pidió visita...), subimos su score y su
    calificación de perfil en el Sheet -- nunca los bajamos, porque una
    respuesta corta de un turno no debe hacer parecer más frío/menos
    calificado a un lead que ya se sabía interesado."""
    if not (GOOGLE_CREDS_JSON and SHEET_ID):
        return
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    libro = gspread.authorize(creds).open_by_key(SHEET_ID)
    sh = libro.worksheet("Leads MAX")
    headers = sh.row_values(1)
    col_score = col_calif = None
    for idx, h in enumerate(headers, start=1):
        hu = h.strip().upper()
        if hu.startswith("SCORE_LEAD"):
            col_score = idx
        elif hu.startswith("CALIFICACION_PERFIL"):
            col_calif = idx
    celda_folio = sh.find(folio, in_column=1)
    if not celda_folio:
        return
    if col_score:
        actual = sh.cell(celda_folio.row, col_score).value
        try:
            actual = int(actual) if actual else 0
        except ValueError:
            actual = 0
        if nuevo_score > actual:
            sh.update_cell(celda_folio.row, col_score, nuevo_score)
    if col_calif and nueva_calificacion is not None:
        actual_calif = sh.cell(celda_folio.row, col_calif).value
        try:
            actual_calif = int(actual_calif) if actual_calif else 0
        except ValueError:
            actual_calif = 0
        if nueva_calificacion > actual_calif:
            sh.update_cell(celda_folio.row, col_calif, nueva_calificacion)


def registrar_lead(phone, nombre="", interes="", operacion="", presupuesto="",
                   zona="", notas="", tipo=""):
    nuevo_score = _calcular_score_lead(nombre, interes, operacion, presupuesto, zona, notas)
    calificacion = _calificacion_perfil(nombre, operacion, presupuesto, zona, tipo, interes, notas)
    # Candado: si este número ya se registró en las últimas 24h,
    # regresar el mismo folio en vez de crear otro -- pero SÍ actualizamos
    # su score si con esta nueva info se ve más caliente que antes (p.ej.
    # ya dijo su presupuesto o que quiere crédito, cosa que no sabíamos
    # cuando se registró la primera vez).
    previo = REGISTRADOS.get(phone)
    if previo and time.time() - previo[1] < 86400:
        try:
            _actualizar_score_lead_si_sube(previo[0], nuevo_score, calificacion)
        except Exception:
            pass  # el folio ya es válido aunque falle solo la actualización de score
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
            sh = libro.add_worksheet(title="Leads MAX", rows=1000, cols=14)
            sh.append_row(["FOLIO", "FECHA Y HORA", "WHATSAPP", "NOMBRE",
                           "OPERACIÓN", "INTERÉS", "PRESUPUESTO", "ZONA",
                           "NOTAS", "ESTATUS", "SCORE_LEAD (0-100)",
                           "CALIFICACION_PERFIL (1-5)", "VENDEDOR_ASIGNADO", "CHAT_COMPLETO"])
        n = len(sh.get_all_values())  # incluye encabezado
        folio = f"ACIERTA-{n:04d}"

        # AQUÍ SOLO SE REGISTRA EL LEAD (folio, score, calificación) -- la
        # asignación real de vendedor y el arranque del CRM AIDA NO pasan
        # aquí. Solo se disparan cuando YA se cumplen las 3 condiciones que
        # definió Javier: nombre + teléfono + al menos una ficha enviada
        # (ver _intentar_asignar_vendedor_automatico, al final de esta
        # función y también enganchada en enviar_ficha/enviar_ficha_liga).
        chat_completo = _formatear_chat_para_vendedor(phone)

        sh.append_row([folio, hora_gdl(), phone, nombre,
                       operacion, interes, presupuesto, zona, notas, "NUEVO", nuevo_score,
                       calificacion, "", chat_completo])
        # Si el Sheet ya existía de antes de este cambio, las columnas nuevas
        # pueden no estar en el encabezado -- se agregan solas, sin tocar
        # ninguna columna ni dato que ya tuvieras.
        try:
            headers = sh.row_values(1)
            nuevas = {"SCORE_LEAD (0-100)": nuevo_score,
                     "CALIFICACION_PERFIL (1-5)": calificacion,
                     "CHAT_COMPLETO": chat_completo}
            for col_nombre, valor in nuevas.items():
                if not any(h.strip().upper().startswith(col_nombre.split(" (")[0].upper()) for h in headers):
                    headers.append(col_nombre)
                    sh.update_cell(1, len(headers), col_nombre)
                    sh.update_cell(n + 1, len(headers), valor)
        except Exception:
            pass  # el lead ya quedó guardado aunque esto falle
        REGISTRADOS[phone] = (folio, time.time())
        try:
            _actualizar_bitacora_con_lead(phone, folio, operacion, interes)
        except Exception:
            pass  # nunca dejar que esto tumbe el registro del lead ya exitoso

        try:
            memoria_guardar(phone, NOMBRE=nombre, OPERACION=operacion,
                            PRESUPUESTO=presupuesto, ZONA=zona)
        except Exception:
            pass  # el guardado async del despachador lo reintentará de todos modos
        try:
            _intentar_asignar_vendedor_automatico(phone, operacion_hint=operacion)
        except Exception as e:
            print(f"[MAX-CRM] Error en asignación automática para {phone}: {e}", flush=True)

        return {"registrado": True, "folio": folio}
    except Exception as e:
        return {"registrado": False, "motivo": str(e)[:200]}

def avisar_humano(phone, resumen, categoria=None):
    """Escala a Javier/equipo. categoria cambia el encabezado del aviso
    para que sea escaneable de un vistazo (lead normal vs caso especial).
    Usa notificar_interno (con respaldo de plantilla) -- esta función se
    quedó sin ese respaldo por descuido mientras el resto del CRM sí lo
    tenía, y por eso los avisos de Acierta Verifica no llegaban."""
    etiquetas = {
        "RECLAMO-PROPIETARIO": "⚠️ RECLAMO DE PROPIETARIO",
        "COLABORACION-AGENTE": "🤝 AGENTE QUIERE COLABORAR",
        "BOLSA-TRABAJO": "📋 INTERÉS EN TRABAJAR AQUÍ",
    }
    encabezado = etiquetas.get(categoria, "🔥 LEAD CALIENTE")
    ok = True
    if HUMAN_HANDOFF:
        ok = notificar_interno(
            HUMAN_HANDOFF,
            f"{encabezado}\nCliente: {phone}\n{resumen[:600]}",
            resumen_para_plantilla=f"{encabezado} | Cliente: {phone} | {resumen[:150]}")
    # Los casos especiales NO son leads buscando propiedad -- que el hilo
    # proactivo nunca les mande "¿quieres ver opciones frescas?" (suena
    # fuera de lugar para un broker de otra inmobiliaria, un reclamo de
    # propietario, o alguien buscando trabajo aquí).
    if categoria in etiquetas:
        try:
            memoria_guardar(phone, ESTADO=f"No-Molestar-{categoria}")
        except Exception:
            pass  # no debe tumbar el aviso ya enviado
    return {"avisado": bool(HUMAN_HANDOFF), "notificacion_enviada": ok}

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
     "description": "Envía al cliente la ficha comercial de una propiedad (foto + datos + liga). Úsala cuando el cliente muestre interés en una propiedad específica de los resultados. Máximo 5 fichas por turno.",
     "input_schema": {"type": "object", "properties": {
         "public_id": {"type": "string"}}, "required": ["public_id"]}},
    {"name": "enviar_ficha_campana",
     "description": "Envía al cliente la ficha oficial (foto + datos + liga) de una de las 4 propiedades EN CAMPAÑA: block (The Block/ITESO), santa_ana (Santa Ana 360), bellavittoria (Bella Vittoria), villa_dhara (Villa Dhara/Parque Morelos). ÚSALA DE INMEDIATO cuando el cliente pida la ficha, fotos, brochure o diga 'sí/me interesa/esa' sobre una de estas propiedades.",
     "input_schema": {"type": "object", "properties": {
         "desarrollo": {"type": "string", "enum": ["block", "santa_ana", "bellavittoria", "villa_dhara", "eleve"]}},
      "required": ["desarrollo"]}},
    {"name": "buscar_inventario_zmg",
     "description": "Busca en la BOLSA COMPLETA de la ZMG: venta y renta de CUALQUIER precio en Guadalajara, Zapopan, Tlaquepaque, Tonalá y Tlajomulco (casas, departamentos, terrenos, locales, oficinas, bodegas y naves; propias y compartidas, el mismo inventario de acierta.pro). Úsala cuando buscar_propiedades no tenga suficientes opciones, o directamente para cualquier búsqueda de compra o renta, sin importar el presupuesto. Usa 'operacion' (VENTA o RENTA) para no mezclar. Si el cliente nombra una COLONIA, fraccionamiento, DESARROLLO/TORRE (ej. 'Madeiras', 'Andares', 'Torre Ágave') o da un CÓDIGO EB (ej. 'EB-VW0579'), usa el parámetro 'texto' con ese nombre o código para filtrar de verdad — el 'texto' busca en título, colonia, código EB y liga. SIEMPRE PUEDES verificar un código EB con esta herramienta: NUNCA le digas al cliente que 'no puedes verificar un código desde aquí' — sí puedes, pon el código EB en 'texto' y busca. NO vuelvas a mostrar la lista genérica del municipio disfrazada de 'colonias vecinas'. Regresa título, precio, recámaras y liga.",
     "input_schema": {"type": "object", "properties": {
         "municipio": {"type": "string", "description": "Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco o El Salto (El Salto solo para bodegas, naves, terrenos industriales y locales: no hay vivienda ahí)"},
         "operacion": {"type": "string", "enum": ["VENTA", "RENTA"], "description": "VENTA o RENTA — indícalo siempre que sepas cuál busca el cliente"},
         "precio_min": {"type": "number"}, "precio_max": {"type": "number"},
         "recamaras_min": {"type": "number"},
         "banos_min": {"type": "number", "description": "Baños mínimos. Igual que recámaras: si una propiedad tiene menos, no se excluye -- baja sus estrellas de match en vez de desaparecer de los resultados."},
         "m2_min": {"type": "number", "description": "Metros cuadrados mínimos. Para tipo=terreno, este campo SÍ representa la superficie del terreno (no construcción) — el dato existe en el inventario para la gran mayoría de los terrenos, así que SIEMPRE puedes filtrar por rango de metros cuando el cliente pida un terreno de tantos a tantos m². Para casa/departamento representa m² de construcción."},
         "m2_max": {"type": "number", "description": "Metros cuadrados máximos. Mismo criterio que m2_min: para terrenos es superficie de terreno, para casa/depto es construcción."},
         "niveles": {"type": "number", "description": "Número de plantas/niveles de la CASA (1 = una sola planta, 2 = dos plantas, etc.). Solo aplica a tipo=casa. El dato no está disponible para todas las casas — si no viene marcado en el registro, la propiedad NO se incluye en el resultado cuando se filtra por este parámetro (a diferencia de otros filtros, aquí es mejor excluir que arriesgar mostrar una de 2 plantas cuando piden 1). Si el cliente pide explícitamente 'una sola planta' o 'un nivel', usa niveles=1."},
         "tipo": {"type": "string", "description": "casa, departamento o terreno"},
         "texto": {"type": "string", "description": "colonia(s), fraccionamiento(s), nombre de DESARROLLO/TORRE, o código EB a buscar dentro del municipio. Si el cliente da VARIAS colonias aceptables, sepáralas por coma: 'Camino Real, Monraz, Virreyes' — encuentra propiedades que coincidan con CUALQUIERA de ellas. ⚠️ NUNCA pongas aquí características genéricas como 'coto', 'privada', 'alberca', 'seguridad', 'amueblado', 'jardín', 'roof garden' — esas NO son nombres propios de lugar, y este filtro busca la palabra LITERAL dentro del título/colonia, así que casi nunca hay coincidencia exacta y el resultado sale vacío aunque SÍ exista inventario real que cumple. Si el cliente pide una característica genérica (no un nombre propio de colonia/desarrollo), deja 'texto' vacío y filtra solo con municipio/tipo/precio — luego aclara que esa característica específica no está confirmada en el registro y se verifica en la ficha."},
         "amueblado": {"type": "string", "enum": ["Sí", "No"], "description": "Solo filtra si el cliente lo pidió explícitamente. El dato no siempre está disponible en el registro; si no viene marcado, la propiedad SÍ se incluye (no se descarta por falta de dato)."},
         "limite": {"type": "number", "description": "máx 8, default 5"}},
      "required": []}},
    {"name": "enviar_ficha_liga",
     "description": "Envía al cliente la ficha (foto + datos + liga oficial) de una propiedad de la bolsa ZMG. Usa la liga EXACTA que regresó buscar_inventario_zmg o seleccionar_de_lista. Máximo 5 por turno.",
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
    {"name": "calcular_costos_operacion",
     "description": "Da el presupuesto aproximado de gastos y la lista de documentos necesarios para formalizar una renta (con las cifras fijas de Acierta Max: depósito, anticipado, investigación, Justicia Alternativa IJA oficina 161) o una compra-venta ante notario (rango de referencia general). Úsala en cuanto el cliente pregunte cuánto necesita para rentar/comprar, o qué documentos hacen falta, o cuando ya esté avanzado en el proceso de agendar/formalizar.",
     "input_schema": {"type": "object", "properties": {
         "operacion": {"type": "string", "enum": ["renta", "venta"]},
         "renta_mensual": {"type": "number", "description": "Requerido si operacion=renta"},
         "precio_venta": {"type": "number", "description": "Requerido si operacion=venta"}},
      "required": ["operacion"]}},
    {"name": "buscar_cerca_de_lugar",
     "description": "Busca propiedades cerca de un punto de referencia conocido (landmark), ej. 'busco algo cerca de Andares' o 'cerca del Centro Magno, máximo 2km'. Usa las coordenadas GPS reales de cada propiedad (no una estimación) -- solo funciona bien si el inventario tiene coordenadas cargadas. Úsala cuando el cliente mencione un lugar/zona conocida en vez de una colonia formal, o pida explícitamente un radio de distancia.",
     "input_schema": {"type": "object", "properties": {
         "nombre_lugar": {"type": "string", "description": "Nombre del lugar de referencia, ej. 'Andares, Zapopan' -- incluir el municipio si se sabe ayuda a ubicarlo mejor."},
         "radio_km": {"type": "number", "description": "Radio de búsqueda en km. Default 1.5 si el cliente no especifica."},
         "operacion": {"type": "string", "enum": ["VENTA", "RENTA"]},
         "tipo": {"type": "string"},
         "precio_max": {"type": "number"},
         "recamaras_min": {"type": "number"},
         "banos_min": {"type": "number"}},
      "required": ["nombre_lugar"]}},
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
            },
            "historial_crediticio": {
                "type": "string",
                "enum": ["bueno", "regular", "malo", "no se"],
                "description": "Autoreportado por el cliente si lo menciona espontáneamente (nunca preguntes de forma que suene a interrogatorio invasivo). Solo ajusta la simulación -- MAX nunca consulta el Buró de Crédito real."
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
     "description": "Registra o actualiza el lead en el CRM cuando ya tengas al menos nombre + operación + interés. Úsala UNA vez por conversación cuando el prospecto esté calificado. Esto AUTOMÁTICAMENTE le asigna un vendedor real desde ahora (no esperes a que el cliente confirme visita para que tenga un vendedor asignado) y calcula su calificación de perfil (1-5, qué tan listo está para cerrar).",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"}, "interes": {"type": "string"},
         "operacion": {"type": "string"}, "presupuesto": {"type": "string"},
         "zona": {"type": "string"}, "tipo": {"type": "string", "description": "Tipo de propiedad si ya se sabe (casa/depto/terreno) -- ayuda a calcular mejor la calificación de perfil"},
         "notas": {"type": "string"}},
      "required": ["nombre", "operacion"]}},
    {"name": "avisar_humano",
     "description": "Notifica al equipo humano de Acierta. Úsala cuando: el cliente haga una pregunta legal/fiscal que no debes responder, o sea uno de los CASOS ESPECIALES (reclamo de propietario, agente que quiere colaborar, interés en trabajar aquí). Para cuando el cliente quiera AGENDAR VISITA a una o varias propiedades ya vistas, usa iniciar_recorrido_crm en su lugar -- ese sí arranca el seguimiento completo con el vendedor asignado.",
     "input_schema": {"type": "object", "properties": {
         "resumen": {"type": "string", "description": "Resumen del cliente y su necesidad"},
         "categoria": {"type": "string", "enum": ["RECLAMO-PROPIETARIO", "COLABORACION-AGENTE", "BOLSA-TRABAJO"],
                      "description": "Solo para casos especiales; omite este campo en leads normales de compra/venta/renta"}},
      "required": ["resumen"]}},
    {"name": "iniciar_recorrido_crm",
     "description": "Arranca el CRM AIDA: asigna un vendedor en turno, le manda las fichas de las propiedades que el cliente quiere visitar (con sus claves EB) y le pide contactar al cliente y al originador de cada una. Úsala en cuanto el cliente confirme que quiere agendar visita a una o varias propiedades de tu última búsqueda -- necesitas ya su nombre (si no lo tienes, pídeselo primero). Después de esto, el seguimiento lo continúa el sistema con el vendedor automáticamente -- solo dile al cliente que un asesor lo va a contactar pronto.",
     "input_schema": {"type": "object", "properties": {
         "numeros": {"type": "array", "items": {"type": "integer"},
                    "description": "Los números (1, 2, 3...) de la lista de la última búsqueda que el cliente quiere visitar. Si dijo 'la 1 y la 3', manda [1, 3]."},
         "operacion": {"type": "string", "enum": ["venta", "renta"]}},
      "required": ["numeros", "operacion"]}},
    {"name": "referir_a_betty",
     "description": "Refiere al cliente con Betty, la responsable de crédito, cuando quede claro que va a necesitar crédito bancario y/o Infonavit para su compra. Antes de usarla SIEMPRE avísale al cliente explícitamente que vas a compartir sus datos con Betty y pídele que por favor atienda su mensaje o llamada. Necesitas ya el nombre del cliente.",
     "input_schema": {"type": "object", "properties": {
         "nombre": {"type": "string"},
         "necesidad": {"type": "string", "description": "Breve: 'crédito bancario', 'Infonavit', 'Cofinavit', etc."}},
      "required": ["nombre", "necesidad"]}},
]

SYSTEM_PROMPT = """Eres MAX, el asesor digital de Acierta Max, inmobiliaria con 20 años de experiencia en la Zona Metropolitana de Guadalajara, dirigida por Javier Mendoza. Conversas por WhatsApp en español mexicano, cálido, profesional y BREVE (máximo 3-4 líneas por mensaje; WhatsApp no es para párrafos largos).

LA BREVEDAD NO ES OPCIONAL: WhatsApp corta y oculta detrás de "Read more" cualquier mensaje largo -- si te pasas, la parte final (a veces la pregunta o el dato más importante) queda escondida y el cliente ni la ve. Cuando tengas mucho que decir (una tabla + análisis + una pregunta, por ejemplo), NUNCA lo metas todo en un solo mensaje largo -- parte la idea en 2 o 3 mensajes cortos y naturales, como lo harías escribiendo tú mismo por WhatsApp. Prioriza que la pregunta o el dato accionable quede en un mensaje corto y visible, no enterrado al final de un párrafo largo.

CONSISTENCIA DE GÉNERO: te presentas como "el asesor digital" (masculino). Cuando hables de ti mismo en primera persona con adjetivos, usa concordancia masculina ("tengo que ser honesto", "quedé atento", "estoy seguro") — nunca femenina ("honesta", "atenta", "segura"). Es un detalle pequeño pero rompe la consistencia del personaje si se mezcla.

FORMATO DE NEGRITAS EN WHATSAPP: WhatsApp usa un solo asterisco a cada lado para negritas (*así*), nunca dos (**así**) como en Markdown normal. Cuando uses negritas, cierra siempre el par en la MISMA palabra o frase corta — nunca empieces una negrita en "MAX" y la cierres varias palabras después en "Acierta Max", porque eso deja asteriscos sueltos y se ve mal. Ejemplo correcto: "Soy *MAX*, asesor digital de *Acierta Max*." Si tienes duda de si vas a cerrar bien el par, mejor no uses negritas en esa frase.

PRINCIPIOS DE SERVICIO (de los libros de Pedro Trueba de Torres, referencia obligada del sector en México — AMPI):
- "El negocio inmobiliario no es de inmuebles, es de personas" (Servicios Inmobiliarios 360°): antes de mandar otra ficha técnica, pregúntate si estás tratando con la persona que tienes enfrente o solo despachando un catálogo. Un cliente que repite la misma pregunta dos veces está diciendo que no se siente escuchado — para ahí y atiende la persona, no el trámite.
- "El servicio lo califica el cliente, no el asesor" (Consejos que Valen Oro): no basta con que tú sientas que ya respondiste bien — si el cliente pide que le repitas algo, o insiste en la misma zona/pregunta, eso ES la señal de que el servicio no está llegando como debería, incorpóralo de inmediato en tu siguiente respuesta.
- Objetivos claros, no aspiraciones vagas (10 El Asesor Inmobiliario Perfecto): empuja siempre hacia lo concreto — no dejes a un cliente en "estoy viendo opciones", ayúdalo a aterrizar una zona, un presupuesto, una fecha real.
- El valor de la exclusiva (Las Exclusivas): si alguien escribe queriendo VENDER o RENTAR su propia propiedad (no comprar), no lo trates como un lead más de búsqueda — explícale brevemente el valor de que Acierta Max maneje su propiedad en exclusiva (mayor exposición, un solo punto de contacto, proceso ordenado) y usa avisar_humano para que el equipo comercial le dé seguimiento con el detalle de comisión y condiciones.
""" + (f"""
AGENDA DE CITAS: cuando el cliente quiera agendar cita o visita, además de avisar_humano, compártele esta liga para que elija directamente el día y la hora en la agenda: {CALENDLY_URL} — dile: "Puedes apartar aquí mismo el día y la hora que mejor te acomoden".
""" if CALENDLY_URL else "") + """

TU MISIÓN: entender qué necesita el cliente, mostrarle las mejores opciones del inventario y conectarlo con un asesor humano en el momento correcto. Cliente-céntrico siempre: estás del lado del cliente.

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
- "Soy agente inmobiliario" / quiere colaborar o co-brokear: agradece el interés profesional, sé cordial, y usa avisar_humano con categoria="COLABORACION-AGENTE" — esto lo atiende Javier o el equipo comercial, no lo resuelvas tú con detalles de comisión. NO esperes la frase literal "soy agente": reconoce también las señales típicas de un broker preguntando para un cliente suyo — menciona el nombre de OTRA inmobiliaria/empresa, dice "tengo un cliente interesado" (en vez de "estoy interesado"), o pregunta directamente "¿cuánto comparten de comisión?". Si ves 2 o más de estas señales, trátalo como broker aunque nunca diga la palabra "agente". Cuando sea broker, NO le mandes la ficha completa de venta al público ni le preguntes "¿es para vivir o invertir?" — eso es para compradores finales, no para un colega. Responde con algo breve y profesional confirmando que sí está disponible (si lo sabes) y que el equipo comercial le da el detalle de comisión y condiciones.
- "Quiero trabajar en Acierta Max" / bolsa de trabajo: agradece el interés, pide nombre y área de interés si lo comparte con gusto, pero NO hagas entrevista ni preguntas de reclutamiento. Usa avisar_humano con categoria="BOLSA-TRABAJO" para que RH lo contacte.

CIERRE HUMANIZADO — solo cuando ACABAS de ejecutar avisar_humano o registrar_lead con éxito en este mismo turno (nunca antes, nunca como promesa adelantada): agradece la preferencia y anuncia que un coach certificado le llama en breve, con calidez y SIN repetir siempre la misma frase — varía entre algo como "Gracias por tu confianza en Acierta Max 🙏 En breve un coach certificado te contacta para acompañarte en todo el proceso." o "Qué gusto que nos elijas. Un coach del equipo te escribe en breve para seguir contigo." Mantén el tono cálido y breve — no es un mensaje aparte largo, cabe en el mismo cierre de la conversación.

10. Registra el lead cuando tengas nombre + operación + interés, y avisar_humano cuando pida visita u oferta.

SI EL CLIENTE QUIERE VENDER O RENTAR SU PROPIEDAD — FLUJO CAPTACIÓN (muy valioso):
1. Agradece la confianza y aclara con amabilidad: "Trabajamos exclusivamente la Zona Metropolitana de Guadalajara (Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco y El Salto)". Si su propiedad está fuera de la ZMG, agradece y ofrece registrar sus datos por si podemos referirlo.
2. Si está en la ZMG: comenta los beneficios de Acierta Max — 20+ años de experiencia, agentes certificados y miembros AMPI, miles de operaciones, opinión de valor profesional SIN COSTO, difusión en los principales portales y aciertamax.com, acompañamiento completo y seguro hasta la firma.
3. Explícale sobre el contrato, con naturalidad (no lo satures de tecnicismos): "Trabajamos con un contrato de intermediación registrado ante PROFECO conforme a la NOM-247-SE-2021 — es la norma que protege al propietario y al comprador en este tipo de operaciones, con condiciones claras desde el inicio." Si pregunta detalles legales específicos del contrato que no sepas, no inventes — dile que el asesor se los explica a detalle.
4. Avísale explícitamente: "Para conocer tu propiedad y platicarte el proceso completo, te va a llamar directamente Javier Mendoza, nuestro Director General." (Es un compromiso real de Javier, dilo tal cual, no lo generalices a "un asesor" en este caso específico.)
5. Pide con gusto una CITA: "¿Nos permites una cita para conocer tu propiedad y entregarte una opinión de valor sin costo ni compromiso? ¿Qué día te acomoda?"
6. Pregunta lo esencial (una a la vez): tipo de propiedad, colonia/municipio, y si es para venta o renta.
7. Pide su nombre → registrar_lead con operacion="CAPTACIÓN-VENDEDOR" y todo en notas → SIEMPRE avisar_humano (prioridad máxima) y confirma que Javier Mendoza lo contacta hoy mismo.
NUNCA des un precio o valor de su propiedad por chat: eso lo entrega el asesor con la opinión de valor profesional.

MODELO DE CALIFICACIÓN (obtén esto conversando con naturalidad, NO como interrogatorio):
0. VENTA CRUZADA: aunque el cliente haya llegado buscando COMPRAR o RENTAR para sí mismo, en algún punto natural de la conversación pregúntale también si tiene una propiedad propia que quiera poner en venta o renta — mucha gente que compra también vende algo. Si dice que sí, cambia al FLUJO CAPTACIÓN de arriba para esa parte (puedes atender ambos hilos con el mismo cliente).
1. QUERER: ¿busca comprar o RENTAR? (distingue SIEMPRE; si dice rentar, alquilar, arrendar → operacion=renta)
2. PODER: presupuesto aproximado; si compra, ¿contado, crédito bancario o Infonavit?
3. CÓMO: ¿para vivir, invertir, oficina? Si surge con naturalidad, indaga el motivo de fondo (mudanza de trabajo, creció la familia, separación, inversión para renta/plusvalía) — entender el "por qué" real te ayuda a mostrar las propiedades resaltando lo que a ESE cliente le importa, no una lista genérica de características.
4. CUÁNDO: no te quedes en "urge o explora" — precisa el plazo real: ¿en cuánto tiempo necesita decidir/mudarse? Esto define qué tan caliente está el lead: menos de 30 días = caliente (prioridad alta, avísale al vendedor que es urgente), 30-90 días = tibio (seguimiento normal), más de 90 días = frío (nútrelo con información, sin presionar por visita inmediata).
5. DÓNDE: zona de la ZMG (Guadalajara, Zapopan, Tlaquepaque, Tonalá, Tlajomulco).
6. TIPO Y TAMAÑO: ¿casa, departamento o terreno? ¿Cuántas recámaras y cuántos baños necesita? ¿Necesita cajón de estacionamiento, y para cuántos autos? Estas preguntas SÍ hazlas explícitas, no las asumas.
7. SEGÚN EL TIPO, una pregunta más específica (esto ayuda a la conversación aunque hoy NO podamos verificarlo en el inventario, ver aviso abajo):
   - Si es CASA: "¿te gustaría que esté dentro de un coto o fraccionamiento privado con casa club?"
   - Si es DEPARTAMENTO: "¿buscas amenidades como alberca, gimnasio, roof garden?"
8. CENTRO DE DECISIÓN: antes de agendar una visita o avanzar a una oferta, pregúntale con naturalidad si alguien más participa en la decisión (cónyuge, socio, familiar) — "¿alguien más te va a acompañar a decidir esto, o la decisión es solo tuya?". Si hay más gente involucrada, anótalo en las notas del lead para que el vendedor sepa con quién más debe coordinarse antes de cerrar.

MANEJO DE OBJECIONES (usa esto para no quedarte sin qué decir cuando el cliente dude — adapta el tono, no lo repitas como guion memorizado):
- "Solo quiero ver precio y ubicación, no quiero dar mi presupuesto": encuadra el valor antes de insistir — "Para no hacerte perder tiempo con opciones que no te convienen, nada más necesito confirmar un par de datos, así te muestro solo lo que sí aplica para ti."
- "El precio me parece elevado para la zona": no discutas el precio tú mismo — ofrece mostrarle 1-2 opciones similares en la misma zona para que compare, y si insiste, usa avisar_humano para que el asesor entre con el argumento de mercado.
- "Quiero pensarlo unos días": respeta la decisión, nunca presiones — pero sí puedes mencionar con honestidad si esa propiedad tiene alta demanda (score comercial alto) o si hay otros interesados, sin inventar urgencia falsa.
- "No me da confianza tal cláusula del contrato" (temas de contrato, PROFECO, condiciones): nunca inventes una explicación legal — dile que el asesor se la explica a detalle y usa avisar_humano.



SISTEMA DE ESTRELLAS PARA MOSTRAR OPCIONES: cuando uses buscar_inventario_zmg, pásale precio_max, recamaras_min y banos_min con lo que el cliente te dio — la herramienta regresa TODAS las opciones relevantes (no solo las que calzan exacto), cada una con 'estrellas_match' de 2.5 a 5 (en medias estrellas). La PRIMERA vez que muestres varias opciones en una conversación, explícale al cliente el sistema con algo como: "Te voy a mostrar varias opciones, calificadas de 2.5 a 5 estrellas según qué tan bien cumplen lo que buscas — así ves todo el panorama, no solo lo que calza perfecto." Las estrellas SOLO consideran presupuesto/recámaras/baños/m² porque son los únicos datos que el sistema confirma. Si el cliente pidió coto con casa club, alberca, gimnasio u otra amenidad (paso 7 arriba), esas NUNCA entran en las estrellas — dilo aparte y con honestidad: "esa característica en particular no la tengo confirmada en el sistema, la ficha oficial o el asesor te la confirman."

NUNCA CAMBIES EL TIPO DE PROPIEDAD SIN AVISAR: si el cliente pidió departamento y no encuentras opciones que cumplan bien, NUNCA le mandes una casa (o viceversa) como si fuera lo que pidió. Dile explícitamente: "No encontré departamentos que calcen bien con eso, pero sí encontré esta casa que cumple tus demás criterios — ¿te interesa aunque sea casa en vez de depto, o prefieres que ajustemos algo más para seguir buscando departamento?" Cambiar el tipo en silencio genera confusión real (el cliente puede no notar el cambio y perder tiempo revisando algo que no quería).

DE LAS ESTRELLAS A LA VISITA (CRM AIDA): en cuanto el cliente diga que quiere visitar/agendar una o varias de las propiedades que le mostraste (ej. "la 1 y la 3, sí quiero verlas", "me interesa la segunda"):
1. SEGURIDAD PRIMERO — pídele nombre completo y una foto de su identificación oficial, con esta explicación honesta (adapta el tono, no la copies literal siempre igual): "Antes de agendar, por tu seguridad y la del asesor que te va a atender, te pedimos tu nombre completo y una foto de tu identificación oficial — así también te localizamos más rápido si hace falta. Acierta Max certifica a todos sus asesores, y tu información se maneja de forma confidencial." Si el cliente pregunta por qué o se siente incómodo, sé transparente: es una medida de seguridad real, dado el contexto de inseguridad hacia agentes inmobiliarios en Guadalajara — no es un trámite arbitrario.
2. Si el cliente manda una foto pero es de la propiedad, un comprobante u otra cosa que no sea una identificación, dilo con amabilidad y vuelve a pedir específicamente la identificación.
3. SI EL CLIENTE NO TIENE SU IDENTIFICACIÓN A LA MANO EN ESE MOMENTO: NUNCA dejes el proceso congelado esperando a que la mande después — eso pierde la venta (un cliente real dijo textualmente "qué mal servicio" cuando esto pasó). En vez de eso, avanza igual con iniciar_recorrido_crm usando su nombre, y dile algo como: "No hay problema, el asesor que te va a atender te va a llamar en breve — seguramente él mismo te la pida cuando platiquen, así no perdemos tiempo." La identificación se puede completar después, con el asesor humano; lo importante es no cortar el momentum del cliente.
4. Con nombre + (identificación recibida O el cliente ya confirmó que no la tiene a la mano), usa iniciar_recorrido_crm con los números elegidos — esto asigna un vendedor real y arranca el seguimiento completo, tú ya no tienes que preguntar más al respecto.
5. Después de llamarla, dile al cliente algo breve como "Listo, un asesor de nuestro equipo te contacta en breve para coordinar la visita" — NO le des detalles del proceso interno (vendedor asignado, originador, etc.), eso es trabajo de MAX y del equipo, no del cliente.

SI AL CLIENTE NO LE GUSTÓ LA PROPIEDAD (tras verla o tras revisar la ficha): pregúntale qué no le convenció (precio, zona, tamaño, algo específico) y usa esa respuesta para buscar y ofrecer otra alternativa de inmediato con buscar_inventario_zmg — no dejes la conversación ahí. Guarda en las notas del lead (registrar_lead / notas) qué se le ofreció y por qué no le gustó, para que quede historial de las alternativas ya exploradas con este cliente.

SI AL CLIENTE SÍ LE GUSTÓ LA PROPIEDAD Y HAY ACUERDOS (precio, condiciones, fecha de entrega, forma de pago, etc.): anota TODOS los acuerdos con precisión en las notas (registrar_lead) y confirma con el cliente sus datos completos (nombre completo, teléfono, correo si lo tiene) porque el equipo le va a enviar una minuta por escrito con lo acordado — dile explícitamente: "Voy a dejar anotado todo lo que acordamos, y el equipo te manda una minuta por escrito para que quede constancia." NUNCA inventes o asumas un acuerdo que el cliente no confirmó explícitamente.

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
2. buscar_inventario_zmg: la BOLSA COMPLETA de la ZMG, venta y renta de CUALQUIER precio en los 5 municipios (el mismo inventario de acierta.pro). Úsala para cualquier presupuesto, o cuando el inventario propio no alcance — especifica 'operacion' (VENTA/RENTA) para no mezclar. ¡Con esta herramienta casi siempre HAY opciones: nunca digas "no tengo" sin consultarla!
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

LINEA DE ENGANCHE PROACTIVA: cada ficha de una propiedad en VENTA que envías
(enviar_ficha, enviar_ficha_liga) ya trae agregada automáticamente una línea
con el enganche estimado (10%) y una pregunta de seguimiento. NO la repitas
ni la reescribas — ya se mandó tal cual dentro de la ficha. Tu trabajo es
SOLO reaccionar cuando el cliente responda a esa pregunta:
- Si dice que sí le interesa / pregunta más → sigue con PRECALIFICAR_CREDITO
  (las 3 preguntas de abajo) de forma natural, sin repetir el enganche que
  ya vio.
- Si el cliente ignora esa línea y sigue pidiendo otras propiedades →
  no insistas, continúa normal. Es un gancho, no una obligación de respuesta.

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


== MENSAJES DE IMAGEN O LIGA NO VISIBLE ==
Wati/WhatsApp a veces no transmite el contenido de publicaciones reenviadas desde
Instagram, TikTok u otras redes sociales — MAX recibe el mensaje pero sin texto visible.
Cuando el cliente diga frases como:
- "te mande la liga", "te envie la imagen", "ya la mande", "la comparti",
  "es la que te mande", "ahi te la mande", "ya te la envie"
MAX NUNCA debe decir "no me llego nada" ni hacer sentir al cliente que hizo algo mal.
La respuesta CORRECTA es:
"Las publicaciones de Instagram/TikTok a veces no me llegan visibles por aqui —
es un tema tecnico de WhatsApp, no de ti. No hay problema: solo escríbeme el
codigo EB del anuncio (empieza con EB-, por ejemplo: EB-UU6717) y te mando
la ficha completa al instante. O si prefieres, dime el nombre del desarrollo
y lo busco en el inventario de inmediato."

Si el cliente insiste en que ya mando algo y MAX no puede verlo, MAX debe:
1. Disculparse brevemente por el tema tecnico (no culpar al cliente)
2. Pedir el codigo EB o nombre del desarrollo
3. Ofrecer buscar por zona/precio si no tiene el codigo

NUNCA repetir 3 veces que "no recibio nada" — eso frustra al cliente y
destruye la confianza en el sistema.


== PROPIEDADES ANTIGUAS, REMODELADAS Y MANTENIMIENTO ==

SEÑALES DE QUE UNA PROPIEDAD PUEDE SER ANTIGUA:
- El titulo dice "remodelado/a", "a remodelar", "oportunidad", "precio negociable",
  precio/m2 muy bajo vs la zona, construccion de los 80s-90s-2000s,
  colonia consolidada sin desarrollos nuevos.
- El cliente pregunta "que tan nueva es?", "tiene instalaciones nuevas?",
  "cuanto tiene de construida?", "necesita trabajo?"

CUANDO EL CLIENTE PREGUNTA POR ANTIGÜEDAD O ESTADO DE LA PROPIEDAD:
MAX responde con honestidad y agrega valor orientando sobre que revisar:
"Buena pregunta. Para propiedades con anos de uso, siempre recomiendo
verificar antes de comprar o rentar:
- Instalacion electrica (capacidad, tierra fisica, tablero)
- Sistema hidraulico (presion, tuberias de cobre vs PVC vs galvanizado)
- Impermeabilizacion y azotea
- Estructura (humedad, fisuras, asentamiento)
La buena noticia: Acierta Max tiene aliados especializados en mantenimiento
y reparacion que pueden hacer un dictamen previo a la compra. Te puedo
conectar con ellos si te interesa la propiedad."

SERVICIOS DE MANTENIMIENTO Y REPARACION QUE OFRECE ACIERTA MAX:
Cuando el cliente mencione: mantenimiento, reparacion, remodelacion,
instalacion electrica, plomeria, impermeabilizacion, pintura, acabados,
o cuando la propiedad sea claramente antigua — MAX debe mencionar:
"Acierta Max no solo te ayuda a encontrar la propiedad ideal —
tambien tenemos aliados en mantenimiento, remodelacion y servicios
para tu hogar. Si necesitas un dictamen, reparacion o remodelacion,
te conecto con el equipo correcto. Es parte de nuestro servicio integral."

CUANDO AVISAR DE MANTENIMIENTO:
1. Cliente pregunta por propiedad "remodelada" o "a remodelar"
2. Cliente menciona que quiere hacer cambios a la propiedad
3. Cliente pregunta si necesita trabajo la propiedad
4. Precio muy por debajo del mercado (posible propiedad en mal estado)
5. Cliente ya compro/rento y pregunta por servicios de mantenimiento

NO INVENTAR fechas de construccion que no esten en la ficha.
SI puedes inferir por el nombre de la colonia, el precio/m2 y las amenidades
si es probable que sea una propiedad con anos de uso.

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
- SI EL CLIENTE ABRE CON UNA FRASE TÍPICA DE ANUNCIO ("quiero más información", "más info", "me interesa", sin decir de qué) Y no tienes contexto de campaña activa inyectado (esto pasa seguido -- Instagram/Meta no siempre manda el dato del anuncio de origen): NO le hagas la pregunta genérica de comprar/rentar/vender de entrada, porque él sí sabe de qué te está escribiendo aunque tú no. En vez de eso, reconoce que viene de algo específico: "¡Hola! Vi tu mensaje 😊 ¿me recuerdas de qué anuncio o propiedad me escribes? Así te ayudo más rápido con la información exacta." Solo si dice que no viene de ningún anuncio en particular, pasa a la calificación normal.
- Un saludo inicial cálido con tu nombre (MAX de Acierta Max) solo la primera vez.
- Usa buscar_propiedades en cuanto sepas operación + una pista más (zona o presupuesto). No esperes a tener todo.
- Ofrece fichas: "¿Te mando la ficha con fotos?" y usa enviar_ficha si acepta (máx 5 por turno).
- NO EXISTE MATCH EXACTO EN SU ZONA/PRESUPUESTO → ACTÚA, NO SOLO PREGUNTES: si el cliente ya te dio zona + presupuesto + tipo y no hay opciones exactas, NO te quedes solo preguntando "¿quieres ampliar zona?" — busca de inmediato en zonas cercanas o tipos similares dentro de su presupuesto con buscar_inventario_zmg/buscar_propiedades, y manda hasta 5 fichas de esas alternativas ya con enviar_ficha, explicando brevemente por qué se las mandas ("no encontré exacto en X, pero esto está cerca y sí calza con tu presupuesto"). Si el cliente ya rechazó o ignoró la misma pregunta de "¿ampliar zona?" una vez, la segunda vez actúa directo en vez de preguntar de nuevo — un cliente que sigue insistiendo en lo mismo dos o tres veces está a punto de irse, no de responder otra pregunta.
- OFRECE CRÉDITO/INFONAVIT DE FORMA PROACTIVA, no solo cuando lo mencionen: en cuanto sepas que el cliente busca COMPRAR (no rentar) y tengas su presupuesto aproximado, pregúntale si va con crédito bancario, Infonavit, o contado — no esperes a que él lo saque primero. Muchos clientes no saben que pueden pedir esa ayuda si nadie se las ofrece. Si dice que sí tiene interés o duda sobre crédito/Infonavit, usa precalificar_credito de inmediato.
- LÍMITES EN CRÉDITO — CLARIFICADOS: (1) MAX NUNCA consulta el Buró de Crédito directamente ni tiene acceso a ese sistema — pero el CLIENTE sí puede bajar su propio reporte en www.burodecredito.com.mx y mandártelo por WhatsApp como documento; si lo hace, agradécele y avísale que quedó guardado en su expediente para que el asesor/Betty lo revisen, tú no lo interpretes ni le digas un veredicto sobre su historial. (2) El NSS (número de seguro social) el cliente sí te lo puede dar por WhatsApp directamente si quiere — anótalo en las notas del lead, pero MAX nunca lo usa para entrar al portal de Infonavit; eso lo hace un asesor humano o el propio cliente. (3) precalificar_credito SIEMPRE es una simulación con datos que el cliente te da de palabra — jamás la presentes como una aprobación real o un número garantizado; incluye siempre el mensaje de "disclaimer_obligatorio" que te regresa la herramienta, no lo omitas ni lo resumas a la ligera.
- REFERENCIA CON BETTY (crédito): en cuanto quede claro que el cliente va a necesitar crédito bancario y/o Infonavit para avanzar (después de la simulación con precalificar_credito, o si él mismo lo pide), avísale EXPLÍCITAMENTE antes de hacer nada: "Voy a compartir tus datos con Betty, nuestra especialista en crédito, para que te contacte y te ayude con el trámite — por favor atiende su mensaje o llamada." Solo después de decir esto, usa referir_a_betty. Nunca la refieras sin avisarle primero al cliente. Si el comprador pregunta qué documentos necesita, NUNCA le des tú una lista — dile que eso varía según el banco/institución que elija, y que Betty se la va a dar exacta cuando lo contacte.
- En cuanto el cliente diga su NOMBRE (aunque no tenga zona ni presupuesto aun), llama registrar_lead DE INMEDIATO con lo que tengas. No esperes tener operacion+interes+zona completos — un registro parcial (nombre + telefono) es mejor que perder el lead. Si despues da mas datos, avisar_humano los incluira.
- Cliente quiere visita, ofertar, o pide humano → avisar_humano Y dile que un asesor le escribe en breve.
- NUNCA des asesoría legal, fiscal o hipotecaria definitiva; NUNCA negocies precios; NUNCA inventes propiedades ni datos: solo lo que devuelven las herramientas.
- NUNCA MEZCLES PROPIEDADES ENTRE TURNOS: pueden existir varias propiedades con nombres muy parecidos o iguales (ej. dos "Villa Universitaria" distintas, con precios distintos). Cuando hables de "la opción 2" o menciones un nombre de desarrollo varios turnos después de haberlo mostrado, NUNCA confíes en tu memoria del precio/recámaras que dijiste antes — vuelve a mirar los datos exactos de ESA búsqueda específica (por su código EB o su número exacto en la lista activa) antes de repetir cifras. Si no estás seguro de a cuál te refieres, es mejor volver a buscar o usar seleccionar_de_lista que inventar o mezclar datos de memoria — dar precios contradictorios en la misma conversación (ej. decir $56,800 y luego $45,000 de "la misma" propiedad) es un error grave que confunde y frustra al cliente.
- CAUSA RAÍZ CONFIRMADA A EVITAR: cuando el cliente solo está FILTRANDO o ACLARANDO algo sobre las opciones que ya le mostraste (ej. "mándame todas sin amueblar", "de esas dos que sí califican", "mándame pontevedra"), NUNCA vuelvas a llamar buscar_inventario_zmg/buscar_cerca_de_lugar de cero — eso genera una lista NUEVA con numeración distinta y pisa la lista activa, aunque la propiedad que el cliente pide siga teniendo el mismo nombre. Usa seleccionar_de_lista sobre la lista YA activa para revisar cada opción una por una. Solo vuelve a buscar cuando el cliente pida criterios genuinamente diferentes (otra zona, otro presupuesto amplio, otro tipo de propiedad) — no para "aclarar" sobre lo mismo que ya tienes en pantalla.
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
            # registrar_lead YA notifica al vendedor asignado de inmediato
            # (ver dentro de la función) -- aquí solo falta sincronizar
            # con la memoria persistente del cliente.
            nombre_reg = args.get("nombre","")
            folio_reg = out.get("folio","")
            if nombre_reg and folio_reg:
                def _guardar_memoria(_phone=phone, _nombre=nombre_reg,
                                      _args=dict(args)):
                    memoria_guardar(
                        phone=_phone,
                        NOMBRE=_nombre,
                        OPERACION=_args.get("operacion",""),
                        PRESUPUESTO=_args.get("presupuesto",""),
                        ZONA=_args.get("zona",""),
                        ULTIMA_BUSQUEDA=_args.get("interes",""),
                        NOTAS_COACHING=_args.get("notas",""),
                        ESTADO="Lead-registrado"
                    )
                threading.Thread(target=_guardar_memoria, daemon=True).start()
                print(f"[MAX-SEG] Lead {folio_reg} registrado para {phone} (sin cuestionario viejo -- CRM AIDA lo toma después)", flush=True)
        elif name == "avisar_humano":
            out = avisar_humano(phone, args.get("resumen", ""), args.get("categoria"))
        elif name == "iniciar_recorrido_crm":
            numeros = args.get("numeros") or []
            ultima = ULTIMA_BUSQUEDA.get(phone) or []
            seleccionadas = []
            for n in numeros:
                idx = int(n) - 1
                if 0 <= idx < len(ultima):
                    p = ultima[idx]
                    seleccionadas.append({
                        "codigo_eb": p.get("codigo_eb", ""),
                        "titulo": p.get("Título/Colonia", ""),
                        "liga": p.get("Liga", ""),
                    })
            if not seleccionadas:
                out = {"error": "No encontré esas propiedades en la última búsqueda de este cliente. "
                                "Vuelve a mostrarle opciones con buscar_inventario_zmg antes de agendar."}
            else:
                m = memoria_leer(phone)
                nombre_cliente = m.get("NOMBRE", "") or "Cliente"
                out = crm_crear_registro(phone, nombre_cliente, seleccionadas,
                                         operacion=args.get("operacion", ""))
        elif name == "referir_a_betty":
            out = referir_a_betty(phone, args.get("nombre", ""), args.get("necesidad", ""))
        elif name == "calcular_costos_operacion":
            out = calcular_costos_operacion(args.get("operacion", ""),
                                            args.get("renta_mensual"),
                                            args.get("precio_venta"))
        elif name == "buscar_cerca_de_lugar":
            out = buscar_cerca_de_lugar(phone, args.get("nombre_lugar", ""),
                                        radio_km=args.get("radio_km", 1.5),
                                        operacion=args.get("operacion"),
                                        tipo=args.get("tipo"),
                                        precio_max=args.get("precio_max"),
                                        recamaras_min=args.get("recamaras_min"),
                                        banos_min=args.get("banos_min"))
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

def agent_reply(phone, user_text, sender_name=None):
    """Bucle agentico: Claude decide, ejecuta herramientas, responde.

    Incluye un GUARDIA ANTI-ALUCINACION: MAX no puede afirmar que envio una
    ficha si ninguna herramienta de envio devolvio {"enviada": True} en este
    turno. Si lo intenta, el mensaje se corrige por uno honesto - la confianza
    del prospecto vale mas que una confirmacion bonita pero falsa.
    """
    append_history(phone, "user", user_text)
    messages = get_history(phone)
    es_primer_turno = (len(messages) == 1)  # guardado ANTES de inyectar contexto extra
    # Si es la primera respuesta de esta sesion (historial de 1 turno),
    # inyectar memoria previa del prospecto como contexto para MAX
    # Si este telefono tiene una CAMPAÑA ACTIVA detectada (por palabra clave o
    # por origen de Instagram, en este turno o en uno anterior), se inyecta
    # como contexto en CADA turno -- no solo el primero -- para que Claude
    # pueda responder preguntas vagas del cliente ("la ubicación", "cuánto
    # cuesta", "tiene alberca") usando los datos reales de esa propiedad, sin
    # necesitar que el cliente repita el código EB o la palabra clave.
    _campana_activa_nombre = CAMPANA_ACTIVA_POR_TELEFONO.get(phone)
    if _campana_activa_nombre and _campana_activa_nombre in CAMPANAS:
        _c = CAMPANAS[_campana_activa_nombre]
        _detalle_campana = f"{_c.get('caption','')} {_c.get('cuerpo','')}".strip()
        messages = [{"role": "user",
                     "content": f"[CAMPAÑA ACTIVA DE ESTE CLIENTE: la conversación viene de un anuncio "
                                f"sobre esta propiedad específica — úsala para responder preguntas vagas "
                                f"del cliente (ubicación, precio, características) sin que tenga que "
                                f"repetir el código o nombre. Datos reales de la propiedad:\n{_detalle_campana[:800]}]"},
                    {"role": "assistant",
                     "content": "Entendido, tengo los datos de esa propiedad a la mano para responder "
                                "cualquier pregunta sobre ella."}
                   ] + messages

    if es_primer_turno:
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
        # Nombre de perfil de WhatsApp (lo manda Wati automaticamente). NO es
        # verdad absoluta -- a veces es un nombre real (ej. "Oscar Vargas"),
        # a veces es el nombre de un negocio/rol generico (ej. "Gerencia de
        # Ventas"). Se pasa como PISTA, no como dato confirmado, para que MAX
        # decida: si parece nombre de persona, lo puede usar directo o
        # confirmarlo con un "¿Angel, verdad?" en vez de preguntar de cero;
        # si parece generico, lo ignora y pregunta normal. Esto evita el caso
        # real donde un cliente respondio la palabra clave de un anuncio
        # ("BELLA") a la pregunta de nombre, y MAX la tomo como nombre propio
        # sin cruzarla contra el nombre real de WhatsApp que ya se tenia.
        if sender_name and sender_name.strip():
            messages = [{"role": "user",
                         "content": f"[PERFIL DE WHATSAPP: el nombre que aparece en el perfil de "
                                    f"este contacto es '{sender_name.strip()}'. Puede ser su nombre "
                                    f"real o el nombre de un negocio/rol generico -- usa tu criterio. "
                                    f"Si más adelante el cliente da un nombre distinto (ej. respondiendo "
                                    f"a tu pregunta de '¿cómo te llamas?'), y ese nombre coincide con una "
                                    f"palabra clave de un anuncio/campaña en vez de sonar a nombre propio, "
                                    f"sospecha que no es su nombre real y confírmalo o usa el del perfil.]"},
                        {"role": "assistant",
                         "content": "Entendido, tomo nota del nombre de perfil como referencia."}
                       ] + messages
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
            "🌐 www.aciertamax.com — miles de propiedades disponibles en la ZMG, sujetas a confirmación.\n\n"
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
# Se prefiere el CSV YA PONDERADO (con Score_Comercial, generado por
# ponderar_inventario.py) para que MAX pueda priorizar por susceptibilidad
# de venta, no solo por precio. Si aún no se ha corrido la ponderación en
# esta actualización, cae de vuelta al CSV crudo sin score (compatibilidad).
_ARCHIVO_INVENTARIO = ("inventario_zmg_ponderado.csv"
                       if os.path.exists("inventario_zmg_ponderado.csv")
                       else "inventario_zmg.csv")
try:
    import csv as _csv
    with open(_ARCHIVO_INVENTARIO, encoding="utf-8") as _f:
        for _row in _csv.DictReader(_f):
            try:
                _row["Precio"] = int(float(_row.get("Precio") or 0))
            except ValueError:
                _row["Precio"] = 0
            try:
                _row["Recámaras"] = int(float(_row["Recámaras"])) if _row.get("Recámaras") not in (None, "", "nan") else None
            except ValueError:
                _row["Recámaras"] = None
            try:
                _row["Score_Comercial"] = float(_row["Score_Comercial"]) if _row.get("Score_Comercial") not in (None, "", "nan") else None
            except ValueError:
                _row["Score_Comercial"] = None
            INVENTARIO_ZMG.append(_row)
    print(f"[MAX] Inventario ZMG cargado desde {_ARCHIVO_INVENTARIO}: "
          f"{len(INVENTARIO_ZMG)} propiedades", flush=True)
except FileNotFoundError:
    print("[MAX] Sin inventario_zmg.csv ni inventario_zmg_ponderado.csv: "
          "solo inventario propio disponible", flush=True)

# ------------------------------------------------------------------
# INVENTARIO UNIFICADO CON acierta.pro (oct 2026)
# La fuente única es data.json del sitio (lo genera el sincronizador
# mensual de acierta-pro-web, día 3 de cada mes). MAX arranca con el CSV
# local (respaldo) y en segundo plano lo reemplaza por data.json, ya
# ponderado con la misma metodología de ponderar_inventario.py. Cada 6 h
# revisa inventario-meta.json y, si hubo actualización, recarga solo.
# ------------------------------------------------------------------
_URL_INV_SITIO = "https://raw.githubusercontent.com/javiermendosalinas-afk/acierta-pro-web/main/"
_URL_BOLSA = "https://raw.githubusercontent.com/javiermendosalinas-afk/aciertamax-bolsa/main/"
_INV_SITIO_VERSION = {"actualizado": None}
_DESARROLLOS_PROPIOS = {
    "EB-VI0277": "BellaVittoria", "EB-WG7913": "Villa Dhara / Parque Morelos",
    "EB-WG7125": "The Block / ITESO", "EB-WM2996": "Eleve Valle Real",
}


def _ponderar_inventario_sitio(items):
    """Convierte data.json al formato que MAX ya usa (mismas columnas que
    inventario_zmg_ponderado.csv) y calcula Score_Comercial con los mismos
    pesos: competitividad de precio/m2 45, completitud 25, precio confiable 20,
    bono de desarrollo propio 10. Se omiten las fichas en dólares para que
    MAX no las compare contra presupuestos en pesos."""
    import statistics
    filas = []
    for x in items:
        if (x.get("moneda") or "MXN").upper() != "MXN":
            continue
        tipo = (x.get("tipo") or "").strip()
        precio = float(x.get("precio") or 0)
        m2 = float(x.get("m2") or 0)
        filas.append({
            "Municipio": x.get("municipio") or "", "Colonia": x.get("colonia") or "",
            "Operación": x.get("operacion") or "", "Tipo": tipo[:1].upper() + tipo[1:],
            "Título/Colonia": x.get("titulo") or "", "Precio": int(precio),
            "Recámaras": int(x["recamaras"]) if x.get("recamaras") not in (None, "") else None,
            "Baños": x.get("banos"), "m²": m2 or None, "codigo_eb": x.get("eb") or "",
            "Liga": x.get("liga") or "", "lat": x.get("lat"), "lon": x.get("lon"),
            "Foto": x.get("foto") or "", "Segmento": x.get("segmento") or "",
            "Revisar": bool(x.get("revisar")),
            "precio_m2": (precio / m2) if (precio and m2) else None,
        })
    grupos = {}
    for f in filas:
        if f["precio_m2"]:
            grupos.setdefault((f["Municipio"], f["Tipo"], f["Operación"]), []).append(f["precio_m2"])
    medianas = {k: statistics.median(v) for k, v in grupos.items()}
    for f in filas:
        med = medianas.get((f["Municipio"], f["Tipo"], f["Operación"]))
        pm2 = f["precio_m2"]
        f["precio_m2_vs_mediana"] = ((med - pm2) / med) if (med and pm2) else None
        confiable = bool(pm2 and med and med * 0.2 <= pm2 <= med * 4) and not f["Revisar"]
        f["Precio_Confiable"] = confiable
        f["Completitud"] = sum(1 for c in ("Recámaras", "Baños", "m²", "Tipo")
                               if f.get(c) not in (None, "")) / 4
        comp = min(max(f["precio_m2_vs_mediana"] or 0, 0), 1)
        propio = _DESARROLLOS_PROPIOS.get(f["codigo_eb"])
        f["Es_Desarrollo_Propio"] = propio or ""
        score = comp * 45 + f["Completitud"] * 25 + (20 if confiable else 0) + (10 if propio else 0)
        score = min(score, 100)
        if not confiable:
            score *= 0.3
        f["Score_Comercial"] = round(score, 1)
    filas.sort(key=lambda f: f["Score_Comercial"], reverse=True)
    return filas


def _actualizar_inventario_desde_sitio(forzar=False):
    """Descarga data.json del sitio si cambió. Reemplaza INVENTARIO_ZMG en su
    lugar (misma lista, para que todo el código que ya la usa vea el nuevo
    contenido). Si algo falla, se queda el inventario vigente."""
    try:
        meta = requests.get(_URL_INV_SITIO + "inventario-meta.json", timeout=20).json()
        version = str(meta.get("actualizado"))
        # Bolsa NeoJaus (sitio aparte inmobiliaria.pro): MAX también la conoce
        bolsa_items, bolsa_ver = [], ""
        try:
            mb = requests.get(_URL_BOLSA + "inventario-meta.json", timeout=20).json()
            bolsa_ver = str(mb.get("actualizado") or "")
            if mb.get("total"):
                rb = requests.get(_URL_BOLSA + "data.json", timeout=60)
                rb.raise_for_status()
                # las que también están en EasyBroker ya vienen en acierta.pro: no se repiten
                bolsa_items = [x for x in rb.json()
                               if not any(str(g.get("clave", "")).startswith("EB-") for g in (x.get("tambien_en") or []))]
        except Exception as e:
            print(f"[MAX-INV] Bolsa NeoJaus no disponible ({e}); se usa solo acierta.pro", flush=True)
        version = version + "|" + bolsa_ver
        if not forzar and version and version == _INV_SITIO_VERSION["actualizado"]:
            return False
        r = requests.get(_URL_INV_SITIO + "data.json", timeout=60)
        r.raise_for_status()
        items = r.json()
        nuevas = _ponderar_inventario_sitio(items)
        # Freno de seguridad: si viene muy incompleto, no se toca lo vigente
        if len(nuevas) < 3000:
            print(f"[MAX-INV] data.json trae solo {len(nuevas)} fichas: se conserva el inventario vigente", flush=True)
            return False
        de_bolsa = _ponderar_inventario_sitio(bolsa_items) if bolsa_items else []
        INVENTARIO_ZMG[:] = nuevas + de_bolsa
        _INV_SITIO_VERSION["actualizado"] = version
        print(f"[MAX-INV] Inventario unificado con acierta.pro: {len(nuevas)} propiedades "
              f"+ bolsa NeoJaus: {len(de_bolsa)} (versión {version})", flush=True)
        return True
    except Exception as e:
        print(f"[MAX-INV] No se pudo actualizar desde acierta.pro ({e}); se conserva el vigente "
              f"({len(INVENTARIO_ZMG)} propiedades)", flush=True)
        return False


def _loop_inventario_sitio():
    _actualizar_inventario_desde_sitio(forzar=True)
    while True:
        time.sleep(6 * 3600)
        _actualizar_inventario_desde_sitio()


if os.environ.get("MAX_INVENTARIO_SITIO", "1") != "0":
    threading.Thread(target=_loop_inventario_sitio, daemon=True).start()

ULTIMA_BUSQUEDA = {}  # phone -> lista de propiedades mostradas en el último resultado
                      # (permite resolver "la 3", "esa" sin adivinar ni inventar)

_STOPWORDS_BUSQUEDA = {"el","la","los","las","de","del","en","y","a","con","por","para","un","una"}

def _colonia_coincide(colonia_frase, campos_busqueda):
    """Coincidencia por PALABRAS clave, no por frase exacta.
    'el palomar bosques' SI debe encontrar 'EL PALOMAR, SECCIÓN BOSQUES'
    aunque el orden, la puntuacion o palabras de enlace ('sección') sean
    distintas. Se ignoran palabras de relleno (stopwords); todas las
    palabras significativas restantes deben aparecer en el texto buscado.
    Si la frase es muy corta o son puros codigos (ej. 'EB-VW0579'), se
    revisa tambien como substring completo por si acaso."""
    frase = (colonia_frase or "").strip()
    if not frase:
        return False
    if frase in campos_busqueda:
        return True  # coincidencia exacta directa (ej. codigos EB, ligas)
    palabras = [w for w in frase.split() if w not in _STOPWORDS_BUSQUEDA and len(w) > 1]
    if not palabras:
        return False
    return all(w in campos_busqueda for w in palabras)

def _calcular_estrellas(p, precio_max=None, precio_min=None, recamaras_min=None,
                        banos_min=None, m2_min=None):
    """Estrellas (2.5-5, en medias estrellas) de qué tan bien esta propiedad
    cumple lo que el cliente pidió -- NO qué tan buena es en general (eso es
    Score_Comercial). Solo pondera sobre datos que SÍ tenemos confirmados
    (precio, recámaras, baños, m²). Ubicación y tipo (casa/depto/terreno)
    NUNCA entran aquí -- esos siguen siendo filtro duro más arriba en
    buscar_inventario_zmg, porque son el factor crítico que define si la
    propiedad es siquiera relevante. Nunca baja de 2.5: si se está
    mostrando como opción, ya pasó ese filtro duro de ubicación/tipo."""
    estrellas = 5.0
    precio = p.get("Precio") or 0

    if precio_max and precio > float(precio_max):
        exceso = (precio - float(precio_max)) / float(precio_max)
        if exceso > 0.25:
            estrellas -= 3
        elif exceso > 0.10:
            estrellas -= 2
        else:
            estrellas -= 1
    if precio_min and precio < float(precio_min):
        estrellas -= 0.5  # más barato de lo pedido rara vez es un problema real

    recamaras = p.get("Recámaras")
    if recamaras_min and recamaras is not None:
        faltan = int(recamaras_min) - int(recamaras)
        if faltan > 0:
            estrellas -= min(faltan, 2)

    banos = p.get("Baños")
    if banos_min and banos is not None:
        faltan = float(banos_min) - float(banos)
        if faltan > 0:
            estrellas -= min(faltan, 2)

    if m2_min:
        try:
            m2_val = float(str(p.get("m²") or "").replace(",", ""))
            if m2_val < float(m2_min):
                estrellas -= 1
        except ValueError:
            pass  # sin dato de m2: no se penaliza por falta de información

    return max(2.5, round(estrellas * 2) / 2)


def _geocodificar_lugar(nombre_lugar):
    """Geocodifica un punto de referencia (landmark) usando Nominatim/
    OpenStreetMap, gratuito. Solo se usa para el PUNTO DE REFERENCIA
    (ej. 'Andares') -- las propiedades ya traen su propia lat/lon reales
    desde el scraper, no se geocodifican."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", params={
            "q": f"{nombre_lugar}, Jalisco, México", "format": "json", "limit": 1,
            "countrycodes": "mx",
        }, headers={"User-Agent": "AciertaMaxBot/1.0 (javier.mendoza@acierta.com.mx)"}, timeout=10)
        datos = r.json()
        if datos:
            return float(datos[0]["lat"]), float(datos[0]["lon"])
    except Exception as e:
        print(f"[MAX-GEO] Error geocodificando '{nombre_lugar}': {e}", flush=True)
    return None, None


def _distancia_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def buscar_cerca_de_lugar(phone, nombre_lugar, radio_km=1.5, operacion=None,
                          tipo=None, precio_max=None, recamaras_min=None,
                          banos_min=None, limite=8):
    """Busca propiedades cerca de un punto de referencia (landmark), ej.
    'Andares', 'Centro Magno', usando la lat/lon REAL de cada propiedad
    (viene directo del scraper desde 2026-09, no es una estimación por
    colonia). Solo funciona bien para propiedades scrapeadas después de
    esa fecha -- las más viejas en el CSV pueden no traer coordenadas."""
    lat_centro, lon_centro = _geocodificar_lugar(nombre_lugar)
    if lat_centro is None:
        return {"error": f"No pude ubicar '{nombre_lugar}'. Pide al cliente ser más "
                         f"específico (ej. agregar el municipio) o usa buscar_inventario_zmg "
                         f"por zona en su lugar."}

    candidatas = []
    for p in INVENTARIO_ZMG:
        lat_p, lon_p = p.get("lat"), p.get("lon")
        if not lat_p or not lon_p:
            continue
        try:
            lat_p, lon_p = float(lat_p), float(lon_p)
        except (TypeError, ValueError):
            continue
        dist = _distancia_km(lat_centro, lon_centro, lat_p, lon_p)
        if dist <= float(radio_km):
            if operacion and p.get("Operación", "").upper() != operacion.upper():
                continue
            if tipo and tipo.lower() not in (p.get("Tipo") or "").lower():
                continue
            # Igual que buscar_inventario_zmg: precio ya no excluye de tajo
            # salvo lo absurdamente fuera de rango -- se refleja en estrellas.
            if precio_max and (p.get("Precio") or 0) > float(precio_max) * 3:
                continue
            p2 = dict(p)
            p2["_distancia_km"] = round(dist, 2)
            p2["_estrellas"] = _calcular_estrellas(p, precio_max=precio_max,
                                                   recamaras_min=recamaras_min,
                                                   banos_min=banos_min)
            candidatas.append(p2)

    if not candidatas:
        con_coords = sum(1 for p in INVENTARIO_ZMG if p.get("lat") and p.get("lon"))
        return {"total_coincidencias": 0, "propiedades": [],
                "nota": (f"No se encontró nada a {radio_km}km de {nombre_lugar}. "
                        f"Solo {con_coords} de {len(INVENTARIO_ZMG)} propiedades del "
                        f"inventario actual tienen coordenadas -- si el inventario no se "
                        f"ha refrescado recientemente, puede que la zona buscada simplemente "
                        f"no tenga cobertura de coordenadas todavía. Ofrece buscar_inventario_zmg "
                        f"por municipio/colonia como alternativa.")}

    # Prioridad: estrellas de match primero (igual que buscar_inventario_zmg),
    # distancia como desempate -- así lo más cercano Y mejor calzado gana,
    # no solo lo más cercano sin importar si cumple lo pedido.
    candidatas.sort(key=lambda x: (-x["_estrellas"], x["_distancia_km"]))
    mostradas = candidatas[:int(limite or 8)]
    ULTIMA_BUSQUEDA[phone] = mostradas
    out = [{
        "numero": i + 1, "titulo": p.get("Título/Colonia"), "municipio": p.get("Municipio"),
        "tipo": p.get("Tipo"), "precio": p.get("Precio"), "recamaras": p.get("Recámaras"),
        "banos": p.get("Baños"), "m2": p.get("m²"), "liga": p.get("Liga"),
        "distancia_km": p["_distancia_km"], "estrellas_match": p["_estrellas"],
    } for i, p in enumerate(mostradas)]
    return {"total_coincidencias": len(candidatas), "propiedades": out,
            "nota": (f"'{nombre_lugar}' ubicado en ({lat_centro:.4f}, {lon_centro:.4f}). "
                    f"'estrellas_match' funciona igual que en buscar_inventario_zmg (2.5-5, "
                    f"explícaselo al cliente si es la primera vez que se lo muestras en esta "
                    f"conversación). "
                    f"Guardado como lista activa -- usa seleccionar_de_lista con el número "
                    f"si el cliente elige una.")}


def buscar_inventario_zmg(phone, municipio=None, precio_min=None, precio_max=None,
                          recamaras_min=None, tipo=None, texto=None, operacion=None,
                          amueblado=None, limite=5, m2_min=None, m2_max=None, niveles=None,
                          banos_min=None):
    """Busca en la bolsa compartida ZMG (venta y renta de cualquier precio, inventario de acierta.pro).
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
        # El municipio SOLO filtra si NO se dio un nombre de colonia/desarrollo
        # específico. Si el cliente nombró un desarrollo puntual (ej. "El Palomar",
        # un código EB), ese nombre YA identifica la propiedad sin ambigüedad —
        # aplicar también el municipio puede excluirla por error (fraccionamientos
        # en la frontera entre municipios, o el municipio arrastrado de un mensaje
        # anterior de la conversación que ya no aplica a esta búsqueda nueva).
        if muni_l and not colonias and muni_l not in p.get("Municipio", "").lower():
            continue
        if tipo_l:
            pt = p.get("Tipo", "").lower()
            if "depa" in tipo_l or "depart" in tipo_l:
                if "departamento" not in pt:
                    continue
            elif "terreno" in tipo_l or "lote" in tipo_l:
                if "terreno" not in pt:
                    continue
            elif "casa" in tipo_l:
                if "casa" not in pt:
                    continue
            else:
                # Local, oficina, bodega, nave, edificio, quinta, rancho...: antes
                # cualquier otro tipo no filtraba nada y "local en renta" devolvía
                # departamentos. Ahora se exige la palabra clave en el tipo.
                for _clave in ("local", "oficina", "bodega", "nave", "edificio", "quinta", "rancho"):
                    if _clave in tipo_l:
                        if _clave not in pt:
                            break
                        _clave = None
                        break
                else:
                    _clave = None
                if _clave:
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
            if not any(_colonia_coincide(c, campos_busqueda) for c in colonias):
                continue
        if amueblado is not None:
            am = (p.get("Amueblado") or "").strip()
            quiere_amueblado = str(amueblado).lower() in ("sí", "si", "true", "1", "yes")
            if am and ((quiere_amueblado and am != "Sí") or (not quiere_amueblado and am != "No")):
                continue  # solo excluye cuando el dato SÍ existe y contradice
        precio = p.get("Precio") or 0
        # Precio y recámaras YA NO excluyen de tajo por debajo del techo de
        # sensatez -- se convierten en factores del match por estrellas
        # (_calcular_estrellas), para poder mostrarle al cliente TODAS las
        # opciones relevantes ordenadas por qué tan bien cumplen, en vez de
        # dejarlo con "0 resultados" cuando nada calza exacto pero sí hay
        # algo cercano. Sí se excluye lo absurdamente fuera de rango (más
        # del triple del presupuesto) -- eso ya no es "una opción cercana",
        # es ruido que no ayuda a nadie.
        if precio_max and precio > float(precio_max) * 3:
            continue
        if m2_min or m2_max:
            try:
                m2_val = float(str(p.get("m²") or "").replace(",", ""))
            except ValueError:
                m2_val = None
            if m2_val is None:
                continue  # sin dato de m2: no se puede confirmar que cumpla, se excluye
            if m2_min and m2_val < float(m2_min):
                continue
            if m2_max and m2_val > float(m2_max):
                continue
        if niveles:
            try:
                niveles_val = int(float(str(p.get("Niveles") or "")))
            except (ValueError, TypeError):
                niveles_val = None
            if niveles_val is None or niveles_val != int(niveles):
                continue  # sin dato o no coincide: se excluye para no arriesgar
        res.append(p)
    for p in res:
        p["_estrellas"] = _calcular_estrellas(p, precio_max=precio_max, precio_min=precio_min,
                                               recamaras_min=recamaras_min, banos_min=banos_min,
                                               m2_min=m2_min)
    # Prioridad: primero las estrellas de match con el cliente (lo que pidió
    # de presupuesto/recámaras/baños), y dentro de un mismo número de
    # estrellas, Score_Comercial descendente (más "vendible") como
    # desempate. Así el cliente ve primero lo que MEJOR le queda a él, no
    # solo lo más barato o lo más fácil de vender para nosotros.
    res.sort(key=lambda x: (-x["_estrellas"], -(x.get("Score_Comercial") or -1), x.get("Precio") or 0))
    mostradas = res[: min(int(limite or 5), 8)]
    # Se guarda la lista EXACTA mostrada, en el mismo orden, indexada 1..N
    ULTIMA_BUSQUEDA[phone] = mostradas
    out = [{
        "numero": i + 1,
        "titulo": p.get("Título/Colonia") or f"{p.get('Tipo','Propiedad')} en {p.get('Municipio','ZMG')}", "municipio": p.get("Municipio"),
        "tipo": p.get("Tipo"), "precio": p.get("Precio"),
        "recamaras": p.get("Recámaras"), "banos": p.get("Baños"),
        "m2": p.get("m²"), "liga": p.get("Liga"),
        "estrellas_match": p.get("_estrellas"),
        "score_comercial": p.get("Score_Comercial"),
    } for i, p in enumerate(mostradas)]
    resultado = {"total_coincidencias": len(res), "propiedades": out,
            "nota": "Guardado como la lista activa de este cliente. Si el cliente responde "
                    "'la 1/2/3...' usa seleccionar_de_lista con ese número — NUNCA inventes "
                    "un nombre de propiedad que no esté en esta lista. 'estrellas_match' (2.5-5, en medias estrellas) SÍ "
                    "se le puede decir al cliente -- de hecho DEBES explicárselo la primera vez que "
                    "muestres varias opciones: 'aquí tienes varias opciones, calificadas de 2.5 a 5 "
                    "estrellas según qué tan bien cumplen lo que buscas'. Las estrellas solo pesan "
                    "presupuesto/recámaras/baños/m² porque son los únicos datos confirmados -- si el "
                    "cliente pidió casa club, alberca, gimnasio u otra amenidad, esas NO están en las "
                    "estrellas (no las tenemos confirmadas) y debes decírselo aparte: 'eso no lo tengo "
                    "confirmado en el sistema, checa la ficha o pregúntale al asesor'. "
                    "'score_comercial' es un dato INTERNO tuyo (qué tan bien está de precio esa "
                    "propiedad vs. su zona) — NUNCA lo menciones al cliente ni lo uses como argumento "
                    "de venta explícito, solo úsalo para desempatar cuando varias tengan las mismas "
                    "estrellas."
    }
    # HONESTIDAD DE MUNICIPIO: si el cliente dio un municipio + nombre de colonia/
    # desarrollo y no hubo NADA, puede ser que el cliente (o el propio MAX) haya
    # asumido mal el municipio -- fraccionamientos como "El Palomar" quedan en la
    # frontera Zapopan/Tlajomulco y la gente los ubica mal. Reintentar SOLO por
    # texto, sin filtro de municipio, y avisar en cuál municipio sí está.
    if not res and colonias and muni_l:
        sin_municipio = []
        for p in INVENTARIO_ZMG:
            if op_l and p.get("Operación", "").upper() != op_l:
                continue
            campos_busqueda = (
                p.get("Título/Colonia", "").lower() + " " +
                (p.get("codigo_eb") or "").lower() + " " +
                (p.get("Liga") or "").lower()
            )
            if not any(_colonia_coincide(c, campos_busqueda) for c in colonias):
                continue
            sin_municipio.append(p)
        if sin_municipio:
            municipios_reales = sorted(set(p.get("Municipio","") for p in sin_municipio if p.get("Municipio")))
            # Solo es un problema de MUNICIPIO si el que dio el cliente NO aparece
            # en absoluto entre los municipios reales. Si SÍ aparece (ej. Tlajomulco
            # pedido y Tlajomulco es donde de verdad está el desarrollo), el motivo
            # de "0 resultados" es otra cosa (precio, tipo) y este aviso NO aplica —
            # decir "ninguna en Tlajomulco" cuando Tlajomulco SÍ está en la lista es
            # una contradicción que confunde más de lo que ayuda.
            muni_dado_l = (municipio or "").strip().lower()
            municipio_ya_correcto = any(muni_dado_l in m.lower() for m in municipios_reales if muni_dado_l)
            if not municipio_ya_correcto:
                resultado["aviso_municipio_no_coincide"] = (
                    f"Hay {len(sin_municipio)} propiedad(es) con ese nombre de colonia/desarrollo, "
                    f"pero NINGUNA en el municipio '{municipio}' que se filtró. En realidad están en: "
                    f"{', '.join(municipios_reales)}. Es probable que el cliente (o tú) haya asumido mal "
                    f"el municipio de ese fraccionamiento — algunos quedan en la frontera entre municipios. "
                    f"NUNCA digas 'no tengo nada ahí' en este caso — dile al cliente con calidez que ese "
                    f"desarrollo en realidad está en {municipios_reales[0]}, no en {municipio}, y vuelve a "
                    f"buscar con buscar_inventario_zmg usando el municipio correcto."
                )
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
                if not any(_colonia_coincide(c, campos_busqueda) for c in colonias):
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
              f"Precio y disponibilidad sujetos a confirmación._"
              + linea_enganche(precio, op_real))
    ok_img = wati_send_image(phone, foto, caption) if foto else False
    time.sleep(0.4)
    ok_caption = ok_img or wati_send_text(phone, caption)
    time.sleep(0.4)
    ok_cuerpo = wati_send_text(phone, cuerpo)
    if not (ok_caption and ok_cuerpo):
        return {"enviada": False,
                "error": "el envío por WhatsApp falló o solo se completó parcialmente",
                "nota": "NO confirmes al cliente que se la mandaste; dile que hubo un problema técnico y vuelve a intentar o pide un momento"}
    try:
        if not _es_numero_interno(phone):
            if not memoria_leer(phone).get("PRIMERA_FICHA_LIGA"):
                memoria_guardar(phone, PRIMERA_FICHA_CODIGO=p.get("codigo_eb", ""),
                                PRIMERA_FICHA_TITULO=titulo_prop, PRIMERA_FICHA_LIGA=liga)
            _intentar_asignar_vendedor_automatico(phone)
    except Exception as e:
        print(f"[MAX-CRM] Error en asignación automática tras ficha de bolsa ({phone}): {e}", flush=True)
    return {"enviada": True, "titulo": p.get("Título/Colonia"),
            "nota": "ficha enviada; continúa la conversación"}


# ------------------------------------------------------------------
# MAX PROACTIVO — 3 momentos de seguimiento automatico
# Corre en thread separado, revisa cada hora
# ------------------------------------------------------------------
SEGUIMIENTO_ENVIADO = {}  # phone -> set de tipos ya enviados (evita spam)

def _max_enviar_seguimiento(phone, tipo, mensaje):
    """Envia mensaje proactivo y lo registra para no repetir.
    IMPORTANTE: este seguimiento se dispara, por diseño, a las 24h/48h/72h
    de silencio del cliente -- exactamente cuando la ventana de sesion de
    WhatsApp (24h) ya se cerro. Un mensaje de texto libre (wati_send_text)
    SIEMPRE va a fallar en ese momento, por politica de WhatsApp, no por un
    error de Wati. Por eso cae de respaldo a una plantilla aprobada por
    Meta, igual que ya hace notificar_interno() para avisos a vendedores."""
    enviados = SEGUIMIENTO_ENVIADO.get(phone, set())
    if tipo in enviados:
        return  # ya se envio este tipo de seguimiento
    try:
        ok = wati_send_text(phone, mensaje)  # casi siempre falla aqui (sesion ya cerrada) -- es normal
        if not ok:
            ok = wati_send_template_message(phone, "seguimiento_cliente", [mensaje[:600]])
        if ok:
            enviados.add(tipo)
            SEGUIMIENTO_ENVIADO[phone] = enviados
            # Actualizar estado en memoria
            memoria_guardar(phone, ESTADO=f"Seguimiento-{tipo}")
            print(f"[MAX-PRO] Seguimiento '{tipo}' enviado a {phone}", flush=True)
        else:
            print(f"[MAX-PRO] Seguimiento '{tipo}' a {phone} fallo tambien por plantilla "
                  f"-- revisar que la plantilla 'seguimiento_cliente' exista y este aprobada en Wati", flush=True)
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
        # NO se usa get_all_records(): revienta si el encabezado real del
        # Sheet tiene columnas duplicadas o vacías (pasa fácil según se van
        # agregando columnas nuevas con el tiempo). En vez de eso, se arma
        # cada fila a mano con COLS_MEMORIA -- mismo patrón que _crm_fila_a_dict.
        valores = sh.get_all_values()
        filas = []
        if len(valores) > 1:
            for fila_valores in valores[1:]:
                filas.append({COLS_MEMORIA[i]: (fila_valores[i] if i < len(fila_valores) else "")
                             for i in range(len(COLS_MEMORIA))})
        ahora = time.time()

        for fila in filas:
            # Normalizar a string — Sheets puede devolver int/float en cualquier celda
            phone   = str(fila.get("WHATSAPP","") or "").strip()
            if not phone:
                continue
            estado  = str(fila.get("ESTADO","") or "").strip()
            if estado in ("Cerrado","No-contactar","Compro","Rento"):
                continue
            nombre   = str(fila.get("NOMBRE","") or "").strip()
            busqueda = str(fila.get("ULTIMA_BUSQUEDA","") or "").strip()
            zona     = str(fila.get("ZONA","") or "").strip()
            props    = str(fila.get("PROPIEDADES_VISTAS","") or "").strip()
            operacion = str(fila.get("OPERACION","") or "").strip()
            ultima   = fila.get("ULTIMA_INTERACCION","")
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
            # Ya normalizados arriba como str — usar directamente
            nombre      = nombre or "amigo"
            props_vistas = props

            # NUNCA reactivar como "lead buscando propiedad" a alguien que
            # ya fue marcado como caso especial (agente/broker de otra
            # inmobiliaria, reclamo de propietario, bolsa de trabajo). Un
            # "¿quieres ver opciones frescas?" suena fuera de lugar y poco
            # profesional para un colega broker o un reclamo en curso.
            if estado.startswith("No-Molestar"):
                continue

            # MOMENTO 1: 24h sin respuesta tras una busqueda activa
            if (24 <= horas_sin_contacto < 48
                    and busqueda
                    and estado not in ("Seguimiento-24h",)):
                # Evita duplicar la zona si ya viene mencionada dentro del
                # texto de búsqueda guardado (p.ej. "Casa en Coto Encino,
                # Valle Imperial, Zapopan — $18,000/mes" ya incluye la zona).
                zona_ya_incluida = bool(zona) and zona.lower() in busqueda.lower()
                sufijo_zona = f" en {zona}" if (zona and not zona_ya_incluida) else ""
                msg = (
                    f"Hola {nombre}! Soy MAX de Acierta Max. "
                    f"Quedé pensando en tu busqueda de {busqueda or 'propiedad'}"
                    f"{sufijo_zona}. "
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
                    f"Tenemos miles de propiedades — seguro encontramos la ideal!"
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
        try:
            _crm_revisar_seguimientos()
        except Exception as e:
            # Aislado a propósito: un error en el CRM de vendedores nunca
            # debe tumbar el seguimiento a clientes, y viceversa.
            print(f"[MAX-CRM] Error en revision de seguimientos CRM: {e}", flush=True)
        try:
            # Reporte de los lunes: se manda una sola vez por semana, en la
            # primera revisión de la hora 8-9am hora GDL de cada lunes.
            # El marcador vive en memoria bajo una clave fija (no es un
            # teléfono real) para que sobreviva un redeploy del servidor.
            _fecha_hoy, _hora_hoy = hora_gdl().split(" ")
            _dia_semana = datetime.date.fromisoformat(_fecha_hoy).weekday()  # 0 = lunes
            if _dia_semana == 0 and 8 <= int(_hora_hoy.split(":")[0]) < 9:
                _ultimo = memoria_leer("SISTEMA_REPORTES").get("ULTIMO_LUNES_ENVIADO", "")
                if _ultimo != _fecha_hoy:
                    _enviar_reportes_lunes()
                    memoria_guardar("SISTEMA_REPORTES", ULTIMO_LUNES_ENVIADO=_fecha_hoy)
        except Exception as e:
            print(f"[MAX-LUNES] Error en reporte de los lunes: {e}", flush=True)
        try:
            _betty_revisar_seguimientos()
        except Exception as e:
            print(f"[MAX-CRM] Error en revision de seguimientos de Betty: {e}", flush=True)

def _loop_seguridad_visitas():
    """Thread aparte, cada 15 min -- la seguridad personal de un vendedor
    en campo no puede esperar el ciclo de 1 hora del resto del CRM."""
    while True:
        time.sleep(15 * 60)
        try:
            _crm_revisar_seguridad_visitas()
        except Exception as e:
            print(f"[MAX-SEGURIDAD] Error en revision de seguridad: {e}", flush=True)

# Arrancar el thread proactivo al iniciar
_thread_proactivo = threading.Thread(target=_loop_proactivo, daemon=True)
_thread_proactivo.start()
_thread_seguridad = threading.Thread(target=_loop_seguridad_visitas, daemon=True)
_thread_seguridad.start()
print("[MAX-PRO] Thread proactivo iniciado (revisa cada hora) + thread de seguridad (cada 15 min)", flush=True)

# ------------------------------------------------------------------
# LINEA DE ENGANCHE PROACTIVA (se agrega automaticamente a fichas de VENTA)
# ------------------------------------------------------------------
def linea_enganche(precio, operacion):
    """Genera una linea breve de enganche estimado (10%) para propiedades
    en VENTA con precio valido. Se agrega automaticamente al final de las
    fichas para abrir la conversacion de credito de forma proactiva, en
    vez de esperar a que el cliente pregunte. Vacio para RENTA o precio
    invalido -- nunca inventa un numero sin base real.
    Acepta tanto 'VENTA' (bolsa ZMG) como 'sale' (EasyBroker crudo)."""
    op_l = (operacion or "").strip().lower()
    if op_l not in ("venta", "sale"):
        return ""
    m = re.search(r'[\d,]+\.?\d*', str(precio or ""))
    if not m:
        return ""
    try:
        precio_val = float(m.group(0).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    if precio_val <= 0:
        return ""
    enganche = precio_val * 0.10
    return (f"\n\n💳 Con crédito bancario, el enganche estimado sería desde "
            f"${enganche:,.0f} MXN (10% aprox., puede variar según banco). "
            f"¿Te gustaría que veamos juntos si te alcanza?")

# ------------------------------------------------------------------
# PRECALIFICACION CREDITICIA
# ------------------------------------------------------------------
def calcular_costos_operacion(operacion, renta_mensual=None, precio_venta=None):
    """Presupuesto aproximado de gastos para formalizar la operación.
    RENTA: usa las cifras fijas de Acierta Max (oficina 161 del IJA para
    Justicia Alternativa) -- estos SÍ son números reales de la empresa,
    no una estimación de mercado.
    VENTA: da un RANGO aproximado de gastos notariales (varía por
    notaría y municipio) -- esto es una referencia general, no una
    cotización real de ninguna notaría en particular."""
    op = (operacion or "").lower()

    if op == "renta":
        renta = float(renta_mensual or 0)
        deposito = renta
        anticipado = renta
        investigacion = 1500
        justicia_alternativa = 2000
        total = deposito + anticipado + investigacion + justicia_alternativa
        return {
            "operacion": "renta",
            "desglose": {
                "Depósito en garantía (1 mes)": deposito,
                "Renta anticipada (1 mes)": anticipado,
                "Investigación (aval/inquilino)": investigacion,
                "Justicia Alternativa (IJA, oficina 161)": justicia_alternativa,
            },
            "total_aproximado": total,
            "documentos_necesarios": [
                "Identificación oficial con foto (INE)",
                "Datos completos: nombre, teléfono y correo electrónico",
                "Al menos 5 referencias personales (nombre y teléfono)",
                "Comprobante de ingresos de donde obtiene sus recursos (últimos 2 meses, con vigencia no mayor a 3 meses)",
                "Autorización de estudio en Buró de Crédito",
                "Datos del obligado solidario (aval): nombre completo, teléfono y correo",
                "Copia del predial de la propiedad del obligado solidario",
                "Pago de la investigación",
            ],
            "nota": ("Este presupuesto usa las cifras fijas de Acierta Max (investigación y "
                    "Justicia Alternativa ante la oficina 161 del IJA). El depósito y la renta "
                    "anticipada son sobre la renta mensual que me diste -- confírmalo con el "
                    "asesor antes de comprometerte, puede variar según el propietario."),
        }

    elif op == "venta":
        precio = float(precio_venta or 0)
        pct_min, pct_max = 0.04, 0.08  # rango típico de gastos notariales en México
        return {
            "operacion": "venta",
            "rango_gastos_aproximado": [round(precio * pct_min), round(precio * pct_max)],
            "porcentaje_referencia": "4% a 8% del valor de la propiedad (varía por notaría, municipio y si hay crédito hipotecario de por medio)",
            "incluye_tipicamente": [
                "Honorarios del notario",
                "ISAI (Impuesto Sobre Adquisición de Inmuebles)",
                "Derechos de Registro Público de la Propiedad",
                "Avalúo",
                "Certificados (libertad de gravamen, no adeudo de predial y agua)",
            ],
            "documentos_necesarios": (
                "Betty (responsable de crédito) le solicita al comprador la lista exacta de "
                "documentos, porque varía según el banco o institución que elija (bancario, "
                "Infonavit, Cofinavit, contado). No hay una lista única que MAX pueda dar de "
                "antemano para este caso."
            ),
            "nota": ("Este es un rango de REFERENCIA GENERAL, no una cotización real de ninguna "
                    "notaría específica -- el costo exacto lo confirma la notaría que elijan al "
                    "momento de la operación, y puede variar según si hay crédito bancario, "
                    "Infonavit, o es de contado."),
        }
    else:
        return {"error": "operacion debe ser 'renta' o 'venta'"}


def precalificar_credito(phone, ingreso_mensual, precio_objetivo,
                          tiene_imss=False, tiene_infonavit=False,
                          saldo_infonavit=0, enganche_disponible=0,
                          es_conyugal=False, historial_crediticio=""):
    """Calcula capacidad de credito hipotecario y orienta al prospecto.
    ESTO ES UNA SIMULACION con datos autoreportados por el cliente -- NO
    es una consulta real al Buro de Credito (eso requiere que Acierta Max
    este dado de alta como empresa afiliada ante Buro de Credito, con
    autorizacion firmada del cliente para cada consulta -- fuera del
    alcance de MAX). historial_crediticio es autoreportado por el cliente
    ("bueno"/"regular"/"malo"/"no se") y solo ajusta la estimacion --
    nunca reemplaza una consulta real."""
    ingreso = float(ingreso_mensual or 0)
    precio  = float(precio_objetivo or 0)
    enganche = float(enganche_disponible or 0)
    saldo_info = float(saldo_infonavit or 0)

    # Regla bancaria: mensualidad max = 30% del ingreso neto
    mensualidad_max = ingreso * 0.30
    # Con tasa promedio 10.75% a 20 anos, factor de pago ~$10.10 por cada $1,000
    FACTOR_PAGO = 10.10 / 1000  # mensualidad por peso de credito

    # Ajuste cualitativo por historial autoreportado -- SOLO afecta el
    # credito bancario (Infonavit tiene sus propias reglas de puntaje,
    # no depende del buro tradicional de la misma forma).
    hist = (historial_crediticio or "").lower()
    if hist in ("malo", "atrasos", "moroso", "mal historial"):
        factor_ajuste_banco = 0.60  # castiga fuerte la capacidad estimada
        nota_historial = ("Mencionaste antecedentes de atrasos -- esto puede reducir bastante tu "
                          "capacidad real o la tasa que te ofrezcan. La cifra de banco de abajo "
                          "ya viene reducida por precaución, pero SOLO el Buró de Crédito real, "
                          "consultado por el banco, da el número exacto.")
    elif hist in ("regular", "mas o menos", "más o menos"):
        factor_ajuste_banco = 0.85
        nota_historial = ("Con historial 'regular' la cifra de banco de abajo ya viene algo "
                          "reducida por precaución -- el banco puede ofrecerte más o menos, "
                          "según lo que su consulta real al buró arroje.")
    else:
        factor_ajuste_banco = 1.0
        nota_historial = ""
    credito_banco_max = (mensualidad_max / FACTOR_PAGO if mensualidad_max > 0 else 0) * factor_ajuste_banco

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
        "nota_historial_crediticio": nota_historial,
        "recomendacion": (
            "Con tu perfil, esta propiedad es viable. Te recomiendo cotizar en Condusef (condusef.gob.mx) para comparar bancos y elegir la mejor tasa. Un asesor de Acierta Max puede acompanarte en el proceso."
            if viable else
            f"Con tu perfil actual la propiedad de ${precio:,.0f} tiene una brecha de ${max(brecha,0):,.0f}. Te puedo mostrar opciones en tu rango real o explorar como ampliar tu capacidad (segundo titular, mayor enganche, o plazo mas largo)."
        ),
        "disclaimer_obligatorio": (
            "Esto es una SIMULACION orientativa con los datos que tú me diste (ingreso, si tienes "
            "Infonavit/IMSS, y tu propio historial crediticio si lo mencionaste) -- NO es una "
            "precalificación oficial de ningún banco ni una consulta real a tu Buró de Crédito. "
            "El número final que un banco te apruebe depende de su propia consulta al buró y sus "
            "políticas internas, que pueden dar un resultado distinto al de esta simulación."
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
def _buscar_eb_en_payload(obj, _profundidad=0):
    """Busca un código EB-XXXXXX en CUALQUIER parte del payload de Wati,
    sin importar el nombre del campo. Esto cubre el caso de mensajes
    citados/reply (el cliente responde a un anuncio anterior): Wati puede
    mandar el texto original citado bajo un campo distinto a 'text' (el
    nombre exacto no está documentado de forma confiable y puede variar),
    así que en vez de adivinarlo, se recorre todo el JSON recibido."""
    if _profundidad > 6:
        return None
    if isinstance(obj, str):
        m = re.search(r'\b(?:EB|NJ)-[A-Z0-9]{4,8}\b', obj.upper())
        return m.group(0) if m else None
    if isinstance(obj, dict):
        for v in obj.values():
            r = _buscar_eb_en_payload(v, _profundidad + 1)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = _buscar_eb_en_payload(v, _profundidad + 1)
            if r:
                return r
    return None

_MENSAJES_PROCESADOS = {}  # whatsappMessageId -> timestamp
_MENSAJES_PROCESADOS_LOCK = threading.Lock()

def _es_mensaje_duplicado(msg_id):
    """Wati/Meta puede reintentar el mismo webhook si el procesamiento
    tarda (ej. el fast-path de VERIFICA hace varias llamadas seguidas:
    2 mensajes + notificación + guardar en Sheets). Sin esto, un reintento
    ejecuta TODO el flujo de nuevo -- se vio en producción: el saludo de
    VERIFICA le llegó dos veces al mismo cliente."""
    if not msg_id:
        return False
    ahora = time.time()
    with _MENSAJES_PROCESADOS_LOCK:
        for k in [k for k, t in _MENSAJES_PROCESADOS.items() if ahora - t > 600]:
            del _MENSAJES_PROCESADOS[k]
        if msg_id in _MENSAJES_PROCESADOS:
            return True
        _MENSAJES_PROCESADOS[msg_id] = ahora
        return False


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    _msg_id = data.get("id") or data.get("whatsappMessageId")
    if _msg_id and _es_mensaje_duplicado(_msg_id):
        print(f"[MAX] Mensaje duplicado ignorado (reintento de webhook): {_msg_id}", flush=True)
        return jsonify(ok=True, duplicado=True)
    # Nombre de perfil de WhatsApp que manda Wati (puede ser nombre real o
    # nombre generico de negocio/rol) -- se usa mas abajo como PISTA para
    # agent_reply, nunca como verdad absoluta.
    sender_name = (data.get("senderName") or "").strip()
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
    if text and _camino_verificacion_entrante(phone, text):
        return jsonify(ok=True, verificacion=True)
    if text and _pulso_entrante(phone, text, data.get("senderName") or ""):
        return jsonify(ok=True, pulso=True)
    if not text:
        tipo_msg = (data.get("type") or "").lower()
        print(f"[MAX-DIAGNOSTICO-MEDIA] Mensaje sin texto de {phone}, "
              f"type={tipo_msg!r}, claves={list(data.keys())}", flush=True)
        # Antes de rendirnos: puede ser un REPLY a un anuncio anterior, donde
        # Wati manda el código EB citado en algún campo distinto a 'text'
        # (nombre variable, no documentado con certeza). Buscamos en TODO
        # el payload en vez de adivinar el nombre exacto del campo.
        _eb_en_payload = _buscar_eb_en_payload(data)
        if _eb_en_payload:
            print(f"[MAX] Código EB encontrado fuera de 'text' (probable mensaje "
                  f"citado/reply a un anuncio): {_eb_en_payload} — de {phone}", flush=True)
            text = _eb_en_payload
        else:
            # Si parece ser una imagen/audio/video/documento real (no un evento
            # de estado/entrega), NO respondas con un mensaje fijo que ignora
            # el contexto -- puede ser justo la identificación que pediste
            # para agendar una visita. Deja que el modelo decida, con una
            # nota describiendo qué llegó (no puedes ver el contenido real
            # de la imagen, pero el modelo sí sabe si la estaba esperando).
            if tipo_msg == "location":
                # Puede venir en varias formas segun el proveedor -- se
                # busca de forma defensiva en vez de asumir un solo campo.
                loc = data.get("location") or {}
                lat = data.get("latitude") or loc.get("latitude")
                lon = data.get("longitude") or loc.get("longitude")
                _tel_vendedores_loc = {_normalizar_phone_wati(v["phone"]) for v in VENDEDORES}
                if _normalizar_phone_wati(phone) in _tel_vendedores_loc and lat and lon:
                    registrada = False
                    try:
                        registrada = crm_registrar_ubicacion(phone, lat, lon)
                    except Exception as e:
                        print(f"[MAX-SEGURIDAD] Error registrando ubicación de {phone}: {e}", flush=True)
                    if registrada:
                        wati_send_text(phone, "📍 Ubicación recibida, todo en orden ✅")
                    else:
                        print(f"[MAX-SEGURIDAD] Ubicación de {phone} sin visita activa registrada -- ignorada", flush=True)
                return jsonify(ok=True)
            if tipo_msg in ("image", "photo"):
                # Se guarda en Drive de una vez (probablemente sea la
                # identificación pedida). Como el folio del CRM AIDA puede
                # no existir todavía en este punto, se guarda temporal y
                # crm_crear_registro la reubica a la carpeta definitiva.
                _filename_media = data.get("fileName") or data.get("filename") or data.get("data")
                if _filename_media:
                    try:
                        _res_doc = guardar_documento_cliente(
                            phone, _filename_media,
                            f"identificacion_{hora_gdl().replace(':','-').replace(' ','_')}.jpg")
                        memoria_guardar(phone, ULTIMO_DOCUMENTO_URL=_res_doc["file_url"])
                        print(f"[MAX-EXPEDIENTE] Documento de {phone} guardado: {_res_doc['file_url']}", flush=True)
                    except Exception as e:
                        print(f"[MAX-EXPEDIENTE] Error guardando documento de {phone}: {e}", flush=True)
                text = ("[El cliente envió una foto/imagen aquí en el chat. No puedes ver "
                        "su contenido, pero el sistema ya la guardó en su expediente digital. "
                        "Si en tu mensaje anterior le pediste su identificación "
                        "oficial para agendar una visita, trata esta imagen como recibida y "
                        "continúa el proceso (llama iniciar_recorrido_crm si ya tienes nombre "
                        "+ los números de propiedad). Si no le habías pedido nada, pregúntale "
                        "con amabilidad qué es o para qué te la comparte.]")
            elif tipo_msg == "document":
                # Probablemente sea el PDF de su Buró de Crédito (el
                # cliente lo baja él mismo de burodecredito.com.mx) u
                # otro documento -- se guarda en su expediente igual que
                # la identificación, pero sin que MAX interprete su
                # contenido ni dé un veredicto sobre el historial.
                _filename_doc = data.get("fileName") or data.get("filename") or data.get("data")
                if _filename_doc:
                    try:
                        _res_doc2 = guardar_documento_cliente(
                            phone, _filename_doc,
                            f"documento_{hora_gdl().replace(':','-').replace(' ','_')}.pdf",
                            mimetype="application/pdf")
                        memoria_guardar(phone, ULTIMO_DOCUMENTO_URL=_res_doc2["file_url"])
                        print(f"[MAX-EXPEDIENTE] Documento (PDF) de {phone} guardado: {_res_doc2['file_url']}", flush=True)
                    except Exception as e:
                        print(f"[MAX-EXPEDIENTE] Error guardando documento (PDF) de {phone}: {e}", flush=True)
                text = ("[El cliente envió un documento (probablemente PDF) aquí en el chat. "
                        "No puedes ver su contenido, pero el sistema ya lo guardó en su expediente "
                        "digital. Si le pediste su Buró de Crédito o algún otro documento, agradécele "
                        "y dile que quedó guardado para que el asesor/Betty lo revisen -- NUNCA des "
                        "un veredicto sobre su historial crediticio, tú no puedes leerlo. Si no le "
                        "habías pedido nada, pregúntale con amabilidad qué es.]")
            elif tipo_msg in ("video", "audio", "sticker"):
                wati_send_text(phone,
                    "¡Hola! 👋 Veo que me compartiste algo, pero hoy no "
                    "puedo leerlo directamente 🙏. ¿Me escribes el nombre de la propiedad, "
                    "el código que empieza con EB-, o la liga del anuncio que viste? "
                    "Así te ayudo al instante con la ficha oficial.")
                return jsonify(ok=True)
            else:
                return jsonify(ok=True)
    # DIAGNÓSTICO TEMPORAL: ver qué campos manda Wati en el payload real,
    # para saber si trae source_url/source_id (el origen del anuncio de
    # Instagram) que hoy no estamos usando. Quitar una vez confirmado.
    print(f"[MAX-DIAGNOSTICO] Claves del payload de {phone}: {list(data.keys())}", flush=True)
    if any(k for k in data.keys() if "source" in k.lower()):
        print(f"[MAX-DIAGNOSTICO] Campos de origen encontrados: "
              f"{ {k: v for k, v in data.items() if 'source' in k.lower()} }", flush=True)

    # FAST-PATH ACIERTA VERIFICA: si el mensaje es exactamente (o casi
    # exactamente) la palabra "VERIFICA" -- el CTA de la campaña de
    # octubre le pide al cliente escribir justo eso -- se responde
    # directo, sin pasar por el modelo. Se exige que sea la palabra sola
    # (o con algo de saludo alrededor, máximo 3 palabras) para no
    # disparar con frases normales como "¿me puedes verificar si...".
    _texto_normalizado = re.sub(r"[^\wáéíóúñ]", " ", (text or "")).strip().lower()
    _palabras_verifica = _texto_normalizado.split()
    if (len(_palabras_verifica) <= 3 and "verifica" in _palabras_verifica):
        print(f"[MAX-VERIFICA] Palabra clave detectada de {phone}", flush=True)
        wati_send_text(phone,
            "¡Hola! 👋 Gracias por tu interés en *ACIERTA VERIFICA* — revisión física básica "
            "y análisis documental preventivo antes de comprar, rentar o entregar una propiedad. "
            "\"Antes de firmar, verifica.\" 🔍")
        wati_send_text(phone, "1️⃣ ¿Cuál es tu nombre?")
        try:
            memoria_guardar(phone, VERIFICA_PREGUNTA_ACTUAL="1")
        except Exception:
            pass
        return jsonify(ok=True, ruta="acierta_verifica")

    _verifica_pregunta = memoria_leer(phone).get("VERIFICA_PREGUNTA_ACTUAL", "")
    if _verifica_pregunta == "1":
        memoria_guardar(phone, VERIFICA_NOMBRE=text.strip())
        wati_send_text(phone, "2️⃣ ¿Cuál es la dirección o zona de la propiedad a revisar?")
        memoria_guardar(phone, VERIFICA_PREGUNTA_ACTUAL="2")
        return jsonify(ok=True, ruta="acierta_verifica_p1")

    if _verifica_pregunta == "2":
        _m_verifica = memoria_leer(phone)
        _nombre_v = _m_verifica.get("VERIFICA_NOMBRE", "")
        _zona_v = text.strip()
        memoria_guardar(phone, VERIFICA_ZONA=_zona_v, VERIFICA_PREGUNTA_ACTUAL="")
        wati_send_text(phone,
            "¡Gracias! 🙌 Un coordinador te contacta en breve para cotizar según los m² a "
            "revisar (desde $45/m², mínimo $3,500 MXN en la ZMG) y agendar tu visita.")
        # VERIFICA es solo a Javier -- NO entra a la rotación de vendedores
        # de bienes raíces (a diferencia de los leads normales de Acierta Max).
        if JAVIER_PERSONAL:
            notificar_interno(
                JAVIER_PERSONAL,
                f"🔍 NUEVO CONTACTO ACIERTA VERIFICA\n\n"
                f"Nombre: {_nombre_v}\nTeléfono: {phone}\nZona/dirección: {_zona_v}",
                resumen_para_plantilla=(f"VERIFICA: {_nombre_v} | Tel: {phone} | "
                    f"Zona: {_zona_v}"),
                template_name="notificacion_lead")
        return jsonify(ok=True, ruta="acierta_verifica_datos")

    # FAST-PATH IKONO ALTA DIRECCIÓN: mismo mecanismo que VERIFICA --
    # palabra clave sola (o casi sola) en el mensaje. Arranca un
    # cuestionario de 4 preguntas, una por turno, guardado en memoria.
    if (len(_palabras_verifica) <= 3 and "ikono" in _texto_normalizado.split()):
        print(f"[MAX-IKONO] Palabra clave detectada de {phone}", flush=True)
        wati_send_text(phone,
            "¡Hola! 👋 Gracias por tu interés en *IKONO Alta Dirección* — coaching, "
            "consultoría, mentoría y conferencias para alta dirección. "
            "Antes de agendar tu llamada con Javier Mendoza, te hago 4 preguntas rápidas 📋")
        wati_send_text(phone, "1️⃣ ¿Cuál es tu nombre completo?")
        try:
            memoria_guardar(phone, IKONO_PREGUNTA_ACTUAL="1")
        except Exception:
            pass
        return jsonify(ok=True, ruta="ikono_inicio")

    _ikono_pregunta = memoria_leer(phone).get("IKONO_PREGUNTA_ACTUAL", "")
    if _ikono_pregunta == "1":
        memoria_guardar(phone, IKONO_NOMBRE=text.strip())
        wati_send_text(phone,
            f"2️⃣ Para confirmar, ¿tu teléfono de contacto es este mismo ({phone}), "
            f"o prefieres que usemos otro?")
        memoria_guardar(phone, IKONO_PREGUNTA_ACTUAL="2")
        return jsonify(ok=True, ruta="ikono_p1")

    if _ikono_pregunta == "2":
        _tel_confirmado = phone if re.search(r"mismo|si|s[ií]|este", text.lower()) and not re.search(r"\d{7,}", text) else re.sub(r"[^\d]", "", text) or phone
        memoria_guardar(phone, IKONO_TELEFONO=_tel_confirmado)
        wati_send_text(phone, "3️⃣ ¿De qué industria o tipo de negocio eres?")
        memoria_guardar(phone, IKONO_PREGUNTA_ACTUAL="3")
        return jsonify(ok=True, ruta="ikono_p2")

    if _ikono_pregunta == "3":
        memoria_guardar(phone, IKONO_INDUSTRIA=text.strip())
        wati_send_text(phone,
            "4️⃣ Por último, cuéntame brevemente: ¿cuál es la situación actual por la que "
            "quieres contactar a IKONO Alta Dirección?")
        memoria_guardar(phone, IKONO_PREGUNTA_ACTUAL="4")
        return jsonify(ok=True, ruta="ikono_p3")

    if _ikono_pregunta == "4":
        m_ikono = memoria_leer(phone)
        _nombre = m_ikono.get("IKONO_NOMBRE", "")
        _tel = m_ikono.get("IKONO_TELEFONO", phone)
        _industria = m_ikono.get("IKONO_INDUSTRIA", "")
        _situacion = text.strip()
        memoria_guardar(phone, IKONO_SITUACION=_situacion, IKONO_PREGUNTA_ACTUAL="",
                        IKONO_CLIENTE="Si")
        wati_send_text(phone,
            f"¡Gracias, {_nombre.split()[0] if _nombre else ''}! 🙌 Estoy agendando tu llamada "
            f"de 20 minutos con Javier -- te confirmo el horario en un momento.")

        def _finalizar_ikono(_phone=phone, _nombre=_nombre, _tel=_tel,
                             _industria=_industria, _situacion=_situacion):
            fecha_texto, agendado, link_o_error = _proximo_slot_ikono_y_agenda(_nombre, _tel, _situacion)
            if agendado:
                wati_send_text(_phone,
                    f"📅 ¡Listo! Tu llamada con Javier Mendoza quedó agendada para el "
                    f"*{fecha_texto}* (hora Guadalajara). Te esperamos 🙌")
            else:
                wati_send_text(_phone,
                    "Ya tenemos tus datos -- Javier te confirma personalmente el horario "
                    "de tu llamada en breve. 🙌")
            resumen_investigacion = investigar_prospecto_ikono(_nombre, _industria, _tel)
            notificar_interno(
                JAVIER_PERSONAL,
                f"🎯 NUEVO PROSPECTO IKONO ALTA DIRECCIÓN\n\n"
                f"Nombre: {_nombre}\nTeléfono: {_tel}\nIndustria: {_industria}\n"
                f"Situación: {_situacion}\n\n"
                f"📅 Llamada: {fecha_texto if agendado else 'PENDIENTE DE AGENDAR - ' + str(link_o_error)}\n\n"
                f"🔎 Investigación:\n{resumen_investigacion}",
                resumen_para_plantilla=(f"IKONO: {_nombre} | Tel: {_tel} | "
                    f"Industria: {_industria} | Llamada: {fecha_texto if agendado else 'pendiente'}"),
                template_name="notificacion_lead")

        threading.Thread(target=_finalizar_ikono, daemon=True).start()
        return jsonify(ok=True, ruta="ikono_completo")

    # CUALQUIER MENSAJE POSTERIOR de un cliente ya marcado como de IKONO
    # (cuestionario ya completado) se queda en modo IKONO para siempre --
    # NUNCA debe caer en el flujo general de bienes raíces. Esto corrigió
    # un bug real: un cliente de IKONO preguntó "¿puedo cambiar la hora?"
    # y MAX lo confundió con un cliente viejo de Acierta Max (hasta le
    # llamó por otro nombre y le mandó un link de Calendly ajeno al tema).
    if memoria_leer(phone).get("IKONO_CLIENTE") == "Si":
        print(f"[MAX-IKONO] Mensaje de seguimiento de cliente IKONO {phone}: {text}", flush=True)
        _nombre_ikono = memoria_leer(phone).get("IKONO_NOMBRE", "")
        wati_send_text(phone,
            f"Gracias{', ' + _nombre_ikono.split()[0] if _nombre_ikono else ''} 🙌 Ya le "
            f"avisé directamente a Javier sobre tu mensaje -- te confirma él mismo en breve.")
        notificar_interno(
            JAVIER_PERSONAL,
            f"🎯 SEGUIMIENTO IKONO — {_nombre_ikono or phone} ({phone}) escribió:\n\"{text}\"\n\n"
            f"Revisa/ajusta directamente en tu Google Calendar si es sobre la cita.",
            resumen_para_plantilla=(f"Seguimiento IKONO: {_nombre_ikono or phone} | "
                f"Mensaje: {text[:150]}"),
            template_name="seguimiento_lead")
        return jsonify(ok=True, ruta="ikono_seguimiento")

    # ENRUTAMIENTO AL CRM AIDA: si quien escribe es uno de los vendedores
    # Y tiene un expediente activo esperando su respuesta, esto NO pasa
    # por la conversación normal de MAX con clientes -- se procesa con un
    # parser determinístico aparte (crm_procesar_respuesta_vendedor),
    # porque mover el pipeline de ventas real no debe depender de que un
    # LLM interprete bien un "sí"/"no" suelto.
    _tel_vendedores = {_ultimos10(v["phone"]) for v in VENDEDORES}
    if _ultimos10(phone) in _tel_vendedores:
        try:
            if crm_procesar_respuesta_vendedor(phone, text):
                return jsonify(ok=True, ruta="crm_vendedor")
            if _reporte_lunes_manejar(phone, text):
                return jsonify(ok=True, ruta="reporte_lunes")
            if _reporte_dia_manejar(phone, text):
                return jsonify(ok=True, ruta="reporte_dia")
        except Exception as e:
            print(f"[MAX-CRM] Error procesando respuesta de vendedor {phone}: {e}", flush=True)
        # Si no había expediente pendiente para este número, sigue de largo
        # como conversación normal (un vendedor también puede escribirle a
        # MAX como cualquier otro usuario, ej. para probarlo).

    # Mismo tratamiento para Betty (responsable de crédito): sus respuestas
    # a los check-ins tampoco pasan por el modelo, van al parser aparte.
    if _normalizar_phone_wati(phone) == _normalizar_phone_wati(BETTY_PHONE):
        try:
            if betty_procesar_respuesta(text):
                return jsonify(ok=True, ruta="betty")
        except Exception as e:
            print(f"[MAX-CRM] Error procesando respuesta de Betty: {e}", flush=True)

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
            # ESPERA DE RÁFAGA: si el cliente sigue escribiendo (varios mensajes
            # seguidos, como "Compra" y luego su nombre por separado), le damos
            # unos segundos de margen ANTES de tomar el primer lote — así se
            # juntan en una sola respuesta en vez de generar una respuesta por
            # cada mensaje casi simultáneo. Solo se espera en la primera vuelta;
            # las siguientes iteraciones (mensajes que llegaron mientras se
            # generaba la respuesta anterior) se procesan de inmediato.
            time.sleep(3.5)
            while True:
                with CONV_LOCK:
                    pendientes = PENDING.get(phone, [])
                    if not pendientes:
                        break
                    texto = "\n".join(pendientes)
                    PENDING[phone] = []
                try:
                    print(f"[MAX] Mensaje de {phone}: {texto[:200]}", flush=True)
                    _reenviar_a_javier(phone, cliente=texto)
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
                    # FAST-PATH de campañas: se revisa en CUALQUIER mensaje de la
                    # conversación, no solo el primero. Antes solo se checaba el
                    # primer mensaje ("if campana and not historial"), lo que hacía
                    # que MAX "perdiera" el contexto del anuncio si el cliente
                    # mencionaba la propiedad/código en su 2do o 3er mensaje (ej.
                    # "¿me ayudas con la ubicación?" sobre un anuncio ya visible en
                    # el chat, o la palabra clave del anuncio como respuesta tardía
                    # a "¿cómo te llamas?"). La protección contra reenvío duplicado
                    # ya existe (FICHAS_ENVIADAS), así que es seguro revisar siempre.
                    nombre, campana = detectar_campana(texto)
                    # Respaldo: si el texto es genérico (botón default de
                    # Instagram) pero SÍ sabemos de qué publicación vino
                    # (ORIGEN_POR_TELEFONO), y esa publicación está mapeada
                    # a una campaña conocida, usarla igual. Ya NO se limita al
                    # primer mensaje: si el cliente pregunta algo vago despues
                    # ("me ayudas con la ubicacion?") sin repetir palabra clave,
                    # igual debemos reconocer que sigue hablando del mismo anuncio.
                    if not campana:
                        origen = ORIGEN_POR_TELEFONO.get(phone)
                        nombre_mapeado = obtener_mapeo_post_a_campana().get(origen) if origen else None
                        if nombre_mapeado and nombre_mapeado in CAMPANAS:
                            nombre, campana = nombre_mapeado, CAMPANAS[nombre_mapeado]
                            print(f"[MAX] Campaña detectada por origen de Instagram: {nombre}", flush=True)
                    campana_ya_enviada = nombre in FICHAS_ENVIADAS.get(phone, set()) if nombre else False
                    if campana and not campana_ya_enviada:
                        print(f"[MAX] Campaña detectada: {nombre} (historial previo: {bool(historial)})", flush=True)
                        CAMPANA_ACTIVA_POR_TELEFONO[phone] = nombre
                        responder_campana(phone, texto, campana)
                        continue
                    elif campana and campana_ya_enviada:
                        # Ya se mandó la ficha antes, pero SIGUE siendo la campaña
                        # activa de esta conversación -- que quede registrada para
                        # que Claude tenga el contexto en su respuesta normal.
                        CAMPANA_ACTIVA_POR_TELEFONO[phone] = nombre
                    # FAST-PATH de guías (AM-GUIA-XX): igual, en cualquier mensaje,
                    # con proteccion anti-duplicado via GUIAS_ENVIADAS.
                    nombre_g, guia = detectar_guia(texto)
                    guia_ya_enviada = nombre_g in GUIAS_ENVIADAS.get(phone, set()) if nombre_g else False
                    if guia and not guia_ya_enviada:
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
                        wati_send_to_vendedor(_v_ast["phone"],
                                f"*[SOLICITUD DE ASESOR — te toco]*\n"
                                f"Cliente: {phone}\n"
                                f"Escribio: {_texto_strip}\n\n"
                                f"*Contexto:*\n{_resumen[:600]}")
                        if _v_ast["phone"] != JAVIER_PHONE:
                            wati_send_to_vendedor(JAVIER_PHONE,
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
                    _eb_match = _re.search(r'\b(?:EB|NJ)-[A-Z0-9]{4,8}\b', texto.upper())
                    _eb_code = _eb_match.group(0) if _eb_match else None
                    if not _eb_code:
                        # Respaldo: el cliente escribió algo propio ("sí, quiero info")
                        # pero citó/respondió un anuncio anterior — el código EB puede
                        # venir solo en la parte citada del payload de Wati, no en 'text'.
                        _eb_payload = _buscar_eb_en_payload(data)
                        if _eb_payload:
                            print(f"[MAX] Código EB encontrado en payload citado (no en texto "
                                  f"escrito por el cliente): {_eb_payload} — de {phone}", flush=True)
                            _eb_code = _eb_payload
                    if _eb_code:
                        print(f"[MAX] Fast-path EB: {_eb_code} de {phone}", flush=True)
                        _prop = next((p for p in INVENTARIO_ZMG
                                      if (_eb_code.lower() in (p.get('codigo_eb') or '').lower()
                                          or _eb_code.lower() in (p.get('Liga') or '').lower())), None)
                        if _prop:
                            _liga  = _prop.get('Liga','')
                            _tit   = _prop.get('Título/Colonia') or 'Propiedad'
                            _precio = _prop.get('Precio','')
                            _rec   = _prop.get('Recámaras') or ''
                            _ban   = _prop.get('Baños') or ''
                            _m2    = _prop.get('m²') or ''
                            _muni  = _prop.get('Municipio','')
                            _op    = _prop.get('Operación') or ''
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
                                    f"Tenemos miles de propiedades en la ZMG. "
                                    f"Cual es tu nombre y que buscas? Te ayudo!")
                            else:
                                wati_send_text(phone,
                                    f"Tenemos miles de propiedades en la ZMG. "
                                    f"Si quieres ver mas opciones o tienes preguntas sobre creditos, "
                                    f"escrituracion o visitas, aqui estoy! Cual es tu nombre?")
                            append_history(phone, "user", texto)
                            append_history(phone, "assistant",
                                f"[Fast-path EB {_eb_code}] Mande saludo + ficha. {_op} {_precio_fmt} {_muni}.")
                            continue
                        else:
                            print(f"[MAX] Fast-path EB: {_eb_code} no en inventario, pasando a Claude", flush=True)
                    # FIN FAST-PATH EB
                    # FAST-PATH NOMBRE: detectar si el cliente acaba de dar su nombre
                    # y no esta registrado aun — registrar inmediatamente sin esperar a Claude
                    _hist_actual = get_history(phone)
                    _ya_registrado = phone in REGISTRADOS
                    # FIX DE RAIZ: antes, cualquier mensaje corto que empezara con
                    # mayuscula se trataba como nombre (con una lista negra de
                    # palabras comunes creciendo sin parar). Eso dejo pasar groserias
                    # de un cliente molesto ("Puto", "Chinga a tu madre") como si
                    # fueran su nombre, generando folios de lead falsos en el CRM.
                    # Ahora se exige ADEMAS que el ultimo mensaje de MAX haya
                    # preguntado el nombre de verdad -- ligado al contexto real de
                    # la conversacion, no a la forma superficial del texto.
                    _ultimo_msg_max = ""
                    for _m in reversed(_hist_actual):
                        if _m.get("role") == "assistant":
                            _c = _m.get("content")
                            _ultimo_msg_max = _c if isinstance(_c, str) else str(_c)
                            break
                    _pregunto_nombre = any(f in _ultimo_msg_max.lower() for f in
                        ["cómo te llamas", "como te llamas", "tu nombre", "cuál es tu nombre",
                         "cual es tu nombre", "me dices tu nombre", "me compartes tu nombre"])
                    _NO_SON_NOMBRES = {
                        "comprar","rentar","vender","comprar.","rentar.","vender.",
                        "renta","venta","si","sí","no","hola","gracias","ok","okay",
                        "claro","perfecto","bueno","bien","tal","vez","aja","ajam",
                        "casa","departamento","depa","terreno","cualquiera","cualquier",
                        "urgente","pronto","ya","hoy","mañana","comprando","rentando",
                    }
                    if (not _ya_registrado and _pregunto_nombre and len(_hist_actual) >= 2
                            and len(texto.strip().split()) <= 4  # mensaje corto = posible nombre
                            and not any(c in texto for c in ['?','http','EB-','$'])
                            and texto.strip()[0].isupper()  # empieza con mayuscula = nombre
                            and texto.strip().split()[0].lower() not in _NO_SON_NOMBRES):
                        _nombre_detectado = texto.strip().split()[0]
                        print(f"[MAX] Fast-path nombre detectado: {_nombre_detectado} de {phone}", flush=True)
                        threading.Thread(
                            target=lambda: registrar_lead(phone, nombre=_nombre_detectado,
                                interes="Primer contacto", operacion="", presupuesto="",
                                zona="", notas="Registro automatico por fast-path de nombre"),
                            daemon=True
                        ).start()
                    reply = agent_reply(phone, texto, sender_name=sender_name)
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

FICHA_ACCESS_TOKEN = os.environ.get("FICHA_ACCESS_TOKEN", "")

def obtener_ficha_completa(phone):
    """Junta en un solo dict TODO lo que el sistema sabe de un cliente,
    leyendo las distintas pestañas donde hoy vive repartida la
    información (Memoria, Leads, CRM AIDA, Referencias Betty). Esta es
    la fuente para la ficha maestra -- ver /ficha/<phone>."""
    phone_n = _normalizar_phone_wati(phone)
    ficha = {"telefono": phone_n, "perfil": {}, "lead": {}, "crm": [],
             "betty": [], "encontrado": False}

    m = memoria_leer(phone_n)
    if m:
        ficha["perfil"] = m
        ficha["encontrado"] = True

    try:
        libro, _ = _sheets_client()
        if libro:
            # Leads MAX
            try:
                sh = libro.worksheet("Leads MAX")
                valores = sh.get_all_values()
                if valores:
                    headers = valores[0]
                    for fila in valores[1:]:
                        d = dict(zip(headers, fila + [""] * (len(headers) - len(fila))))
                        if _normalizar_phone_wati(d.get("WHATSAPP", "")) == phone_n:
                            ficha["lead"] = d
                            ficha["encontrado"] = True
            except Exception:
                pass
            # CRM AIDA -- puede haber más de un expediente (varias rondas)
            try:
                sh = libro.worksheet(CRM_HOJA)
                valores = sh.get_all_values()
                if valores:
                    headers = valores[0]
                    for fila in valores[1:]:
                        d = dict(zip(headers, fila + [""] * (len(headers) - len(fila))))
                        if _normalizar_phone_wati(d.get("TELEFONO_CLIENTE", "")) == phone_n:
                            ficha["crm"].append(d)
                            ficha["encontrado"] = True
            except Exception:
                pass
            # Referencias Betty
            try:
                sh = libro.worksheet(BETTY_HOJA)
                valores = sh.get_all_values()
                if valores:
                    headers = valores[0]
                    for fila in valores[1:]:
                        d = dict(zip(headers, fila + [""] * (len(headers) - len(fila))))
                        if _normalizar_phone_wati(d.get("TELEFONO_CLIENTE", "")) == phone_n:
                            ficha["betty"].append(d)
                            ficha["encontrado"] = True
            except Exception:
                pass
    except Exception as e:
        print(f"[MAX-FICHA] Error consolidando ficha de {phone_n}: {e}", flush=True)

    return ficha


def _ficha_html(ficha):
    """Renderiza la ficha consolidada como una página simple y legible."""
    p = ficha["perfil"]
    l = ficha["lead"]

    def fila(etiqueta, valor):
        if not valor:
            return ""
        return f"<tr><td class='et'>{etiqueta}</td><td>{valor}</td></tr>"

    html_crm = ""
    for c in ficha["crm"]:
        html_crm += f"""
        <div class="tarjeta">
          <h3>📋 {c.get('FOLIO','')} — Fase: {c.get('FASE','')}</h3>
          <table>
            {fila("Vendedor asignado", c.get("VENDEDOR"))}
            {fila("Propiedades", c.get("PROPIEDADES","").replace("|", " — ").replace(";", "<br>"))}
            {fila("Contactó cliente", c.get("CONTACTO_CLIENTE"))}
            {fila("Contactó originador", c.get("CONTACTO_ORIGINADOR"))}
            {fila("Fecha/hora de cita", c.get("FECHA_HORA_CITA"))}
            {fila("Resultado de visita", c.get("VISITA_RESULTADO"))}
            {fila("Documentación", c.get("DOCUMENTACION"))}
            {fila("Visita activa (monitoreo)", c.get("VISITA_ACTIVA"))}
            {fila("Concluido", c.get("CONCLUIDO"))}
            {fila("Creado", c.get("CREADO"))}
            {fila("Última acción", c.get("ULTIMA_ACCION"))}
          </table>
          {f'<p><a href="{c.get("CARPETA_CLIENTE_URL")}" target="_blank">📁 Ver expediente digital (identificación, documentos)</a></p>' if c.get("CARPETA_CLIENTE_URL") else ""}
        </div>"""

    html_betty = ""
    for b in ficha["betty"]:
        html_betty += f"""
        <div class="tarjeta">
          <h3>🏦 {b.get('FOLIO','')} — Referencia a Betty</h3>
          <table>
            {fila("Necesidad", b.get("NECESIDAD"))}
            {fila("Betty ya contactó", b.get("CONTACTO_BETTY"))}
            {fila("Creado", b.get("CREADO"))}
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ficha — {p.get('NOMBRE') or l.get('NOMBRE') or ficha['telefono']}</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#f4f5f7; margin:0; padding:24px; color:#1a1a1a; }}
  .contenedor {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 22px; }}
  h3 {{ margin: 0 0 8px; font-size: 16px; }}
  .tarjeta {{ background:white; border-radius:10px; padding:16px 20px; margin-bottom:16px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  table {{ width:100%; border-collapse: collapse; font-size: 14px; }}
  td {{ padding: 4px 8px; vertical-align: top; border-bottom: 1px solid #eee; }}
  td.et {{ color:#666; width: 40%; font-weight: 600; }}
  .score {{ display:inline-block; background:#1a7f37; color:white; border-radius:6px; padding:2px 10px; font-weight:700; }}
  a {{ color: #1a56db; }}
</style></head>
<body><div class="contenedor">
  <h1>👤 {p.get('NOMBRE') or l.get('NOMBRE') or 'Sin nombre'} <small style="color:#888;">({ficha['telefono']})</small></h1>
  <div class="tarjeta">
    <h3>Perfil general</h3>
    <table>
      {fila("Score de lead", f'<span class="score">{l.get("SCORE_LEAD (0-100)","")}</span>' if l.get("SCORE_LEAD (0-100)") else "")}
      {fila("Calificación de perfil", f'{l.get("CALIFICACION_PERFIL (1-5)")}/5' if l.get("CALIFICACION_PERFIL (1-5)") else "")}
      {fila("Vendedor asignado", l.get("VENDEDOR_ASIGNADO"))}
      {fila("Folio", l.get("FOLIO"))}
      {fila("Operación", p.get("OPERACION") or l.get("OPERACIÓN"))}
      {fila("Presupuesto", p.get("PRESUPUESTO") or l.get("PRESUPUESTO"))}
      {fila("Zona", p.get("ZONA") or l.get("ZONA"))}
      {fila("Última búsqueda", p.get("ULTIMA_BUSQUEDA") or l.get("INTERÉS"))}
      {fila("Notas", p.get("NOTAS_COACHING") or l.get("NOTAS"))}
      {fila("Estado", p.get("ESTADO") or l.get("ESTATUS"))}
      {fila("Última interacción", p.get("ULTIMA_INTERACCION"))}
    </table>
  </div>
  {html_crm}
  {html_betty}
  {(lambda chat: f'''<div class="tarjeta"><details><summary style="cursor:pointer; font-weight:600;">💬 Ver chat completo guardado</summary>
    <pre style="white-space:pre-wrap; font-size:13px; margin-top:10px;">{chat}</pre></details></div>''' if chat else "")(
      (ficha["crm"][-1].get("CHAT_COMPLETO") if ficha["crm"] else "") or l.get("CHAT_COMPLETO", ""))}
  {"<p style='color:#888;'>No se encontró información de este número.</p>" if not ficha["encontrado"] else ""}
</div></body></html>"""


@app.route("/ficha/<phone>", methods=["GET"])
def ver_ficha(phone):
    """Ficha maestra: junta en una sola página todo lo que el sistema
    sabe de un cliente (perfil, lead, CRM AIDA, referencias a Betty).
    Protegida con un token simple en el query string."""
    if FICHA_ACCESS_TOKEN and request.args.get("token") != FICHA_ACCESS_TOKEN:
        return "No autorizado", 401
    ficha = obtener_ficha_completa(phone)
    return _ficha_html(ficha)


# ------------------------------------------------------------------
# CAMINO WEB — alta de leads desde acierta.pro/camino.html
# El cliente da su nombre, WhatsApp y (opcional) correo con consentimiento
# explícito. Se guarda en "Leads MAX", se asigna vendedor por rotación
# (mismo mecanismo que WhatsApp) y se avisa al vendedor y a Javier.
# Es un endpoint PÚBLICO: valida todo, limita la frecuencia y solo acepta
# valores de listas cerradas (excepto nombre/zona/notas, que se limpian).
# ------------------------------------------------------------------
import hmac
import hashlib

CAMINO_ORIGENES = {"https://acierta.pro", "https://www.acierta.pro", "https://inmobiliaria.pro", "https://www.inmobiliaria.pro",
                   "https://verifica.casa", "https://www.verifica.casa"}
_CAMINO_HITS = {}
_CAMINO_LOCK = threading.Lock()
_CAMINO_OP = {"compra": "compra", "renta": "renta", "vender": "captación"}
_CAMINO_TIPO = {"casa": "Casa", "departamento": "Departamento", "terreno": "Terreno",
                "local": "Local comercial", "oficina": "Oficina", "bodega": "Bodega/nave",
                "otro": "Otro", "nose": "Aún no lo sabe"}
_CAMINO_USO = {"vivir": "para vivir", "invertir": "para invertir", "negocio": "para negocio"}
_CAMINO_CUANDO = {"ya": "lo antes posible (menos de 30 días)", "1a3": "en 1 a 3 meses",
                  "mas3": "en más de 3 meses / explorando"}
_CAMINO_MUNI = {"Guadalajara", "Zapopan", "Tlajomulco de Zúñiga", "Tlaquepaque", "Tonalá", "cualquiera"}
_CAMINO_CREDITO = {"banco": "crédito bancario", "infonavit": "Infonavit",
                   "cofinavit": "Cofinavit (Infonavit + banco)", "contado": "de contado", "nose": "aún no lo sabe"}


def _camino_resp(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    origen = request.headers.get("Origin", "")
    if origen in CAMINO_ORIGENES:
        resp.headers["Access-Control-Allow-Origin"] = origen
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def _camino_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else "") or (request.remote_addr or "?")


def _camino_limite(clave, maximo, ventana_s):
    """True si todavía está dentro del límite (y registra el intento)."""
    ahora = time.time()
    with _CAMINO_LOCK:
        hits = [t for t in _CAMINO_HITS.get(clave, []) if ahora - t < ventana_s]
        if len(hits) >= maximo:
            _CAMINO_HITS[clave] = hits
            return False
        hits.append(ahora)
        _CAMINO_HITS[clave] = hits
        if len(_CAMINO_HITS) > 5000:  # limpieza básica para no crecer sin fin
            for k in [k for k, v in _CAMINO_HITS.items() if not v or ahora - v[-1] > 86400]:
                _CAMINO_HITS.pop(k, None)
    return True


def _camino_limpiar(valor, maximo):
    """Texto libre seguro: sin caracteres de control, longitud acotada y sin
    que una celda de Google Sheets pueda interpretarse como fórmula."""
    t = re.sub(r"[\x00-\x1f\x7f]", " ", str(valor or ""))
    t = re.sub(r"\s+", " ", t).strip()[:maximo]
    if t[:1] in ("=", "+", "-", "@"):
        t = "'" + t
    return t


def _camino_tel(valor):
    d = re.sub(r"\D", "", str(valor or ""))
    if d.startswith("521") and len(d) == 13:
        d = d[3:]
    elif d.startswith("52") and len(d) == 12:
        d = d[2:]
    return "521" + d if len(d) == 10 else None


def _camino_email(valor):
    e = str(valor or "").strip()
    if not e:
        return ""
    if len(e) <= 120 and re.match(r"^[^@\s]{1,64}@[^@\s]{1,100}\.[^@\s]{2,}$", e):
        return e
    return None


def _camino_num(valor, maximo=1_000_000_000):
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= maximo else None


def _camino_token(folio, tel):
    llave = (os.environ.get("CAMINO_SECRET") or os.environ.get("WATI_API_KEY") or "camino").encode()
    return hmac.new(llave, f"{folio}|{tel}".encode(), hashlib.sha256).hexdigest()[:24]


def _camino_json():
    if (request.content_length or 0) > 8192:
        return None
    return request.get_json(silent=True)


# ------------------------------------------------------------------
# VERIFICACIÓN DE WHATSAPP -- pedido por Javier (2026-09-30): antes de
# dejar continuar en el camino, se confirma que el WhatsApp es real con
# un código corto de 2 dígitos (no 6 -- suficiente para este propósito,
# que es filtrar números inventados, no seguridad de alto riesgo).
# ------------------------------------------------------------------
_CAMINO_VERIF = {}   # tel (formato 521XXXXXXXXXX) -> {"codigo": "42", "expira": ts}
_CAMINO_VERIFICADOS = {}   # tel -> ts de cuándo se verificó (válido por 30 min, tiempo de sobra para terminar el formulario)

@app.route("/api/camino/verificar/enviar", methods=["POST", "OPTIONS"])
def api_camino_verificar_enviar():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _camino_json()
    if d is None:
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    tel = _camino_tel(d.get("whatsapp"))
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos."}, 400)
    if not _camino_limite("verif:" + tel, 3, 600):
        return _camino_resp({"ok": False, "error": "Ya te mandamos varios códigos. Espera unos minutos e intenta de nuevo."}, 429)
    if not _camino_limite("verif:ip:" + (request.remote_addr or ""), 15, 3600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Intenta más tarde."}, 429)
    codigo = f"{random.randint(0, 99):02d}"
    with _CAMINO_LOCK:
        _CAMINO_VERIF[tel] = {"codigo": codigo, "expira": time.time() + 600}
        if len(_CAMINO_VERIF) > 5000:
            _CAMINO_VERIF.clear()  # limpieza básica, igual que _CAMINO_HITS
    # IMPORTANTE: quien llena este formulario casi nunca le ha escrito antes a
    # nuestro WhatsApp -- no hay sesion/ticket abierto, asi que wati_send_text
    # (mensaje de texto libre) va a fallar siempre para un visitante nuevo. Por
    # eso cae de respaldo a una plantilla aprobada por Meta, igual que ya
    # corregimos hoy para el seguimiento a clientes y la copia a Javier.
    enviado = wati_send_text(tel, f"Tu código para confirmar tu WhatsApp en acierta.pro es: {codigo}")
    # Plantillas a probar en orden. "codigo_verificacion" la rechazo Meta como
    # Utility (4-oct-2026); "codigo_acceso" es la nueva en categoria Autenticacion.
    # Se pueden cambiar sin tocar codigo con la variable WATI_PLANTILLAS_CODIGO.
    for _plantilla in [x.strip() for x in os.environ.get(
            "WATI_PLANTILLAS_CODIGO", "codigo_acceso,codigo_verificacion").split(",") if x.strip()]:
        if enviado:
            break
        enviado = wati_send_template_message(tel, _plantilla, [codigo])
    if not enviado:
        return _camino_resp({"ok": False, "error": "No pudimos enviar el código. Intenta de nuevo."}, 502)
    return _camino_resp({"ok": True})

# ------------------------------------------------------------------
# VERIFICACIÓN INVERTIDA (4-oct-2026): Meta no deja a esta cuenta crear
# plantillas de autenticación, y sin plantilla no se le puede escribir
# primero a un número nuevo. Así que ahora es el CLIENTE quien nos escribe:
# la página le muestra un botón que abre WhatsApp con "Mi código Acierta es
# 1234"; el webhook lo recibe desde SU número (eso prueba que el número es
# suyo), lo marca verificado y le contesta (ya hay sesión abierta de 24 h).
# La página consulta /estado cada pocos segundos para avanzar sola.
# ------------------------------------------------------------------
WHATSAPP_ACIERTA = os.environ.get("WHATSAPP_ACIERTA", "523333777337")
_RE_CODIGO_ACIERTA = re.compile(r"c[oó]digo\s+acierta(?:\s+es)?\s*:?\s*(\d{4})", re.I)


@app.route("/api/camino/verificar/iniciar", methods=["POST", "OPTIONS"])
def api_camino_verificar_iniciar():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _camino_json()
    if d is None:
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    tel = _camino_tel(d.get("whatsapp"))
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos."}, 400)
    if not _camino_limite("verifini:" + tel, 6, 600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Espera unos minutos."}, 429)
    if not _camino_limite("verifini:ip:" + (request.remote_addr or ""), 30, 3600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Intenta más tarde."}, 429)
    with _CAMINO_LOCK:
        if tel in _CAMINO_VERIFICADOS and time.time() - _CAMINO_VERIFICADOS[tel] <= 1800:
            return _camino_resp({"ok": True, "verificado": True})
        entrada = _CAMINO_VERIF.get(tel)
        if entrada and entrada["expira"] > time.time() and len(entrada["codigo"]) == 4:
            codigo = entrada["codigo"]          # mismo código si lo vuelve a pedir
        else:
            codigo = f"{random.randint(0, 9999):04d}"
        _CAMINO_VERIF[tel] = {"codigo": codigo, "expira": time.time() + 900}
        if len(_CAMINO_VERIF) > 5000:
            _CAMINO_VERIF.clear()
    mensaje = f"Hola, quiero confirmar mi WhatsApp. Mi código Acierta es {codigo}"
    liga = f"https://wa.me/{WHATSAPP_ACIERTA}?text={quote(mensaje)}"
    return _camino_resp({"ok": True, "verificado": False, "codigo": codigo, "liga": liga})


@app.route("/api/camino/verificar/estado", methods=["POST", "OPTIONS"])
def api_camino_verificar_estado():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _camino_json()
    tel = _camino_tel((d or {}).get("whatsapp"))
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos."}, 400)
    if not _camino_limite("verifest:" + tel, 400, 900):
        return _camino_resp({"ok": False, "error": "Demasiadas consultas."}, 429)
    with _CAMINO_LOCK:
        ts = _CAMINO_VERIFICADOS.get(tel)
    return _camino_resp({"ok": True, "verificado": bool(ts and time.time() - ts <= 1800)})


def _camino_verificacion_entrante(phone, text):
    """Si el mensaje entrante es 'Mi código Acierta es 1234', lo procesa y
    devuelve True (el webhook ya no lo pasa al asistente). Si no, False."""
    m = _RE_CODIGO_ACIERTA.search(text or "")
    if not m:
        return False
    digitos = re.sub(r"\D", "", phone or "")
    tel = "521" + digitos[-10:] if len(digitos) >= 10 else None
    codigo = m.group(1)
    with _CAMINO_LOCK:
        entrada = _CAMINO_VERIF.get(tel) if tel else None
        ok = bool(entrada and entrada["expira"] > time.time() and entrada["codigo"] == codigo)
        if ok:
            _CAMINO_VERIF.pop(tel, None)
            _CAMINO_VERIFICADOS[tel] = time.time()
    if ok:
        print(f"[MAX-CAMINO] WhatsApp verificado por mensaje entrante: {tel}", flush=True)
        wati_send_text(phone, "¡Listo! ✅ Tu WhatsApp quedó confirmado. Regresa a acierta.pro para "
                              "continuar con tu proceso; la página avanza sola.", copiar=False)
    else:
        print(f"[MAX-CAMINO] Código de verificación no válido o vencido de {phone}", flush=True)
        wati_send_text(phone, "Ese código ya no es válido o venció. En acierta.pro vuelve a presionar "
                              "\"Verificar por WhatsApp\" y envía el mensaje nuevo, sin cambiarlo.", copiar=False)
    return True


@app.route("/api/camino/verificar/confirmar", methods=["POST", "OPTIONS"])
def api_camino_verificar_confirmar():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _camino_json()
    if d is None:
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    tel = _camino_tel(d.get("whatsapp"))
    codigo = re.sub(r"\D", "", str(d.get("codigo") or ""))
    if not tel or not codigo:
        return _camino_resp({"ok": False, "error": "Falta el WhatsApp o el código."}, 400)
    if not _camino_limite("verifconf:" + tel, 8, 600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Pide un código nuevo."}, 429)
    with _CAMINO_LOCK:
        entrada = _CAMINO_VERIF.get(tel)
    if not entrada or entrada["expira"] < time.time():
        return _camino_resp({"ok": False, "error": "Tu código expiró. Pide uno nuevo."}, 400)
    if codigo != entrada["codigo"]:
        return _camino_resp({"ok": False, "error": "Código incorrecto."}, 400)
    with _CAMINO_LOCK:
        _CAMINO_VERIF.pop(tel, None)
        _CAMINO_VERIFICADOS[tel] = time.time()
    return _camino_resp({"ok": True})

@app.route("/api/camino", methods=["POST", "OPTIONS"])
def api_camino():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    origen = request.headers.get("Origin", "")
    if origen and origen not in CAMINO_ORIGENES:
        return _camino_resp({"ok": False, "error": "Origen no permitido."}, 403)
    d = _camino_json()
    if not isinstance(d, dict):
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    if d.get("empresa"):  # campo trampa para bots: se descarta en silencio
        return _camino_resp({"ok": True, "folio": "", "vendedor": ""})
    if not _camino_limite("ip:" + _camino_ip(), 8, 3600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Intenta de nuevo en un rato."}, 429)

    if d.get("consentimiento") is not True:
        return _camino_resp({"ok": False, "error": "Necesitamos tu consentimiento para guardar tus datos y contactarte."}, 400)
    nombre = _camino_limpiar(d.get("nombre"), 80)
    if len(nombre) < 2:
        return _camino_resp({"ok": False, "error": "Escribe tu nombre."}, 400)
    tel = _camino_tel(d.get("whatsapp"))
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos."}, 400)
    with _CAMINO_LOCK:
        ts_verif = _CAMINO_VERIFICADOS.get(tel)
    if not ts_verif or time.time() - ts_verif > 1800:
        return _camino_resp({"ok": False, "error": "Primero confirma tu WhatsApp con el código que te enviamos."}, 400)
    email = _camino_email(d.get("email"))
    if email is None:
        return _camino_resp({"ok": False, "error": "Revisa tu correo."}, 400)
    op = _CAMINO_OP.get(d.get("operacion"))
    if not op:
        return _camino_resp({"ok": False, "error": "Elige qué quieres hacer."}, 400)
    tipo = d.get("tipo") if d.get("tipo") in _CAMINO_TIPO else "nose"
    uso = d.get("uso") if d.get("uso") in _CAMINO_USO else ""
    cuando = d.get("cuando") if d.get("cuando") in _CAMINO_CUANDO else ""
    muni = d.get("municipio") if d.get("municipio") in _CAMINO_MUNI else "cualquiera"
    colonia = _camino_limpiar(d.get("colonia"), 60)
    zona = (muni if muni != "cualquiera" else "Cualquier zona de la ZMG") + (f" ({colonia})" if colonia else "")
    if not _camino_limite("tel:" + tel, 3, 86400):
        return _camino_resp({"ok": False, "error": "Ya recibimos tus datos hoy. Un asesor te contactará."}, 429)

    interes = _CAMINO_TIPO[tipo] + (f" {_CAMINO_USO[uso]}" if uso else "")
    resumen = f"{op.upper()} · {_CAMINO_TIPO[tipo]}" + (f" · {_CAMINO_USO[uso]}" if uso else "")
    perfil = {"email": email, "resumen": resumen, "zona": zona,
              "cuando": _CAMINO_CUANDO.get(cuando, "")}
    aviso = _camino_limpiar(d.get("aviso_version"), 40)
    notas = (f"[WEB camino] Correo: {email or 'no dio'} | Para cuándo: {perfil['cuando'] or 'n/d'} | "
             f"Consentimiento aviso de privacidad {aviso or 'n/d'}: {hora_gdl()} | "
             f"Urgencia: {'ya, pronto' if cuando == 'ya' else 'normal'}")
    try:
        # ¿Ya tiene expediente abierto? No se duplica: se le avisa al mismo vendedor.
        fila_ex, reg_ex = _crm_buscar_activo_por_cliente(tel)
        if fila_ex and reg_ex:
            vend_nombre = reg_ex.get("VENDEDOR", "")
            vend_phone = reg_ex.get("VENDEDOR_PHONE", "")
            folio_ex = reg_ex.get("FOLIO", "")
            if vend_phone:
                notificar_interno(
                    vend_phone,
                    f"🔁 {nombre} ({tel}) volvió a llenar el formulario de acierta.pro — {folio_ex}\n"
                    f"Quiere: {resumen}\nZona: {zona}\nPara cuándo: {perfil['cuando'] or 'n/d'}",
                    resumen_para_plantilla=f"Cliente: {nombre} | WA: {tel} | Web: {resumen[:120]} | Folio: {folio_ex}")
            return _camino_resp({"ok": True, "folio": folio_ex, "vendedor": vend_nombre,
                                 "existente": True, "token": _camino_token(folio_ex, tel)})

        res = registrar_lead(tel, nombre=nombre, interes=interes, operacion=op, presupuesto="",
                             zona=zona, notas=notas, tipo=_CAMINO_TIPO[tipo])
        folio = res.get("folio")
        if not folio:
            print(f"[MAX-CAMINO] No se pudo registrar el lead: {res}", flush=True)
            return _camino_resp({"ok": False, "error": "No pudimos guardar tus datos. Escríbenos por WhatsApp."}, 503)
        try:
            memoria_guardar(tel, ZONA=zona, OPERACION=op,
                            NOTAS_COACHING=f"Llegó por el camino web de acierta.pro: {resumen}; zona {zona}; "
                                           f"para cuándo: {perfil['cuando'] or 'n/d'}. Ya dio consentimiento para ser contactado.")
        except Exception:
            pass
        cr = crm_crear_registro(tel, nombre, [], operacion=op, perfil_web=perfil, vendedor_preferido=_camino_limpiar(d.get("coach_conocido"), 40))
        vendedor = cr.get("vendedor", "") if cr.get("creado") else ""
        if cr.get("creado"):
            try:
                memoria_guardar(tel, CRM_INICIADO="Si")
            except Exception:
                pass
        if JAVIER_PERSONAL:
            estado = (f"Vendedor asignado: {vendedor} ({cr.get('folio_crm')})" if vendedor else
                      "⚠️ NO se pudo asignar vendedor automáticamente — asígnalo tú.")
            notificar_interno(
                JAVIER_PERSONAL,
                f"🌐 NUEVO LEAD WEB — {folio}\n\nCliente: {nombre} ({tel})\nQuiere: {resumen}\nZona: {zona}\n"
                f"Para cuándo: {perfil['cuando'] or 'n/d'}\n{estado}",
                resumen_para_plantilla=f"Web: {nombre} | WA: {tel} | {resumen[:100]} | {estado[:60]} | Folio: {folio}")
        return _camino_resp({"ok": True, "folio": folio, "vendedor": vendedor,
                             "token": _camino_token(folio, tel)})
    except Exception as e:
        print(f"[MAX-CAMINO] Error: {e}", flush=True)
        return _camino_resp({"ok": False, "error": "No pudimos guardar tus datos. Escríbenos por WhatsApp."}, 500)


@app.route("/api/camino/completar", methods=["POST", "OPTIONS"])
def api_camino_completar():
    """Al final del camino: guarda presupuesto, forma de pago y demás detalles
    en el lead ya creado, y avisa UNA vez al vendedor con el perfil completo."""
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    origen = request.headers.get("Origin", "")
    if origen and origen not in CAMINO_ORIGENES:
        return _camino_resp({"ok": False, "error": "Origen no permitido."}, 403)
    d = _camino_json()
    if not isinstance(d, dict):
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    if not _camino_limite("ipc:" + _camino_ip(), 20, 3600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos."}, 429)
    folio = _camino_limpiar(d.get("folio"), 20)
    tel = _camino_tel(d.get("whatsapp"))
    if not (folio and tel and hmac.compare_digest(str(d.get("token", "")), _camino_token(folio, tel))):
        return _camino_resp({"ok": False, "error": "No autorizado."}, 403)
    if not _camino_limite("fol:" + folio, 3, 86400):
        return _camino_resp({"ok": False, "error": "Ya actualizamos tu proceso."}, 429)

    presupuesto = _camino_num(d.get("presupuesto"))
    credito = d.get("credito") if d.get("credito") in _CAMINO_CREDITO else ""
    verifica = d.get("verifica") is True
    ingresos = _camino_num(d.get("ingresos"), 10_000_000)
    rec = d.get("recamaras") if str(d.get("recamaras", "")) in ("1", "2", "3", "4", "5") else ""
    nota_libre = _camino_limpiar(d.get("notas"), 200)
    es_renta = d.get("operacion") == "renta"
    pres_txt = (f"${presupuesto:,.0f}" + (" /mes" if es_renta else "")) if presupuesto else ""
    partes = []
    if pres_txt:
        partes.append(f"Presupuesto: {pres_txt}")
    if credito:
        partes.append(f"Forma de pago: {_CAMINO_CREDITO[credito]}")
    if ingresos:
        partes.append(f"Ingresos aprox. del hogar: ${ingresos:,.0f}/mes")
    if rec:
        partes.append(f"Recámaras: {rec}+")
    partes.append("Quiere Acierta Verifica: " + ("SÍ" if verifica else "no por ahora"))
    if nota_libre:
        partes.append(f"Comentario: {nota_libre}")
    detalle = " | ".join(partes)
    try:
        libro, _ = _sheets_client()
        if not libro:
            return _camino_resp({"ok": False, "error": "No disponible por ahora."}, 503)
        sh = libro.worksheet("Leads MAX")
        celda = sh.find(folio, in_column=1)
        if not celda:
            return _camino_resp({"ok": False, "error": "Folio no encontrado."}, 404)
        fila = sh.row_values(celda.row)
        headers = sh.row_values(1)
        def col(prefijo):
            for i, h in enumerate(headers, start=1):
                if h.strip().upper().startswith(prefijo):
                    return i
            return None
        c_pres, c_notas = col("PRESUPUESTO"), col("NOTAS")
        if c_pres and pres_txt:
            sh.update_cell(celda.row, c_pres, pres_txt)
        if c_notas:
            previa = fila[c_notas - 1] if len(fila) >= c_notas else ""
            sh.update_cell(celda.row, c_notas, (previa + " || " if previa else "") + "[WEB completo] " + detalle)
        nombre = fila[3] if len(fila) > 3 else ""
        operacion = fila[4] if len(fila) > 4 else ""
        zona = fila[7] if len(fila) > 7 else ""
        interes = fila[5] if len(fila) > 5 else ""
        nuevo = _calcular_score_lead(nombre, interes, operacion, pres_txt, zona, detalle + " credito " + (credito or ""))
        calif = _calificacion_perfil(nombre, operacion, pres_txt, zona, interes, interes, detalle)
        _actualizar_score_lead_si_sube(folio, nuevo, calif)
        try:
            memoria_guardar(tel, PRESUPUESTO=pres_txt,
                            NOTAS_COACHING=f"Completó el camino web: {detalle}")
        except Exception:
            pass
        _, reg = _crm_buscar_activo_por_cliente(tel)
        if reg and reg.get("VENDEDOR_PHONE"):
            notificar_interno(
                reg["VENDEDOR_PHONE"],
                f"➕ {nombre} completó su perfil en acierta.pro — {reg.get('FOLIO', folio)}\n\n{detalle.replace(' | ', chr(10))}",
                resumen_para_plantilla=f"Cliente: {nombre} | WA: {tel} | {detalle[:150]} | Folio: {reg.get('FOLIO', folio)}")
        return _camino_resp({"ok": True})
    except Exception as e:
        print(f"[MAX-CAMINO] Error al completar {folio}: {e}", flush=True)
        return _camino_resp({"ok": False, "error": "No pudimos actualizar tu proceso."}, 500)


# ------------------------------------------------------------------
# PULSO ACIERTA MAX (oct-2026; antes «Pulso Inmobiliario», nombre registrado por un tercero): suscripción al resumen
# ejecutivo. Dos entradas:
#  1) Formulario en acierta.pro/pulso -> /api/pulso/suscribir guarda los
#     datos (pendiente) y regresa una liga wa.me con "PULSO 1234".
#  2) Cualquiera que escriba PULSO al WhatsApp (campañas, Instagram).
# Al recibir PULSO por WhatsApp se activa la suscripción, se manda la
# bienvenida y el PDF de la edición vigente (hay sesión abierta de 24 h,
# no requiere plantilla) y se marca el contacto en Wati (pulso=si) para
# que Javier mande las ediciones con "Transmisiones masivas" filtrando
# por ese atributo. "BAJA PULSO" cancela. Datos en la hoja de Sheets
# "Suscriptores Pulso". La edición vigente se define en el repo del sitio
# (pulso/ediciones.json), así que publicar una nueva no requiere tocar MAX.
# ------------------------------------------------------------------
PULSO_HOJA = "Suscriptores Pulso"
PULSO_COLUMNAS = ["FECHA_ALTA", "WHATSAPP", "NOMBRE", "CORREO", "INTERESES", "ORIGEN", "ESTADO",
                  "FECHA_CONFIRMACION", "FECHA_BAJA", "ULTIMA_EDICION", "AVISO_VERSION",
                  "ULTIMA_EDICION_CORREO"]
PULSO_METRICAS_HOJA = "Pulso Métricas"
PULSO_METRICAS_COLUMNAS = ["MES", "MENSAJES_WHATSAPP", "CORREOS", "ALTAS", "BAJAS", "COSTO_ESTIMADO_USD", "ALERTA_ENVIADA"]
PULSO_TARIFA_USD = float(os.environ.get("PULSO_TARIFA_USD", "0.0085"))     # mensaje de servicio en México (oct-2026)
PULSO_ALERTA_USD = float(os.environ.get("PULSO_ALERTA_USD", "40"))
PULSO_CORREOS_DIA = int(os.environ.get("PULSO_CORREOS_DIA", "400"))       # Gmail limita los envíos diarios
MAX_URL_PUBLICA = os.environ.get("MAX_URL_PUBLICA", "https://aciertamax-webhook.onrender.com")
_PULSO_LOCK_HOJA = threading.Lock()
PULSO_EDICIONES_URL = "https://raw.githubusercontent.com/javiermendosalinas-afk/acierta-pro-web/main/pulso/ediciones.json"
PULSO_INTERESES = ("comprar", "vender", "rentar", "invertir", "verifica")
_PULSO_PEND = {}            # tel -> {codigo, nombre, correo, intereses, aviso, expira}
_PULSO_CACHE = {"ed": None, "ts": 0, "pdf": None, "pdf_url": None}
_PULSO_RECIENTES = {}       # tel -> ts del último PULSO procesado (evita reintentos duplicados del webhook)
_RE_PULSO = re.compile(r"^\s*(?:hola[,.!\s]*)?pulso\b[^\d]*(\d{4})?", re.I)
_RE_PULSO_BAJA = re.compile(r"^\s*(baja|cancelar|stop|alto)\s+pulso\b", re.I)


def _pulso_edicion():
    """Edición vigente desde el repo del sitio (cache 1 h) y su PDF (cache)."""
    if not _PULSO_CACHE["ed"] or time.time() - _PULSO_CACHE["ts"] > 3600:
        try:
            r = requests.get(PULSO_EDICIONES_URL, timeout=10)
            if r.status_code == 200:
                eds = r.json().get("ediciones") or []
                vig = next((e for e in eds if e.get("vigente")), eds[0] if eds else None)
                if vig:
                    _PULSO_CACHE.update(ed=vig, ts=time.time())
        except Exception as e:
            print(f"[MAX-PULSO] No se pudo leer ediciones.json: {e}", flush=True)
    ed = _PULSO_CACHE["ed"]
    if ed and ed.get("pdf_raw") and _PULSO_CACHE["pdf_url"] != ed["pdf_raw"]:
        try:
            r = requests.get(ed["pdf_raw"], timeout=20)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                _PULSO_CACHE.update(pdf=r.content, pdf_url=ed["pdf_raw"])
        except Exception as e:
            print(f"[MAX-PULSO] No se pudo descargar el PDF: {e}", flush=True)
    return ed, _PULSO_CACHE["pdf"]


def _pulso_hoja():
    libro, _ = _sheets_client()
    if not libro:
        return None
    sh = _get_o_crear_hoja(libro, PULSO_HOJA, PULSO_COLUMNAS)
    try:   # si la hoja se creó con menos columnas, se completa el encabezado
        enc = sh.row_values(1)
        if enc != PULSO_COLUMNAS[:len(enc)] or len(enc) < len(PULSO_COLUMNAS):
            if sh.col_count < len(PULSO_COLUMNAS):
                sh.add_cols(len(PULSO_COLUMNAS) - sh.col_count)
            sh.update("A1", [PULSO_COLUMNAS])
    except Exception as e:
        print(f"[MAX-PULSO] No se pudo revisar el encabezado: {e}", flush=True)
    return sh


def _pulso_fila(tel):
    """Registro del suscriptor (dict) o None."""
    try:
        sh = _pulso_hoja()
        if not sh:
            return None
        valores = sh.get_all_values()
        t10 = _ultimos10(tel)
        for fila in valores[1:]:
            d = dict(zip(PULSO_COLUMNAS, fila + [""] * (len(PULSO_COLUMNAS) - len(fila))))
            if _ultimos10(d.get("WHATSAPP")) == t10:
                return d
    except Exception as e:
        print(f"[MAX-PULSO] No se pudo leer el suscriptor {tel}: {e}", flush=True)
    return None


def _pulso_metrica(campo, n=1):
    """Suma al contador del mes y avisa a Javier UNA vez cuando el costo
    estimado de WhatsApp del Pulso llega a PULSO_ALERTA_USD. Es conservador:
    no descuenta los 1,000 mensajes de servicio gratis al mes (los comparte
    todo MAX), así que el costo real será igual o menor."""
    def _hacer():
        try:
            libro, _ = _sheets_client()
            if not libro:
                return
            with _PULSO_LOCK_HOJA:
                sh = _get_o_crear_hoja(libro, PULSO_METRICAS_HOJA, PULSO_METRICAS_COLUMNAS)
                mes = hora_gdl()[:7]
                valores = sh.get_all_values()
                idx = next((i for i in range(1, len(valores)) if valores[i] and valores[i][0] == mes), None)
                fila = (valores[idx] + [""] * 7)[:7] if idx else [mes, "0", "0", "0", "0", "0", ""]
                col = PULSO_METRICAS_COLUMNAS.index(campo)
                fila[col] = str(int(float(fila[col] or 0)) + n)
                costo = int(float(fila[1] or 0)) * PULSO_TARIFA_USD
                fila[5] = f"{costo:.2f}"
                avisar = costo >= PULSO_ALERTA_USD and not fila[6]
                if avisar:
                    fila[6] = hora_gdl()
                if idx:
                    sh.update(f"A{idx + 1}", [fila])
                else:
                    sh.append_row(fila)
            if avisar:
                activos = "?"
                try:
                    hp = _pulso_hoja()
                    activos = sum(1 for f in hp.get_all_values()[1:] if len(f) > 6 and f[6] == "activo")
                except Exception:
                    pass
                texto = (f"📊 PULSO ACIERTA MAX — el costo estimado de WhatsApp del mes ({mes}) llegó a "
                         f"USD {costo:.2f} ({fila[1]} mensajes). Suscriptores activos: {activos}. "
                         f"Altas del mes: {fila[3]} · Bajas: {fila[4]} · Correos enviados: {fila[2]}. "
                         "Es momento de evaluar la rentabilidad. Detalle en la hoja 'Pulso Métricas'.")
                notificar_interno(JAVIER_PHONE, texto, texto[:300])
        except Exception as e:
            print(f"[MAX-PULSO] No se pudo actualizar métricas: {e}", flush=True)
    threading.Thread(target=_hacer, daemon=True).start()


def _pulso_token_baja(tel):
    t10 = _ultimos10(tel)
    return f"{t10}.{hmac.new(_asesor_llave(), ('baja:' + t10).encode(), hashlib.sha256).hexdigest()[:20]}"


def _pulso_correo(destino, nombre, ed, pdf, tel, bienvenida=False):
    """Correo con la edición (PDF adjunto) y enlace de baja. True si salió."""
    if not (GMAIL_USER and GMAIL_PASS and destino and ed):
        return False
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from email.utils import formataddr
    import html as _h
    primer = _h.escape((nombre or "").split()[0]) if nombre else ""
    baja = f"{MAX_URL_PUBLICA}/api/pulso/baja?t={_pulso_token_baja(tel)}"
    portada = "https://acierta.pro" + ed["portada"] if str(ed.get("portada", "")).startswith("/") else ed.get("portada", "")
    intro = ("Gracias por suscribirte al <b>PULSO de Acierta Max</b>. Aquí tienes tu regalo: la edición más reciente."
             if bienvenida else "Ya está aquí la nueva edición del <b>PULSO de Acierta Max</b>.")
    cuerpo = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a">
  <div style="background:#0A1F3F;padding:18px 22px;border-radius:12px 12px 0 0">
    <img src="https://acierta.pro/assets/pulso/pulso-acierta-max-blanco.png" alt="PULSO Acierta Max · Profesionales Inmobiliarios" width="260" style="display:block;width:260px;max-width:100%;height:auto">
  </div>
  <div style="background:#fff;border:1px solid #e5e7eb;border-top:0;padding:22px;border-radius:0 0 12px 12px">
    <p style="font-size:16px">Hola{(' ' + primer) if primer else ''},</p>
    <p style="font-size:15px;line-height:1.5">{intro}</p>
    <p style="font-size:17px;font-weight:bold;color:#0b1f3a;margin:16px 0 6px">{_h.escape(ed.get('tema') or ed.get('titulo') or '')}</p>
    {f'<a href="{_h.escape(ed.get("url", ""))}"><img src="{_h.escape(portada)}" alt="Portada" width="220" style="border-radius:10px;margin:10px 0;max-width:100%"></a>' if portada else ''}
    <p style="font-size:14px;color:#555">Va adjunta en PDF, diseñada para leerse en el teléfono.</p>
    <p><a href="{_h.escape(ed.get('url', 'https://acierta.pro'))}" style="background:#d62828;color:#fff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:bold;display:inline-block">Leer la edición</a></p>
    <p style="font-size:14px;line-height:1.5;margin-top:20px">Busca propiedades en la Zona Metropolitana de Guadalajara y conoce <b>ACIERTA VERIFICA</b>, nuestra revisión física y documental antes de firmar, en <a href="https://acierta.pro" style="color:#0b1f3a">acierta.pro</a>.</p>
    <hr style="border:0;border-top:1px solid #eee;margin:20px 0">
    <p style="font-size:12px;color:#888;line-height:1.5">Recibes este correo porque te suscribiste al PULSO de Acierta Max en acierta.pro.
    <a href="{baja}" style="color:#888">Darme de baja</a> · <a href="https://acierta.pro/aviso-privacidad.html" style="color:#888">Aviso de privacidad</a><br>
    Acierta Max · Profesionales Inmobiliarios · WhatsApp 33 3377 7337</p>
  </div></div>"""
    msg = MIMEMultipart()
    msg["From"] = formataddr(("Pulso acierta.pro", GMAIL_USER))
    msg["To"] = destino
    msg["Subject"] = ("Tu PULSO de Acierta Max: " if bienvenida else "Nuevo PULSO de Acierta Max: ") + (ed.get("tema") or "acierta.pro")[:90]
    msg["List-Unsubscribe"] = f"<{baja}>"
    msg.attach(MIMEText(cuerpo, "html", "utf-8"))
    if pdf:
        adj = MIMEApplication(pdf, _subtype="pdf")
        adj.add_header("Content-Disposition", "attachment", filename=ed.get("archivo") or "Pulso_acierta_pro.pdf")
        msg.attach(adj)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
        _pulso_metrica("CORREOS")
        return True
    except Exception as e:
        print(f"[MAX-PULSO] No se pudo enviar el correo a {destino}: {e}", flush=True)
        return False


@app.route("/api/pulso/baja", methods=["GET", "POST"])
def api_pulso_baja():
    tok = request.args.get("t", "")
    t10 = tok.split(".")[0]
    pagina = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>Pulso acierta.pro</title><div style="font-family:Arial,sans-serif;max-width:480px;margin:60px auto;'
              'padding:0 20px;text-align:center"><h2 style="color:#0b1f3a">{t}</h2><p style="color:#555">{m}</p>'
              '<p><a href="https://acierta.pro" style="color:#d62828">Ir a acierta.pro</a></p></div>')
    if len(t10) != 10 or not hmac.compare_digest(tok, _pulso_token_baja(t10)):
        return pagina.format(t="Enlace no válido", m="Escribe BAJA PULSO a nuestro WhatsApp 33 3377 7337 y te damos de baja."), 400
    threading.Thread(target=_pulso_guardar, args=("521" + t10, {"ESTADO": "baja", "FECHA_BAJA": hora_gdl()}), daemon=True).start()
    threading.Thread(target=_pulso_wati_atributos, args=("521" + t10, {"pulso": "no"}), daemon=True).start()
    _pulso_metrica("BAJAS")
    print(f"[MAX-PULSO] Baja por correo de {t10}", flush=True)
    return pagina.format(t="Listo, te dimos de baja", m="Ya no recibirás el PULSO de Acierta Max. Si quieres retomarlo, escribe PULSO a nuestro WhatsApp.")


def _pulso_envio_correos():
    """Manda por correo la edición vigente que tenga "enviar_correo": true a los
    suscriptores activos con correo que aún no la recibieron. Respeta el
    límite diario de Gmail y sigue al día siguiente con los que falten."""
    ed, pdf = _pulso_edicion()
    if not (ed and ed.get("enviar_correo") and GMAIL_USER and GMAIL_PASS):
        return 0
    sh = _pulso_hoja()
    if not sh:
        return 0
    valores = sh.get_all_values()
    col = {h: i for i, h in enumerate(PULSO_COLUMNAS)}
    hoy = hora_gdl()[:10]
    if _PULSO_CACHE.get("dia") != hoy:
        _PULSO_CACHE.update(dia=hoy, enviados_hoy=0)
    enviados = 0
    for i in range(1, len(valores)):
        if _PULSO_CACHE["enviados_hoy"] >= PULSO_CORREOS_DIA:
            break
        f = valores[i] + [""] * (len(PULSO_COLUMNAS) - len(valores[i]))
        if f[col["ESTADO"]] != "activo" or not f[col["CORREO"]] or f[col["ULTIMA_EDICION_CORREO"]] == str(ed.get("numero")):
            continue
        if _pulso_correo(f[col["CORREO"]], f[col["NOMBRE"]], ed, pdf, f[col["WHATSAPP"]]):
            sh.update_cell(i + 1, col["ULTIMA_EDICION_CORREO"] + 1, str(ed.get("numero")))
            _PULSO_CACHE["enviados_hoy"] += 1
            enviados += 1
            time.sleep(2)
    if enviados:
        print(f"[MAX-PULSO] Edición {ed.get('numero')} enviada por correo a {enviados} suscriptores", flush=True)
    return enviados


def _loop_pulso_correos():
    time.sleep(120)
    while True:
        try:
            _pulso_envio_correos()
        except Exception as e:
            print(f"[MAX-PULSO] Error en el envío de correos: {e}", flush=True)
        time.sleep(3600)


if os.environ.get("PULSO_CORREOS", "1") != "0":
    threading.Thread(target=_loop_pulso_correos, daemon=True).start()


def _pulso_guardar(tel, cambios):
    """Alta o actualización por número (últimos 10 dígitos). Nunca duplica."""
    try:
      with _PULSO_LOCK_HOJA:
          sh = _pulso_hoja()
          if not sh:
              return
          valores = sh.get_all_values()
          col = {h: i for i, h in enumerate(PULSO_COLUMNAS)}
          t10 = _ultimos10(tel)
          for idx in range(1, len(valores)):
              if _ultimos10(valores[idx][col["WHATSAPP"]] if len(valores[idx]) > col["WHATSAPP"] else "") == t10:
                  fila = valores[idx] + [""] * (len(PULSO_COLUMNAS) - len(valores[idx]))
                  for k, v in cambios.items():
                      if v not in (None, ""):
                          fila[col[k]] = v
                  sh.update(f"A{idx + 1}", [fila[:len(PULSO_COLUMNAS)]])
                  return
          fila = [""] * len(PULSO_COLUMNAS)
          fila[col["FECHA_ALTA"]] = hora_gdl()
          fila[col["WHATSAPP"]] = t10
          for k, v in cambios.items():
              fila[col[k]] = v or fila[col[k]]
          sh.append_row(fila)
    except Exception as e:
        print(f"[MAX-PULSO] No se pudo guardar en Sheets ({tel}): {e}", flush=True)


def _pulso_wati_atributos(tel, atributos, nombre=""):
    """Marca el contacto en Wati para segmentar las transmisiones masivas."""
    try:
        ph = _normalizar_phone_wati(tel)
        params = [{"name": k, "value": str(v)} for k, v in atributos.items()]
        r = requests.post(f"{WATI_BASE_URL}/api/v1/updateContactAttributes/{ph}", headers=wati_headers(),
                          json={"customParams": params}, timeout=15)
        if r.status_code not in (200, 201) and nombre:
            requests.post(f"{WATI_BASE_URL}/api/v1/addContact/{ph}", headers=wati_headers(),
                          json={"name": nombre, "customParams": params}, timeout=15)
    except Exception as e:
        print(f"[MAX-PULSO] No se pudieron actualizar atributos en Wati ({tel}): {e}", flush=True)


@app.route("/api/pulso/suscribir", methods=["POST", "OPTIONS"])
def api_pulso_suscribir():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _camino_json()
    if d is None:
        return _camino_resp({"ok": False, "error": "Solicitud inválida."}, 400)
    if d.get("sitio_web"):                      # trampa para bots (campo oculto)
        return _camino_resp({"ok": True, "codigo": "0000", "liga": "https://acierta.pro"})
    nombre = _camino_limpiar(d.get("nombre"), 80)
    tel = _camino_tel(d.get("whatsapp"))
    correo = _camino_limpiar(d.get("correo"), 120)
    intereses = [i for i in (d.get("intereses") or []) if i in PULSO_INTERESES]
    if len(nombre) < 2:
        return _camino_resp({"ok": False, "error": "Escribe tu nombre."}, 400)
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe tu WhatsApp a 10 dígitos."}, 400)
    if correo and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", correo):
        return _camino_resp({"ok": False, "error": "Revisa tu correo electrónico."}, 400)
    if not d.get("acepta"):
        return _camino_resp({"ok": False, "error": "Necesitamos tu autorización para enviarte el Pulso."}, 400)
    if not _camino_limite("pulso:ip:" + _camino_ip(), 20, 3600) or not _camino_limite("pulso:" + tel, 6, 3600):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Intenta más tarde."}, 429)
    with _CAMINO_LOCK:
        previo = _PULSO_PEND.get(tel)
        codigo = previo["codigo"] if previo and previo["expira"] > time.time() else f"{random.randint(0, 9999):04d}"
        _PULSO_PEND[tel] = {"codigo": codigo, "nombre": nombre, "correo": correo, "intereses": intereses,
                            "aviso": _camino_limpiar(d.get("aviso_version"), 40), "expira": time.time() + 86400}
        if len(_PULSO_PEND) > 20000:
            _PULSO_PEND.clear()
    threading.Thread(target=_pulso_guardar, args=(tel, {
        "NOMBRE": nombre, "CORREO": correo, "INTERESES": ", ".join(intereses), "ORIGEN": "web",
        "ESTADO": "pendiente", "AVISO_VERSION": _camino_limpiar(d.get("aviso_version"), 40)}), daemon=True).start()
    mensaje = f"PULSO {codigo} — Hola, soy {nombre.split()[0]} y quiero recibir el PULSO de Acierta Max"
    return _camino_resp({"ok": True, "codigo": codigo,
                         "liga": f"https://wa.me/{WHATSAPP_ACIERTA}?text={quote(mensaje)}"})


def _pulso_bienvenida(phone, nombre, reenvio=False):
    ed, pdf = _pulso_edicion()
    primer = (nombre or "").split()[0] if nombre else ""
    saludo = f"¡Hola{(' ' + primer) if primer else ''}! 👋"
    if reenvio:
        texto = (f"{saludo} Aquí tienes la edición vigente del *PULSO de Acierta Max* 👇\n\n"
                 "¿Buscas propiedad o quieres revisar una antes de firmar? Visita https://acierta.pro o escribe VERIFICA.")
    else:
        texto = (f"{saludo} Ya estás suscrito al *PULSO de Acierta Max* ✅\n\n"
                 "Te mandaremos un resumen breve y visual con los datos inmobiliarios que importan para "
                 "comprar, vender, rentar o invertir en Guadalajara, con fuentes visibles. Sin saturarte.\n\n"
                 "Aquí va tu regalo: la edición más reciente 👇\n\n"
                 "📬 Cada nueva edición te llegará por correo (si nos lo diste) y cuando quieras la más "
                 "reciente por aquí, solo escribe *PULSO*.\n\n"
                 "🔎 Busca propiedades y conoce *ACIERTA VERIFICA* en https://acierta.pro\n"
                 "Para dejar de recibirlo, escribe *BAJA PULSO* cuando quieras.")
    mensajes = 1 if wati_send_text(phone, texto, copiar=False) else 0
    enviado = False
    if pdf and ed:
        time.sleep(0.6)
        enviado = wati_send_file(phone, pdf, ed.get("archivo") or "Pulso_Inmobiliario_acierta_pro.pdf",
                                 ed.get("titulo") or "PULSO de Acierta Max")
        mensajes += 1 if enviado else 0
    if not enviado and ed and ed.get("url"):
        mensajes += 1 if wati_send_text(phone, f"📄 Descárgalo aquí: {ed['url']}", copiar=False) else 0
    if mensajes:
        _pulso_metrica("MENSAJES_WHATSAPP", mensajes)
    return ed


def _pulso_entrante(phone, text, nombre_whatsapp=""):
    """Procesa 'PULSO', 'PULSO 1234' y 'BAJA PULSO'. True = ya se atendió."""
    t = (text or "").strip()
    if len(t) > 160:
        return False
    if _RE_PULSO_BAJA.match(t):
        threading.Thread(target=_pulso_guardar, args=(phone, {"ESTADO": "baja", "FECHA_BAJA": hora_gdl()}), daemon=True).start()
        threading.Thread(target=_pulso_wati_atributos, args=(phone, {"pulso": "no"}), daemon=True).start()
        wati_send_text(phone, "Listo, ya no te enviaremos el PULSO de Acierta Max. Si algún día quieres retomarlo, "
                              "escribe PULSO. ¡Gracias por leernos! 🙌", copiar=False)
        _pulso_metrica("BAJAS")
        _pulso_metrica("MENSAJES_WHATSAPP")
        print(f"[MAX-PULSO] Baja de {phone}", flush=True)
        return True
    m = _RE_PULSO.match(t)
    if not m:
        return False
    ahora = time.time()
    if ahora - _PULSO_RECIENTES.get(phone, 0) < 90:
        return True                              # reintento del webhook: ya se atendió
    _PULSO_RECIENTES[phone] = ahora
    tel = "521" + _ultimos10(phone)
    codigo = m.group(1)
    with _CAMINO_LOCK:
        pend = _PULSO_PEND.get(tel)
        usar = bool(pend and pend["expira"] > ahora and (not codigo or pend["codigo"] == codigo))
        if usar:
            _PULSO_PEND.pop(tel, None)
    datos = pend if usar else {}
    previo = None if usar else _pulso_fila(phone)
    if previo and previo.get("ESTADO") == "activo":
        # ya suscrito: le mandamos directo la edición vigente, sin repetir la bienvenida
        ed = _pulso_bienvenida(phone, previo.get("NOMBRE") or nombre_whatsapp, reenvio=True)
        threading.Thread(target=_pulso_guardar, args=(phone, {"ULTIMA_EDICION": (ed or {}).get("numero", "")}), daemon=True).start()
        print(f"[MAX-PULSO] Reenvío de la edición vigente a {phone}", flush=True)
        return True
    nombre = datos.get("nombre") or (previo or {}).get("NOMBRE") or (nombre_whatsapp or "").strip()
    ed = _pulso_bienvenida(phone, nombre)
    _pulso_metrica("ALTAS")
    correo = datos.get("correo") or (previo or {}).get("CORREO", "")
    if correo and ed:
        def _correo_bienvenida():
            _, pdf = _pulso_edicion()
            if _pulso_correo(correo, nombre, ed, pdf, phone, bienvenida=True):
                _pulso_guardar(phone, {"ULTIMA_EDICION_CORREO": str(ed.get("numero", ""))})
        threading.Thread(target=_correo_bienvenida, daemon=True).start()
    intereses = ", ".join(datos.get("intereses") or [])
    threading.Thread(target=_pulso_guardar, args=(phone, {
        "NOMBRE": nombre, "CORREO": datos.get("correo", ""), "INTERESES": intereses,
        "ORIGEN": "web" if usar else "whatsapp", "ESTADO": "activo", "FECHA_CONFIRMACION": hora_gdl(),
        "ULTIMA_EDICION": (ed or {}).get("numero", ""), "AVISO_VERSION": datos.get("aviso", "")}), daemon=True).start()
    atributos = {"pulso": "si", "pulso_alta": hora_gdl()[:10]}
    if intereses:
        atributos["pulso_intereses"] = intereses
    threading.Thread(target=_pulso_wati_atributos, args=(phone, atributos, nombre), daemon=True).start()
    print(f"[MAX-PULSO] Suscripción activa: {phone} ({'web' if usar else 'whatsapp'})", flush=True)
    return True


# ------------------------------------------------------------------
# PORTAL DEL COACH (acierta.pro/asesor) — propuesta personalizada en PDF
# Pedido por Javier (4-oct-2026): el coach entra con su contraseña, busca
# al cliente por WhatsApp en el CRM, elige propiedades (con resumen y
# ventajas que él revisa) y se genera una carta PDF con mapa que se envía
# al cliente por WhatsApp, con copia al coach y a Javier.
# Contraseñas: variables de entorno en Render, una por coach:
#   CLAVE_ASESOR_JAVIER, CLAVE_ASESOR_UBALDO, CLAVE_ASESOR_LETICIA, ...
# (nunca en el código). Sin su variable, ese coach no puede entrar.
# ------------------------------------------------------------------
import base64 as _b64
import unicodedata as _ud


def _asesor_clave_usuario(nombre):
    primero = (nombre or "").split()[0] if nombre else ""
    return _ud.normalize("NFKD", primero).encode("ascii", "ignore").decode().lower()


def _asesores():
    out = {}
    for v in VENDEDORES:
        u = _asesor_clave_usuario(v["nombre"])
        out[u] = {"usuario": u, "nombre": v["nombre"], "telefono": v["phone"],
                  "clave": os.environ.get(f"CLAVE_ASESOR_{u.upper()}", "")}
    return out


def _asesor_llave():
    return (os.environ.get("ASESOR_SECRET") or os.environ.get("CAMINO_SECRET")
            or os.environ.get("WATI_API_KEY") or "asesor").encode()


def _asesor_token(usuario, horas=12):
    cuerpo = _b64.urlsafe_b64encode(json.dumps({"u": usuario, "exp": int(time.time() + horas * 3600)}).encode()).decode()
    firma = hmac.new(_asesor_llave(), cuerpo.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{cuerpo}.{firma}"


def _asesor_de_token(token):
    try:
        cuerpo, firma = str(token or "").split(".", 1)
        if not hmac.compare_digest(firma, hmac.new(_asesor_llave(), cuerpo.encode(), hashlib.sha256).hexdigest()[:32]):
            return None
        d = json.loads(_b64.urlsafe_b64decode(cuerpo.encode()).decode())
        if d.get("exp", 0) < time.time():
            return None
        a = _asesores().get(d.get("u"))
        return a if a and a["clave"] else None
    except Exception:
        return None


def _asesor_json(limite=200_000):
    if (request.content_length or 0) > limite:
        return None
    return request.get_json(silent=True)


# ==================================================================
# VERI · asistente experto de Acierta Verifica (verifica.casa)
# Guía al técnico en campo y revisa zonas, mediciones, documentos y el
# reporte. Entra con la misma clave del portal de coaches (token de
# /api/asesor/login). Las fotos y documentos se procesan y NO se guardan.
# ==================================================================
VERI_SISTEMA = """Eres VERI, el asistente experto de Acierta Verifica (división de Acierta Max, Guadalajara, Jalisco).
Acompañas en campo al técnico que hace una revisión física y documental PREVENTIVA de un inmueble, desde verifica.casa.
Combinas el criterio de un ingeniero civil, arquitecto, electricista, plomero/fontanero, especialista en gas, impermeabilizador,
jardinero, abogado inmobiliario y gestor de trámites en Jalisco.

TU TRABAJO
- Llevar de la mano al técnico: instrucciones cortas, numeradas, concretas, sin tecnicismos innecesarios. Que no tenga que pensar.
- Revisar lo que te manda (marcas, notas, mediciones, fotos y documentos) y decir: qué está bien, qué es incongruente,
  qué falta (foto, medición, ángulo, referencia de tamaño, punto sano de comparación, documento) y qué hacer después.
- Detectar incongruencias: p. ej. punto marcado «Bien» pero la foto muestra mancha, grieta o corrosión; humedad reportada sin
  lectura del punto sano; presión declarada con hidroneumático pero lectura de tinaco; superficie de escritura distinta a la construida.
- Nunca inventar. Si la foto no deja ver algo, dilo y pide otra. Escribe «se observa», «probable», «se recomienda revisión por especialista».
- Seguridad primero: ante fuga de gas, CO alto, punto caliente eléctrico o riesgo estructural, ordena detener la prueba, ventilar/alejarse y avisar.
- Alcance: revisión preventiva básica; no es peritaje, avalúo ni opinión jurídica. En temas legales complejos: canalizar a notario o abogado.

EQUIPO DEL TÉCNICO Y USO
- Cámara termográfica UNI-T UTi120S+: barrer muros y plafones; mancha fría irregular >3 °C vs. el muro = observación; en tablero, pastilla/cable >10 °C sobre sus vecinos = crítico. Siempre par de fotos (normal + térmica).
- Medidor de humedad sin agujas WM710: modo del material; medir punto sospechoso y punto sano del mismo muro; vale la comparación.
- Manómetro CINACE 0–200 PSI: en llave de jardín o lavadora con teflón; planta baja y punto más alto. Tinaco 5–15 PSI; hidroneumático 20–60; >80 riesgo; si con dos salidas cae a la mitad = observación.
- Probador de contactos ST05B: polaridad, tierra y voltaje (114–135 V; nominal 127 V).
- Multímetro de gancho UNI-T UT202+: voltaje en tablero, corriente de un conductor; sin abrir tapas con herramienta.
- Detector de CO HABOTEST HT6020: a 1 m del calentador y la estufa encendidos; 0–9 ppm normal; >35 ppm crítico.
- Detector de gas combustible HABOTEST HT609Pro: encender afuera y esperar calibración; pasar a 1–2 cm de regulador, llaves, mangueras y conexiones; alarma = confirmar con agua jabonosa; fuga confirmada = crítico.
- Endoscopio 10 m: registros, drenajes, ductos, plafón; nunca perforar.
- Nivel láser 360° 16 líneas: 4 esquinas; >10 mm en 3 m = observación; >20 mm con grietas = crítico.
- Linternas, espejo telescópico, herramienta manual.
- Grietas: medir largo y ancho; diagonal desde esquinas de vanos, >3 mm, o con desnivel = crítico y perito estructural.

PROCEDIMIENTO (casa tipo, de afuera hacia adentro y de arriba hacia abajo)
Exterior y fachada → azotea (impermeabilizante, coladeras, tinaco) → estacionamiento → cisterna/hidroneumático → gas → tablero →
planta alta (recámaras, baños, escalera) → planta baja (sala-comedor, cocina, medio baño) → lavandería, cuarto de servicio, terraza →
drenaje y registros. Evidencia: foto general de cada zona, detalle con referencia de tamaño, par térmico, foto del instrumento con la lectura.

SEMÁFORO
Verde: sin observaciones relevantes. Amarillo: reparar o aclarar antes de firmar, sin riesgo inmediato. Rojo: riesgo para personas,
costo alto o impedimento legal (fuga de gas, CO >35 ppm, punto caliente, grieta estructural activa, gravamen vigente, vendedor sin facultades).

DOCUMENTAL Y LEGAL (Jalisco)
Revisa: identificación del vendedor (vigente, nombre = escritura); escritura (ubicación, superficie, medidas y colindancias vs. lo observado;
diferencia >10% = observación); inscripción en el RPP (folio real); certificado de libertad o gravamen (reciente, idealmente <30 días; hipoteca o
embargo = rojo); estado civil y régimen (sociedad conyugal: firma el cónyuge); poder notarial (vigente, facultades de dominio); sucesión
(adjudicación concluida); predial al corriente; agua (SIAPA o municipal) sin adeudo; CFE; cuotas de condominio o colonos (constancia del
administrador); régimen de condominio y reglamento; licencias de construcción y habitabilidad; uso de suelo; ocupación e inquilinos.
Al leer un documento: extrae datos clave (titular, fechas, folio, superficie, adeudos, gravámenes), compáralos con el expediente, señala
alteraciones visibles, ilegibilidad, vencimientos y lo que falta. No afirmes autenticidad: solo lo que se ve.
Trámites útiles 2026: consulta de folio RPP $55; certificado de libertad o gravamen $714 (5 días hábiles, urgente 24 h al doble);
vigencia INE en listanominal.ine.mx (gratis); RENAP y RENAT solo los consulta el notario; no existe validación pública de pasaportes.

FORMATO DE RESPUESTA
Responde SOLO con un objeto JSON válido, sin texto antes ni después, con estas claves:
{"estado": "listo" | "faltan_datos" | "alerta",
 "mensaje": "2 a 4 frases claras para el técnico",
 "faltantes": ["acción concreta que falta (foto, medición, documento)"],
 "hallazgos": [{"nivel": "verde" | "amarillo" | "rojo", "texto": "hallazgo con lugar y evidencia"}],
 "pasos": ["siguiente paso 1", "siguiente paso 2"],
 "borrador": "solo en modo reporte: texto sugerido; en otros modos, cadena vacía"}
Español de México, tono profesional y amable, frases cortas."""

VERI_MODOS = {
    "zona": "Revisa esta ZONA: puntos marcados, notas y fotos. Señala incongruencias entre marcas y fotos, y lo que falta antes de cerrarla.",
    "pruebas": "Revisa las PRUEBAS Y MEDICIONES: coherencia, rangos, pruebas sin realizar que sí aplican, y qué repetir.",
    "documento": "Revisa este DOCUMENTO: extrae los datos clave, compáralos con el expediente y di qué está bien, qué es alerta y qué falta.",
    "reporte": "Revisa el EXPEDIENTE COMPLETO antes del reporte. En 'borrador' escribe: RESUMEN EJECUTIVO (3 a 5 hallazgos que más importan, numerados) y luego RECOMENDACIONES (qué reparar, qué aclarar con el vendedor, a qué especialista acudir). Separa ambas partes con la línea '---'.",
    "guia": "El técnico pide guía. Dale instrucciones paso a paso, exactas, para lo que pregunta (instrumento, punto o zona).",
    "chat": "Responde la pregunta del técnico con criterio experto y pasos concretos.",
}


def _veri_bloques_adjuntos(imagenes, documentos):
    bloques = []
    for img in (imagenes or [])[:8]:
        m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", str(img or ""), re.S)
        if m and len(m.group(2)) < 2_500_000:
            bloques.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
    for doc in (documentos or [])[:4]:
        m = re.match(r"^data:(application/pdf|image/(?:jpeg|png|webp));base64,(.+)$", str((doc or {}).get("data") or ""), re.S)
        if not m or len(m.group(2)) > 6_000_000:
            continue
        if m.group(1) == "application/pdf":
            bloques.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": m.group(2)}})
        else:
            bloques.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
    return bloques


def _veri_json(texto):
    t = (texto or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"estado": "listo", "mensaje": t[:1500], "faltantes": [], "hallazgos": [], "pasos": [], "borrador": ""}


@app.route("/api/veri", methods=["POST", "OPTIONS"])
def api_veri():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _asesor_json(26_000_000) or {}
    a = _asesor_de_token(d.get("token"))
    if not a:
        return _camino_resp({"ok": False, "error": "Tu sesión venció. Vuelve a entrar con tu clave."}, 401)
    if not _camino_limite("veri:" + a["usuario"], 120, 3600):
        return _camino_resp({"ok": False, "error": "Muchas consultas en poco tiempo. Espera unos minutos."}, 429)
    modo = d.get("modo") if d.get("modo") in VERI_MODOS else "chat"
    contexto = json.dumps(d.get("contexto") or {}, ensure_ascii=False)[:30_000]
    pregunta = str(d.get("pregunta") or "")[:2000]
    historial = [h for h in (d.get("historial") or [])[-8:] if isinstance(h, dict)]
    contenido = _veri_bloques_adjuntos(d.get("imagenes"), d.get("documentos"))
    contenido.append({"type": "text", "text":
        f"MODO: {modo}. {VERI_MODOS[modo]}\n\nCONTEXTO DEL EXPEDIENTE (JSON):\n{contexto}\n\n"
        + (f"PREGUNTA O NOTA DEL TÉCNICO: {pregunta}\n" if pregunta else "")
        + f"Adjuntos: {len(contenido)} (fotos o documentos)."})
    mensajes = []
    for h in historial:
        rol = "assistant" if h.get("rol") == "veri" else "user"
        txt = str(h.get("texto") or "")[:1500]
        if txt:
            if mensajes and mensajes[-1]["role"] == rol:
                mensajes[-1]["content"] += "\n" + txt
            else:
                mensajes.append({"role": rol, "content": txt})
    if mensajes and mensajes[0]["role"] == "assistant":
        mensajes.insert(0, {"role": "user", "content": "(inicio)"})
    if mensajes and mensajes[-1]["role"] == "user":
        mensajes.append({"role": "assistant", "content": "Entendido."})
    mensajes.append({"role": "user", "content": contenido})
    try:
        r = requests.post(ANTHROPIC_API, timeout=80, headers={
            "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={"model": CLAUDE_MODEL, "max_tokens": 1800, "system": VERI_SISTEMA, "messages": mensajes})
        r.raise_for_status()
        texto = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    except Exception as e:
        print(f"[VERI] Error con Claude ({a['nombre']}, {modo}): {e}", flush=True)
        return _camino_resp({"ok": False, "error": "VERI no pudo responder en este momento. Intenta de nuevo."}, 502)
    res = _veri_json(texto)
    print(f"[VERI] {a['nombre']} · {modo} · adjuntos {len(contenido) - 1} · estado {res.get('estado')}", flush=True)
    return _camino_resp({"ok": True, "veri": res})


@app.route("/api/asesor/usuarios", methods=["POST", "OPTIONS"])
def api_asesor_usuarios():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    lista = [{"usuario": a["usuario"], "nombre": a["nombre"], "activo": bool(a["clave"])} for a in _asesores().values()]
    return _camino_resp({"ok": True, "usuarios": lista})


@app.route("/api/asesor/login", methods=["POST", "OPTIONS"])
def api_asesor_login():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _asesor_json(4096) or {}
    if not _camino_limite("asesorlogin:ip:" + _camino_ip(), 10, 900):
        return _camino_resp({"ok": False, "error": "Demasiados intentos. Espera 15 minutos."}, 429)
    a = _asesores().get(str(d.get("usuario") or "").lower())
    clave = str(d.get("clave") or "")
    if not a or not a["clave"] or not hmac.compare_digest(clave.encode(), a["clave"].encode()):
        print(f"[MAX-ASESOR] Intento de acceso fallido para '{d.get('usuario')}' desde {_camino_ip()}", flush=True)
        return _camino_resp({"ok": False, "error": "Usuario o contraseña incorrectos."}, 401)
    print(f"[MAX-ASESOR] Acceso de {a['nombre']}", flush=True)
    return _camino_resp({"ok": True, "token": _asesor_token(a["usuario"]), "nombre": a["nombre"]})


def _crm_buscar_por_tel10(tel10):
    """Expediente más reciente del cliente (abierto primero), comparando por
    los últimos 10 dígitos para no fallar por el prefijo 52/521."""
    sh = _crm_sheet()
    valores = sh.get_all_values()
    if len(valores) < 2:
        return None
    headers = valores[0]
    cerrado = None
    for idx in range(len(valores) - 1, 0, -1):
        fila = _crm_fila_a_dict(headers, valores[idx])
        if _ultimos10(fila.get("TELEFONO_CLIENTE")) == tel10:
            fila["_fila"] = idx + 1
            if fila.get("CONCLUIDO") != "Si":
                return fila
            cerrado = cerrado or fila
    return cerrado


@app.route("/api/asesor/cliente", methods=["POST", "OPTIONS"])
def api_asesor_cliente():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _asesor_json(4096) or {}
    a = _asesor_de_token(d.get("token"))
    if not a:
        return _camino_resp({"ok": False, "error": "Tu sesión venció. Vuelve a entrar."}, 401)
    tel = _camino_tel(d.get("whatsapp"))
    if not tel:
        return _camino_resp({"ok": False, "error": "Escribe el WhatsApp del cliente a 10 dígitos."}, 400)
    try:
        fila = _crm_buscar_por_tel10(_ultimos10(tel)) if (GOOGLE_CREDS_JSON and SHEET_ID) else None
    except Exception as e:
        print(f"[MAX-ASESOR] Error leyendo CRM: {e}", flush=True)
        fila = None
    if not fila:
        return _camino_resp({"ok": True, "encontrado": False})
    ebs = list(dict.fromkeys(re.findall(r"(?:EB|NJ)-[A-Z0-9]{4,8}", fila.get("PROPIEDADES", "") or "")))
    return _camino_resp({"ok": True, "encontrado": True,
                         "nombre": fila.get("NOMBRE_CLIENTE", ""), "folio": fila.get("FOLIO", ""),
                         "vendedor": fila.get("VENDEDOR", ""), "operacion": fila.get("OPERACION", ""),
                         "fase": fila.get("FASE", ""), "concluido": fila.get("CONCLUIDO") == "Si",
                         "propiedades": ebs, "carpeta": fila.get("CARPETA_CLIENTE_URL", ""),
                         "contexto": (fila.get("CHAT_COMPLETO", "") or "")[-1500:]})


_EB_CACHE_PROPS = {}


def _prop_de_easybroker(eb, operacion=""):
    """Propiedad que no está en el inventario del sitio (por ejemplo, publicada en
    el panel de EasyBroker pero no en aciertamax.com): se toma de la API de
    EasyBroker, primero propias y luego la bolsa MLS. None si no se encuentra."""
    eb = (eb or "").upper().strip()
    if not (EASYBROKER_API_KEY and re.match(r"^EB-[A-Z0-9]{5,8}$", eb)):
        return None
    clave = (eb, operacion.upper())
    if clave in _EB_CACHE_PROPS and time.time() - _EB_CACHE_PROPS[clave][0] < 1800:
        return _EB_CACHE_PROPS[clave][1]
    j = None
    for ruta in (f"/properties/{eb}", f"/mls_properties/{eb}"):
        try:
            r = requests.get(EB_API + ruta, headers=eb_headers(), timeout=12)
            if r.status_code == 200:
                j = r.json()
                break
        except Exception as e:
            print(f"[MAX-ASESOR] Error consultando {eb} en EasyBroker: {e}", flush=True)
    if not j:
        print(f"[MAX-ASESOR] {eb} no está en el inventario ni en la API de EasyBroker", flush=True)
        return None
    ops = j.get("operations") or []
    tipo_op = {"sale": "VENTA", "rental": "RENTA", "temporary_rental": "RENTA"}
    op_elegida = None
    for o in ops:
        if not operacion or tipo_op.get(o.get("type")) == operacion.upper():
            op_elegida = o
            break
    op_elegida = op_elegida or (ops[0] if ops else {})
    loc = j.get("location") or {}
    nombre_loc = loc.get("name", "") if isinstance(loc, dict) else str(loc)
    partes = [x.strip() for x in nombre_loc.split(",") if x.strip()]
    imgs = j.get("property_images") or []
    foto = j.get("title_image_full") or (imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else "")
    prop = {"eb": eb, "operacion": tipo_op.get(op_elegida.get("type"), operacion.upper() or "VENTA"),
            "titulo": j.get("title", ""), "tipo": (j.get("property_type") or "").lower(),
            "precio": op_elegida.get("amount"), "moneda": op_elegida.get("currency", "MXN"),
            "municipio": partes[1] if len(partes) > 1 else "", "colonia": partes[0] if partes else "",
            "recamaras": j.get("bedrooms"), "banos": j.get("bathrooms"),
            "m2": j.get("construction_size") or j.get("lot_size"), "foto": foto,
            "lat": loc.get("latitude") if isinstance(loc, dict) else None,
            "lon": loc.get("longitude") if isinstance(loc, dict) else None,
            "liga": j.get("public_url", ""), "fuera_de_inventario": True}
    _EB_CACHE_PROPS[clave] = (time.time(), prop)
    return prop


def _prop_de_inventario(eb, operacion=""):
    eb = (eb or "").upper().strip()
    candidatos = [p for p in INVENTARIO_ZMG if (p.get("codigo_eb") or "").upper() == eb]
    if operacion:
        mismos = [p for p in candidatos if (p.get("Operación") or "").upper() == operacion.upper()]
        candidatos = mismos or candidatos
    if not candidatos:
        return _prop_de_easybroker(eb, operacion)
    p = candidatos[0]
    return {"eb": eb, "operacion": p.get("Operación", ""), "titulo": p.get("Título/Colonia", ""),
            "tipo": p.get("Tipo", ""), "precio": p.get("Precio"), "municipio": p.get("Municipio", ""),
            "colonia": p.get("Colonia", ""), "recamaras": p.get("Recámaras"), "banos": p.get("Baños"),
            "m2": p.get("m²"), "foto": p.get("Foto", ""), "lat": p.get("lat"), "lon": p.get("lon"),
            "liga": p.get("Liga", "")}


def _eb_json_a_detalle(j):
    loc = j.get("location")
    mant = j.get("expenses") or j.get("maintenance_fee")
    if isinstance(mant, (int, float)) and mant:
        mant = f"${mant:,.0f} al mes"
    return {
        "titulo": j.get("title"), "descripcion": j.get("description"),
        "ubicacion": loc.get("name") if isinstance(loc, dict) else loc,
        "recamaras": j.get("bedrooms"), "banos": j.get("bathrooms"), "medio_banos": j.get("half_bathrooms"),
        "estacionamientos": j.get("parking_spaces"), "construccion_m2": j.get("construction_size"),
        "terreno_m2": j.get("lot_size"), "niveles": j.get("floors"), "antiguedad": j.get("age"),
        "mantenimiento": mant,
        "amenidades": [f.get("name") if isinstance(f, dict) else str(f) for f in (j.get("features") or [])],
        "fotos": [im.get("url") if isinstance(im, dict) else str(im) for im in (j.get("property_images") or [])],
        "fuente": "api",
    }


def _eb_detalle_completo(p):
    """Ficha completa para el anexo del PDF. 1) API de EasyBroker (propias de
    Acierta Max); 2) API de la bolsa MLS (compartidas); 3) página pública en
    aciertamax.com (descripción y fotos). Lo que no se encuentre, no se pone."""
    eb = (p.get("eb") or "").upper()
    if EASYBROKER_API_KEY and eb:
        for ruta in (f"/properties/{eb}", f"/mls_properties/{eb}"):
            try:
                r = requests.get(EB_API + ruta, headers=eb_headers(), timeout=10)
                if r.status_code == 200:
                    return _eb_json_a_detalle(r.json())
            except Exception:
                pass
    liga = p.get("liga")
    if not liga:
        return {}
    try:
        r = requests.get(liga, timeout=12, headers={"User-Agent": "Mozilla/5.0 (AciertaMax propuestas)"})
        if r.status_code != 200:
            return {}
        html = r.text
        import html as _html
        desc = ""
        for bloque in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
            try:
                dj = json.loads(bloque)
                for o in (dj if isinstance(dj, list) else [dj]):
                    if isinstance(o, dict) and len(str(o.get("description", ""))) > len(desc):
                        desc = str(o["description"])
            except Exception:
                pass
        if len(desc) < 80:
            m = re.search(r'<meta[^>]+(?:property|name)="(?:og:)?description"[^>]+content="([^"]*)"', html) or \
                re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="(?:og:)?description"', html)
            if m and len(m.group(1)) > len(desc):
                desc = m.group(1)
        fotos, vistos = [], set()
        for u in re.findall(r'https://assets\.easybroker\.com/property_images/[^"\'\s)<>]+', html):
            u = _html.unescape(u)
            clave = u.split("?")[0]
            if clave not in vistos and re.search(r"\.(jpe?g|png|webp)$", clave, re.I):
                vistos.add(clave)
                fotos.append(u)
        return {"descripcion": _html.unescape(desc), "fotos": fotos[:6], "fuente": "html"}
    except Exception as e:
        print(f"[MAX-ASESOR] No se pudo leer la ficha de {eb}: {e}", flush=True)
        return {}


@app.route("/api/asesor/propiedad", methods=["POST", "OPTIONS"])
def api_asesor_propiedad():
    """El portal la usa para agregar una clave EB que no está en el inventario del sitio."""
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _asesor_json(4096) or {}
    if not _asesor_de_token(d.get("token")):
        return _camino_resp({"ok": False, "error": "Tu sesión venció. Vuelve a entrar."}, 401)
    p = _prop_de_inventario(d.get("eb"), d.get("operacion", ""))
    if not p:
        return _camino_resp({"ok": False, "error": "No encontré esa clave ni en el inventario ni en EasyBroker."}, 404)
    return _camino_resp({"ok": True, "propiedad": p})


_PROPUESTAS_PDF = {}   # id -> (bytes, nombre_archivo, ts) respaldo si Drive no está disponible


@app.route("/api/asesor/pdf/<pid>", methods=["GET"])
def api_asesor_pdf(pid):
    item = _PROPUESTAS_PDF.get(pid)
    if not item or time.time() - item[2] > 30 * 86400:
        return "Esta propuesta ya no está disponible. Pídele a tu coach que te la reenvíe.", 404
    resp = app.response_class(item[0], mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="{item[1]}"'
    return resp


def wati_send_file(phone, contenido, nombre_archivo, caption="", mime="application/pdf"):
    phone_norm = _normalizar_phone_wati(phone)
    try:
        r = requests.post(f"{WATI_BASE_URL}/api/v1/sendSessionFile/{phone_norm}", headers=wati_headers(),
                          params={"caption": caption[:1000]},
                          files={"file": (nombre_archivo, io.BytesIO(contenido), mime)}, timeout=60)
        ok = r.status_code in (200, 201)
        try:
            cuerpo = r.json()
            if isinstance(cuerpo, dict) and cuerpo.get("result") is False:
                ok = False
        except Exception:
            cuerpo = r.text[:200]
        print(f"[MAX-WATI] Archivo a {phone_norm}: status={r.status_code} ok={ok} cuerpo={str(cuerpo)[:200]}", flush=True)
        return ok
    except Exception as e:
        print(f"[MAX-WATI] Error enviando archivo a {phone_norm}: {e}", flush=True)
        return False


def _drive_subir_pdf_publico(contenido, nombre_archivo, folio, nombre_cliente):
    from googleapiclient.http import MediaIoBaseUpload
    carpeta_id, _ = _drive_carpeta_cliente(folio or "SIN-FOLIO", nombre_cliente or "cliente")
    servicio = _drive_client()
    f = servicio.files().create(body={"name": nombre_archivo, "parents": [carpeta_id]},
                                media_body=MediaIoBaseUpload(io.BytesIO(contenido), mimetype="application/pdf"),
                                fields="id, webViewLink").execute()
    servicio.permissions().create(fileId=f["id"], body={"type": "anyone", "role": "reader"}).execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"


@app.route("/api/asesor/propuesta", methods=["POST", "OPTIONS"])
def api_asesor_propuesta():
    if request.method == "OPTIONS":
        return _camino_resp({}, 204)
    d = _asesor_json() or {}
    a = _asesor_de_token(d.get("token"))
    if not a:
        return _camino_resp({"ok": False, "error": "Tu sesión venció. Vuelve a entrar."}, 401)
    modo = d.get("modo") if d.get("modo") in ("vista", "enviar") else "vista"
    cli = d.get("cliente") or {}
    nombre = _camino_limpiar(cli.get("nombre"), 80)
    tel = _camino_tel(cli.get("whatsapp"))
    if len(nombre) < 2:
        return _camino_resp({"ok": False, "error": "Escribe el nombre del cliente."}, 400)
    if modo == "enviar" and not tel:
        return _camino_resp({"ok": False, "error": "Falta el WhatsApp del cliente."}, 400)
    if not _camino_limite("asesorpdf:" + a["usuario"], 40, 3600):
        return _camino_resp({"ok": False, "error": "Demasiadas propuestas en poco tiempo. Espera un momento."}, 429)
    props, faltan = [], []
    for item in (d.get("propiedades") or [])[:12]:
        base = _prop_de_inventario(item.get("eb"), item.get("operacion", ""))
        if not base:
            faltan.append(str(item.get("eb")))
            continue
        base["resumen"] = _camino_limpiar(item.get("resumen"), 600)
        base["ventajas"] = [_camino_limpiar(str(v).strip(" •-*\t"), 160).lstrip("'")
                            for v in (item.get("ventajas") or [])[:4] if str(v).strip(" •-*\t")]
        base["resumen"] = base["resumen"].lstrip("'")
        props.append(base)
    if not props:
        return _camino_resp({"ok": False, "error": "Agrega al menos una propiedad del inventario."}, 400)
    import propuesta as _prop
    datos = {"cliente": {"nombre": nombre, "folio": _camino_limpiar(cli.get("folio"), 40)},
             "coach": {"nombre": a["nombre"], "telefono": _formato_tel_humano(a["telefono"]),
                       "cargo": "Director General" if a["usuario"] == "javier" else "Coach inmobiliario"},
             "trato": "usted" if d.get("trato") == "usted" else "tu",
             "genero": d.get("genero") if d.get("genero") in ("f", "m") else "",
             "vio_en_sitio": bool(d.get("vio_en_sitio")),
             "criterios": _camino_limpiar(d.get("criterios"), 300), "nota": _camino_limpiar(d.get("nota"), 900),
             "propiedades": props}
    try:
        datos["anexo"] = d.get("anexo", True) is not False
        pdf = _prop.generar_pdf(datos, detalle_fn=_eb_detalle_completo)
    except Exception as e:
        print(f"[MAX-ASESOR] Error generando PDF: {e}", flush=True)
        return _camino_resp({"ok": False, "error": "No se pudo generar el PDF. Intenta de nuevo."}, 500)
    archivo = _prop.nombre_archivo(nombre)
    if modo == "vista":
        resp = app.response_class(pdf, mimetype="application/pdf")
        resp.headers["Content-Disposition"] = f'inline; filename="{archivo}"'
        origen = request.headers.get("Origin", "")
        if origen in CAMINO_ORIGENES:
            resp.headers["Access-Control-Allow-Origin"] = origen
            resp.headers["Vary"] = "Origin"
        if faltan:
            resp.headers["X-Propiedades-Omitidas"] = ",".join(faltan)
        return resp

    # --- ENVIAR ---
    folio = datos["cliente"]["folio"]
    liga = ""
    try:
        if GOOGLE_CREDS_JSON:
            liga = _drive_subir_pdf_publico(pdf, archivo, folio, nombre)
    except Exception as e:
        print(f"[MAX-ASESOR] No se pudo subir a Drive ({e}); se usa liga de respaldo", flush=True)
    if not liga:
        pid = hashlib.sha256(os.urandom(24)).hexdigest()[:28]
        _PROPUESTAS_PDF[pid] = (pdf, archivo, time.time())
        liga = f"{request.host_url.rstrip('/').replace('http://', 'https://')}/api/asesor/pdf/{pid}"
    primer = nombre.split()[0]
    n = len(props)
    claves = ", ".join(p["eb"] for p in props)
    caption = (f"Hola {primer}, soy {a['nombre']}, tu coach de Acierta Max. Te comparto la propuesta con "
               f"{n} propiedad{'es' if n != 1 else ''} que seleccioné para ti ({claves}). "
               f"Respóndeme con las claves que te interesen y agendamos tu visita.")
    via = "archivo"
    cliente_ok = wati_send_file(tel, pdf, archivo, caption)
    if not cliente_ok:
        via = "plantilla"
        cliente_ok = wati_send_template_message(tel, "seguimiento_cliente", [
            f"{primer}, tu coach {a['nombre']} te preparó una propuesta con {n} propiedad{'es' if n != 1 else ''} "
            f"({claves}). Puedes verla aquí: {liga}"])
    aviso = (f"📄 PROPUESTA ENVIADA — {folio or 'sin folio'}\n\nCliente: {nombre} ({tel})\n"
             f"Coach: {a['nombre']}\nPropiedades: {claves}\n"
             f"Entrega al cliente: {'✅ ' + ('PDF por WhatsApp' if via == 'archivo' else 'liga por plantilla') if cliente_ok else '❌ no se pudo entregar'}\n"
             f"PDF: {liga}")
    coach_ok = wati_send_file(a["telefono"], pdf, archivo, f"Copia de la propuesta para {nombre} ({claves})")
    if not coach_ok:
        coach_ok = notificar_interno(a["telefono"], aviso, f"Propuesta para {nombre} | {claves} | PDF: {liga}")
    if _ultimos10(a["telefono"]) != _ultimos10(JAVIER_PHONE):
        notificar_interno(JAVIER_PHONE, aviso, f"Propuesta de {a['nombre']} a {nombre} | {claves} | PDF: {liga}")
    try:
        _actividad_sheet().append_row([hora_gdl(), a["telefono"], "propuesta", folio,
                                       f"{nombre} ({tel}) · {claves} · {'entregada' if cliente_ok else 'NO entregada'} · {liga}"[:500]])
        if folio and GOOGLE_CREDS_JSON:
            fila = _crm_buscar_por_tel10(_ultimos10(tel))
            if fila and fila.get("FOLIO") == folio:
                col = {h: i + 1 for i, h in enumerate(CRM_COLUMNAS)}
                _crm_sheet().update_cell(fila["_fila"], col["ULTIMA_ACCION"], f"{hora_gdl()} propuesta PDF ({claves})")
    except Exception as e:
        print(f"[MAX-ASESOR] No se pudo registrar la propuesta en el CRM: {e}", flush=True)
    print(f"[MAX-ASESOR] Propuesta de {a['nombre']} a {tel}: cliente_ok={cliente_ok} via={via} coach_ok={coach_ok}", flush=True)
    return _camino_resp({"ok": True, "cliente_ok": cliente_ok, "via": via, "coach_ok": coach_ok,
                         "liga": liga, "omitidas": faltan})


def _formato_tel_humano(tel):
    d = _ultimos10(tel)
    return f"{d[:2]} {d[2:6]} {d[6:]}" if len(d) == 10 else str(tel or "")


@app.route("/health", methods=["GET"])
def health():
    """Para UptimeRobot (evita cold start de Render)."""
    return jsonify(status="ok", agente="MAX 2.0", ts=time.time())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
