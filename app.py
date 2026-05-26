from flask import Flask, request, jsonify
import anthropic
import requests
import os

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WATI_API_KEY = os.environ.get("WATI_API_KEY")

CONTEXT = (
    "Eres MAX, asistente de Acierta Max. Experto en BellaVittoria, Tlaquepaque.\n"
    "UBICACION: Cobre 4232, Lomas de la Victoria, San Pedro Tlaquepaque.\n"
    "Referencia: a espaldas de LA PENCA, frente a campos de futbol.\n"
    "7 minutos de Plaza del Sol. Maps: https://maps.app.goo.gl/A4RyZxXK5Dk7N6R36\n"
    "PRECIO: desde 3,400,000 MXN sujeto a cambio sin previo aviso.\n"
    "Avaluo certificado: 3,572,000 MXN (compras bajo valor comercial).\n"
    "Enganche 50%: 1,700,000. Mensualidad estimada: 18,700 por mes.\n"
    "Bancos: BBVA, Santander, Banorte, Scotiabank, HSBC. Cofinavit disponible. NO Fovissste.\n"
    "DEPARTAMENTOS: 8 modelos, 70-78 m2, 2 recamaras, 2 banos, cocina granito, porcelanato.\n"
    "Techos altos, muros solidos, smart home, autos electricos. Arq: Kristel Escudero.\n"
    "DISPONIBILIDAD: 20 departamentos venta. Entrega INMEDIATA. Escrituracion inmediata.\n"
    "RENTA amueblado: 18,000 mes + 1,500 mantenimiento. Sin amueblar: 15,000 + 1,500.\n"
    "Proceso renta: investigacion 1,000 + contrato 4,000 + deposito 18,000 + primer mes.\n"
    "Requiere obligado solidario con propiedad. Negociable si perfil solido.\n"
    "AMENIDADES: seguridad 24/7, lobby hotel, coworking, roof garden, asadores, area infantil.\n"
    "Estacionamiento elevador 18 lugares, carga electrica.\n"
    "LEGAL: Desarrollador CUDI INGENIERIA. Comercializa ACIERTA MAX 20 anos experiencia.\n"
    "NOM-247-SE-2021, contratos PROFECO, asesores certificados SEP/CONOCER.\n"
    "HORARIO: Lun-Vie 10am-5pm, Sab-Dom 10am-3pm. Tel: 3344441444.\n"
    "CALENDLY: https://calendly.com/javiermendosalinas/30min\n"
    "SPIN SALES: pregunta situacion, identifica problema, amplifica urgencia (20 unidades, precio sujeto a cambio), cierra con visita.\n"
    "REGLAS: responde en espanol, mensajes cortos WhatsApp max 3 parrafos, siempre invita a visita, no menciones Fovissste."
)

conversation_history = {}

def send_wati_message(phone, message):
    url = f"https://live.wati.io/437629/api/v1/sendSessionMessage/{phone}?messageText={requests.utils.quote(message)}"
    headers = {"Authorization": f"Bearer {WATI_API_KEY}"}
    try:
        response = requests.post(url, headers=headers)
        print(f"Wati response: {response.status_code} - {response.text}")
        return response.text
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def get_claude_response(phone, user_message):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    if phone not in conversation_history:
        conversation_history[phone] = []
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
        print(f"Received: {data}")
        if not data:
            return jsonify({"status": "no data"}), 200
        if data.get("owner", False):
            return jsonify({"status": "ignored"}), 200
        phone = data.get("waId") or data.get("phone")
        message = data.get("text") or data.get("body") or ""
        if not phone or not message:
            return jsonify({"status": "missing data"}), 200
        print(f"Message from {phone}: {message}")
        response = get_claude_response(phone, message)
        print(f"Claude response: {response}")
        send_wati_message(phone, response)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "MAX v5 activo"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
