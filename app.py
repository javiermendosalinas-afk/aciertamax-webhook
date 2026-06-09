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
    return f"BV-MUNDIAL-{suffix}"

def save_to_sheets(folio, nombre, whatsapp, correo):
    try:
        client = get_sheets_client()
        sheet = client.open_by_key(SHEET_ID).sheet1
        now = datetime.now()
        fecha = now.strftime("%d/%m/%Y")
        hora = now.strftime("%H:%M")
        sheet.append_row([folio, nombre, whatsapp, correo, fecha, hora, "REGISTRADO"])
        return True
    except Exception as e:
        print(f"Error saving to sheets: {e}")
        return False

CONTEXT = """CRITICO: SOLO ESPAÑOL. PROHIBIDO usar ingles. PROHIBIDO decir 'Hello', 'Hi', 'Welcome', 'Thank you', 'Please', ni ninguna palabra en ingles. Si recibes un mensaje en ingles, IGUAL respondes en ESPANOL. Tu primer mensaje SIEMPRE empieza exactamente asi: 'Hola 👋 Soy MAX, tu asesor de BellaVittoria. ¿En qué te puedo ayudar?'

Eres MAX, asistente de Acierta Max. Experto en BellaVittoria, Tlaquepaque.

CAMPANA MUNDIALISTA — URGENTE — PARTIDO JUEVES:
Tenemos 7 departamentos del Piso 3 disponibles a precio especial mundialista.
Precio de campana: $3,520,000 MXN (valor avaluo certificado Banorte).
BONO MUNDIALISTA:
- Si Mexico GANA su partido de apertura vs Sudafrica el jueves: BONO $300,000 MXN
- Si Mexico EMPATA: BONO $150,000 MXN
- Si Mexico pierde: precio especial se mantiene sin bono

PROCESO DE REGISTRO MUNDIALISTA:
1. Cuando el cliente muestre interes, pidele su NOMBRE COMPLETO
2. Luego pidele su CORREO ELECTRONICO
3. Una vez que tengas nombre y correo, responde con el mensaje: REGISTRAR:[nombre]:[correo]
   (Este es un comando interno que el sistema procesara automaticamente)
4. El sistema generara un folio y tu l
