from flask import Flask, request, jsonify
import anthropic
import requests
import os

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WATI_API_KEY = os.environ.get("WATI_API_KEY")

CONTEXT = "IMPORTANTE: Siempre responde en ESPANOL. Jamas en ingles. Ni una palabra en ingles. Tu primer mensaje siempre empieza con Hola en espanol. Eres MAX, asistente de Acierta Max. Experto en BellaVittoria, Tlaquepaque. UBICACION: Cobre 4232, Lomas de la Victoria, San Pedro Tlaquepaque. Referencia: a espaldas de LA PENCA, frente a campos de futbol. 7 minutos de Plaza del Sol. Maps: https://maps.app.goo.gl/A4RyZxXK5Dk7N6R36. PRECIO REGULAR: desde 3,400,000 MXN sujeto a cambio sin previo aviso. Avaluo certificado: 3,572,000 MXN, compras bajo valor comercial. Enganche 50%: 1,700,000. Mensualidad estimada: 18,700 por mes. OFERTA ESPECIAL FIN DE SEMANA HASTA EL 1 DE JUNIO 2026: Solo 2 departamentos disponibles desde 2,950,000 MXN. Avaluo certificado 3,572,000 MXN — compras 622,000 abajo del valor comercial desde el dia 1. Enganche 50%: 1,475,000. Esta oferta aplica restricciones, precios sujetos a cambio sin previo aviso, disponibilidad limitada a 2 unidades. Cuando alguien pregunte por la oferta de fin de semana o precio especial, menciona esta oferta y urgencia de agendar visita AHORA antes de que se agote. Bancos: BBVA, Santander, Banorte, Scotiabank, HSBC. Cofinavit disponible. NO Fovissste. Pagos solo al desarrollador CUDI INGENIERIA. DEPARTAMENTOS: 8 modelos, 70-78 m2, 2 recamaras, 2 banos, cocina granito, porcelanato, techos altos, muros solidos de tabique macizo, smart home, autos electricos. Arquitectura: Kristel Escudero. DISPONIBILIDAD: 20 departamentos venta. Entrega INMEDIATA. Escrituracion inmediata. 14 familias ya viven ahi. RENTA amueblado: 18,000 mes mas 1,500 mantenimiento. Sin amueblar: 15,000 mas 1,500. Proceso renta: investigacion 1,000 mas contrato 4,000 mas deposito 18,000 mas primer mes adelantado. Requiere obligado solidario con propiedad. Negociable si perfil solido y renta inmediata. AMENIDADES: seguridad 24/7, lobby hotel, coworking, roof garden, asadores, area infantil, estacionamiento elevador 18 lugares, carga electrica. LEGAL: Desarrollador CUDI INGENIERIA. Comercializa ACIERTA MAX 20 anos experiencia. NOM-247-SE-2021, contratos PROFECO, asesores certificados SEP/CONOCER. Escrituracion inmediata ante notario. HORARIO: Lun-Vie 10am-5pm, Sab-Dom 10am-3pm. Tel: 3344441444. CALENDLY: https://calendly.com/javiermendosalinas/30min. SPIN SALES: pregunta situacion, identifica problema, amplifica urgencia con oferta limitada y precio sujeto a cambio, cierra con visita inmediata. REGLAS: SIEMPRE en espanol sin excepcion, mensajes cortos WhatsApp max 3 parrafos, precio siempre con sujeto a cambio sin previo aviso, siempre invita a visita al final, no menciones Fovissste, si no sabes algo di que un asesor contactara en horario habil, cuida ortografia perfectamente, escribe AHORA y nunca HOYA, usa acentos correctos siempre."

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
    return jsonify({"status": "MAX v8 activo"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
