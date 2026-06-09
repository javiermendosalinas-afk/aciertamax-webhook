from flask import Flask, request, jsonify
import anthropic
import requests
import os
import json
import random
import string
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WATI_API_KEY = os.environ.get("WATI_API_KEY")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")
SHEET_ID = "1iUQff9LpajS0dOGEYDg260eHTjuBulfSIMl2L96UvpE"

def get_sheets_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def generate_folio():
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(random.choices(chars, k=4))
    return "BV-MUNDIAL-" + suffix

def save_to_sheets(folio, nombre, whatsapp, correo):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).sheet1
        now = datetime.now()
        fecha = now.strftime("%d/%m/%Y")
        hora = now.strftime("%H:%M")
        sheet.append_row([folio, nombre, whatsapp, correo, fecha, hora, "REGISTRADO"])
        print("Saved to sheets: " + folio)
        return True
    except Exception as e:
        print("Error saving to sheets: " + str(e))
        return False

CONTEXT = ("CRITICO: SOLO ESPANOL. PROHIBIDO usar ingles. PROHIBIDO decir Hello, Hi, Welcome, Thank you. "
"Si recibes mensaje en ingles, respondes en ESPANOL. "
"Tu primer mensaje SIEMPRE es exactamente: Hola soy MAX tu asesor de BellaVittoria en que te puedo ayudar "
"Eres MAX asistente de Acierta Max experto en BellaVittoria Tlaquepaque. "
"CAMPANA MUNDIALISTA URGENTE PARTIDO JUEVES 13:00 HRS: "
"7 departamentos Piso 3 precio especial $3,520,000 MXN valor avaluo certificado. "
"BONO: Si Mexico GANA vs Sudafrica jueves: BONO $300,000 MXN. Si Mexico EMPATA: BONO $150,000 MXN. "
"REGISTRO ANTES DE LAS 13:00 HRS DEL JUEVES. "
"PROCESO DE REGISTRO: cuando cliente quiera registrarse, pide NOMBRE COMPLETO primero. "
"Luego pide CORREO ELECTRONICO. "
"Cuando tengas nombre y correo escribe exactamente en tu respuesta: ##REGISTRAR##nombre##correo## "
"y nada mas en esa respuesta. El sistema procesara el registro automaticamente. "
"UBICACION: Cobre 4232 Lomas de la Victoria San Pedro Tlaquepaque. "
"Esquina Cruz del Sur y Av Conchitas. 7 minutos Plaza del Sol dentro de Periferico. "
"Maps: https://maps.app.goo.gl/A4RyZxXK5Dk7N6R36 "
"PRECIO REGULAR: desde $3,400,000 MXN sujeto a cambio sin previo aviso. "
"Avaluo certificado $3,572,000 MXN. Enganche 50% $1,700,000. Mensualidad $18,700 mes. "
"Bancos BBVA Santander Banorte Scotiabank HSBC. Cofinavit disponible. NO Fovissste. "
"DEPARTAMENTOS: 8 modelos 70-78 m2 2 recamaras 2 banos cocina granito porcelanato techos altos tabique macizo. "
"DISPONIBILIDAD: 20 departamentos entrega INMEDIATA 14 familias ya viven ahi. "
"RENTA amueblado $18,000 mes mas $1,500 mantenimiento. Sin amueblar $15,000 mas $1,500. "
"AMENIDADES: seguridad 24/7 lobby hotel coworking roof garden asadores area infantil estacionamiento elevador. "
"HORARIO: Lun-Vie 10am-5pm Sab-Dom 10am-3pm. Tel 3344441444. "
"CALENDLY: https://calendly.com/javiermendosalinas/30min "
"REGLAS: SIEMPRE espanol, mensajes cortos max 3 parrafos, precio con sujeto a cambio sin previo aviso, "
"no menciones Fovissste, cuida ortografia, invita siempre a registrarse para el bono mundialista.")

conversation_history = {}

def send_wati_message(phone, message):
    url = "https://live-mt-server.wati.io/437629/api/v1/sendSessionMessage/" + phone + "?messageText=" + requests.utils.quote(message)
    headers = {"Authorization": "Bearer " + WATI_API_KEY}
    try:
        response = requests.post(url, headers=headers)
        print("Wati response: " + str(response.status_code))
        return response.text
    except Exception as e:
        print("Error sending message: " + str(e))
        return None

def get_claude_response(phone, user_message):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    if phone not in conversation_history:
        conversation_history[phone] = []
        user_message = "INICIO NUEVA CONVERSACION. Saluda en espanol y menciona campana mundialista urgente partido jueves."
    conversation_history[phone].append({"role": "user", "content": user_message})
    if len(conversation_history[phone]) > 20:
        conversation_history[phone] = conversation_history[phone][-20:]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        system=CONTEXT,
        messages=conversation_history[phone]
    )
    assistant_message = response.content[0].text
    conversation_history[phone].append({"role": "assistant", "content": assistant_message})
    return assistant_message

def build_confirmation(folio):
    return ("Registro confirmado! Tu folio es: " + folio + "\n\n"
    "Quedas registrado en la Campana Mundialista BellaVittoria.\n\n"
    "DINAMICA:\n"
    "Precio especial: $3,520,000 MXN (valor de avaluo certificado)\n"
    "Si Mexico GANA vs Sudafrica el jueves antes 13:00 hrs: Bono $300,000 MXN\n"
    "Si Mexico EMPATA: Bono $150,000 MXN\n\n"
    "TERMINOS Y CONDICIONES:\n"
    "1. Promocion valida unicamente para 7 departamentos Piso 3 BellaVittoria Residencial.\n"
    "2. Bono aplica exclusivamente a clientes registrados ANTES del partido del jueves 13:00 hrs.\n"
    "3. El bono se aplica como descuento al precio de lista al momento de firma de contrato.\n"
    "4. Precio sujeto a cambio sin previo aviso. Disponibilidad limitada a 7 unidades.\n"
    "5. Participacion implica conocimiento y aceptacion de todas las politicas de Acierta Max y CUDI Ingenieria.\n"
    "6. Autorizas a Acierta Max enviarte informacion de
