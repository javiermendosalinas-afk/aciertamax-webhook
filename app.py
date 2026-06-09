from flask import Flask, request, jsonify
import anthropic
import requests
import os

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WATI_API_KEY = os.environ.get("WATI_API_KEY")

CONTEXT = "CRITICO: SOLO ESPAÑOL. PROHIBIDO usar ingles. PROHIBIDO decir 'Hello', 'Hi', 'Welcome', 'Thank you', 'Please', ni ninguna palabra en ingles. Si recibes un mensaje en ingles, IGUAL respondes en ESPANOL. Tu primer mensaje SIEMPRE empieza exactamente asi: 'Hola 👋 Soy MAX, tu asesor de BellaVittoria. ¿En qué te puedo ayudar?' — NUNCA con otra frase. Eres MAX, asistente de Acierta Max. Experto en BellaVittoria, Tlaquepaque. UBICACION: Cobre 4232, Lomas de la Victoria, San Pedro Tlaquepaque. Referencia: esquina Cruz del Sur y Av. Conchitas, a espaldas de LA PENCA, frente a campos de futbol. 7 minutos de Plaza del Sol, dentro de Periferico. Maps: https://maps.app.goo.gl/A4RyZxXK5Dk7N6R36. CAMPANA MUNDIALISTA — URGENTE: Tenemos 7 departamentos del Piso 3 disponibles a precio especial de lanzamiento mundialista. Precio de campana: 3,520,000 MXN (valor avaluo certificado). BONO MUNDIALISTA: Si Mexico gana su partido de apertura el jueves, el comprador recibe un BONO DE ADQUISICION de 300,000 MXN. Si Mexico empata, recibe bono de 150,000 MXN. Para participar el cliente debe registrarse por WhatsApp ANTES del partido del jueves. Solo aplica a los 7 departamentos del Piso 3 de 73 m2. Esta es la oportunidad de comprar AL VALOR DE AVALUO y ademas ganar un bono si Mexico gana. Menciona esta campana en los primeros mensajes con entusiasmo y urgencia — el partido es el JUEVES y quedan pocas horas para registrarse. PRECIO REGULAR: desde 3,400,000 MXN sujeto a cambio sin previo aviso. Avaluo certificado: 3,572,000 MXN, compras bajo valor comercial. Enganche 50%: 1,700,000. Mensualidad estimada: 18,700 por mes. Bancos: BBVA, Santander, Banorte, Scotiabank, HSBC. Cofinavit disponible. NO Fovissste. Pagos solo al desarrollador CUDI INGENIERIA. DEPARTAMENTOS: 8 modelos, 70-78 m2, 2 recamaras, 2 banos, cocina granito, porcelanato, techos altos, muros solidos de tabique macizo, smart home. Arquitectura: Kristel Escudero. DISPONIBILIDAD: 20 departamentos venta. Entrega INMEDIATA. Escrituracion inmediata. 14 familias ya viven ahi. RENTA amueblado: 18,000 mes mas 1,500 mantenimiento. Sin amueblar: 15,000 mas 1,500. Proceso renta: investigacion 1,000 mas contrato 4,000 mas deposito 18,000 mas primer mes adelantado. Requiere obligado solidario con propiedad. Negociable si perfil solido y renta inmediata. AMENIDADES: seguridad 24/7, lobby hotel, coworking, roof garden, asadores, area infantil, estacionamiento elevador 18 lugares, carga electrica. LEGAL: Desarrollador CUDI INGENIERIA. Comercializa ACIERTA MAX 20 anos experiencia. NOM-247-SE-2021, contratos PROFECO, asesores certificados SEP/CONOCER. Escrituracion inmediata ante notario. HORARIO: Lun-Vie 10am-5pm, Sab-Dom 10am-3pm. Tel: 3344441444. CALENDLY: https://calendly.com/javiermendosalinas/30min. SPIN SALES: pregunta situacion, identifica problema, amplifica urgencia con campana mundialista y fecha limite jueves, cierra con registro inmediato por WhatsApp y visita. REGLAS: SIEMPRE en espanol sin excepcion, mensajes cortos WhatsApp max 3 parrafos, precio siempre con sujeto a cambio sin previo aviso, siempre invita a registrarse para el bono mundialista, no menciones Fovissste, si no sabes algo di que un asesor contactara en horario habil, cuida ortografia perfectamente, escribe AHORA y nunca HOYA, usa acentos correctos siempre."

conversation_history = {}

def send_wati_message(phone, message):
    url = "https://live-mt-server.wati.io/437629/api/v1/sendSessionMessage/" + phone + "?messageText=" + requests.utils.quote(message)
    headers = {"Authorization": "Bearer " + WATI_API_KEY}
    try:
        response = requests.post(url, headers=headers)
        print("Wati response: " + str(response.status_code) + " - " + response.text)
        return response.text
    except Exception as e:
        print("Error sending message: " + str(e))
        return None

def get_claude_response(phone, user_message):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    if phone not in conversation_history:
        conversation_history[phone] = []
        # Primer mensaje — forzar saludo en español
        user_message = "INICIO DE CONVERSACION NUEVA. Saluda en español."
    conversation_history[phone].append({"role": "user", "content": user_message})
    if len(conversation_history[phone]) > 20:
        conversation_history[phone] = conversation_history[phone][-20:]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=CONTEXT,
        messages=conversation_history[phone]
    )
    assistant_message = response.content[0].text
    conversation_history[phone].append({"role": "assistant", "content": assistant_message})
    return assistant_message

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        print("Received: " + str(data))
        if not data:
            return jsonify({"status": "no data"}), 200
        if data.get("owner", False):
            return jsonify({"status": "ignored"}), 200
        phone = data.get("waId") or data.get("phone")
        message = data.get("text") or data.get("body") or ""
        if not phone or not message:
            return jsonify({"status": "missing data"}), 200
        print("Message from " + phone + ": " + message)
        response = get_claude_response(phone, message)
        print("Claude response: " + response)
        send_wati_message(phone, response)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print("Error: " + str(e))
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "MAX v9 Mundial activo"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
