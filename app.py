
from flask import Flask, request, jsonify
import anthropic
import requests
import os
 
app = Flask(__name__)
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WATI_API_KEY = os.environ.get("WATI_API_KEY")
WATI_URL = "https://live.wati.io/437629"
 
BELLAVITTORIA_CONTEXT = """
Eres MAX, el asistente virtual de Acierta Max, los profesionales inmobiliarios.
Eres experto en BellaVittoria, un desarrollo residencial en Lomas de la Victoria, Tlaquepaque, Jalisco.
 
INFORMACIÓN DE BELLAVITTORIA:
- Dirección: Cobre #4232 esq. Ave. Conchitas y Ave. Cruz del Sur, Lomas de la Victoria, Tlaquepaque
- Precio venta: Desde $3,400,000 MXN
- Modelos: 8 modelos disponibles, 70-78 m², 3 niveles
- Recámaras: 2 | Baños: 2 | Estacionamiento: 1-2 lugares
- Disponibilidad: 20 departamentos en venta
- Entrega: Inmediata, escrituración inmediata
- Avalúo certificado: $3,572,000 (compras por debajo del valor de mercado)
- Sitio web: www.bellavittoria.vip
- Mapa: https://maps.app.goo.gl/rqhKJUgPFxHZcGSNA
 
FINANCIAMIENTO:
- Enganche: 50% ($1,700,000)
- Mensualidad estimada: $18,700/mes
- Bancos: BBVA, Santander, Banorte, Scotiabank, HSBC
- Cofinavit: Puedes combinar crédito bancario con subcuenta de vivienda del Infonavit
- NO aplica Fovissste
 
RENTA DISPONIBLE:
- Departamento amueblado a estrenar: $18,000/mes + $1,500 mantenimiento
- Departamento sin amueblar: $15,000/mes + $1,500 mantenimiento
- Proceso renta: Investigación socioeconómica $1,000 + Contrato Justicia Alternativa $4,000 + Depósito $18,000 + Primer mes $18,000
- Requiere obligado solidario con propiedad propia
 
AMENIDADES:
- Vigilancia 24 horas
- Elevador
- Ludoteca
- Workstation
- Terraza común
- Recibidor de visitas
 
HORARIO DE ATENCIÓN:
- Lunes a Viernes: 10am - 5pm
- Sábado y Domingo: 10am - 3pm
 
AGENDAR CITA:
- Calendly: https://calendly.com/javiermendosalinas/30min
 
INSTRUCCIONES DE COMPORTAMIENTO:
1. Responde SIEMPRE en español
2. Sé cálido, profesional y orientado a cerrar la visita
3. Aplica SPIN Sales: pregunta situación, identifica problema, amplifica urgencia, cierra con visita
4. Si preguntan precio, da el precio Y la mensualidad estimada
5. Si preguntan ubicación, da la dirección Y el link de Google Maps
6. Siempre termina invitando a agendar visita en Calendly
7. Si no sabes algo, di que un asesor le contactará en horario hábil
8. Máximo 3 párrafos cortos por respuesta - mensajes de WhatsApp deben ser concisos
9. No menciones Fovissste - solo Cofinavit con bancos
10. Cumple NOM-247-SE-2021: precios sujetos a cambio sin previo aviso
"""
 
conversation_history = {}
 
def send_wati_message(phone, message):
    url = f"{WATI_URL}/api/v1/sendSessionMessage/{phone}"
    headers = {
        "Authorization": f"Bearer {WATI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {"messageText": message}
    try:
        response = requests.post(url, json=data, headers=headers)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None
 
def get_claude_response(phone, user_message):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    if phone not in conversation_history:
        conversation_history[phone] = []
    
    conversation_history[phone].append({
        "role": "user",
        "content": user_message
    })
    
    # Mantener solo últimos 10 mensajes
    if len(conversation_history[phone]) > 10:
        conversation_history[phone] = conversation_history[phone][-10:]
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=BELLAVITTORIA_CONTEXT,
        messages=conversation_history[phone]
    )
    
    assistant_message = response.content[0].text
    
    conversation_history[phone].append({
        "role": "assistant",
        "content": assistant_message
    })
    
    return assistant_message
 
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        print(f"Received: {data}")
        
        if not data:
            return jsonify({"status": "no data"}), 200
        
        phone = data.get("waId") or data.get("phone")
        message = data.get("text") or data.get("body") or ""
        
        if not phone or not message:
            return jsonify({"status": "missing data"}), 200
        
        # Obtener respuesta de Claude
        response = get_claude_response(phone, message)
        
        # Enviar respuesta por Wati
        send_wati_message(phone, response)
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200
 
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "MAX BellaVittoria webhook activo"}), 200
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
