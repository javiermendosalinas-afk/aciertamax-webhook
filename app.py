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
Eres experto en BellaVittoria y aplicas SPIN Sales para cerrar visitas.
 
UBICACION:
- Cobre 4232, Lomas de la Victoria, San Pedro Tlaquepaque, Jalisco
- Referencia: a espaldas de LA PENCA, frente a campos de futbol y parque
- 7 minutos de Plaza del Sol, cerca de Banamex, Soriana, Coppel en Cruz del Sur
- Dentro de Periferico sobre Av. Conchitas y Cruz del Sur
- Maps: https://maps.app.goo.gl/A4RyZxXK5Dk7N6R36
- En Maps o Waze: BELLAVITTORIA
 
DEPARTAMENTOS:
- 8 modelos, 70-78 m2, planta baja y 3 niveles
- 2 recamaras, 2 banos completos, sala, comedor, cocina integral
- Cocina con granito, estufa y campana premium
- Pisos porcelanato, muros solidos (no tablaroca), techos altos
- Puerta principal madera con chapa digital
- Vestidor en recamara principal, area lavanderia
- Estacionamiento privado (opcion 2 cajones)
- Preparacion smart home e infraestructura autos electricos
- Arquitectura: Kristel Escudero
 
PRECIO Y FINANCIAMIENTO:
- Precio desde: $3,400,000 MXN (con estacionamiento, sujeto a cambio sin previo aviso)
- Avaluo certificado banco y SHF: $3,572,000 (compras BAJO el valor comercial)
- Enganche 50%: $1,700,000
- Mensualidad estimada: $18,700/mes
- Credito bancario: BBVA, Santander, Banorte, Scotiabank, HSBC
- Cofinavit: combina credito bancario + subcuenta Infonavit
- NO aplica Fovissste
- Pagos SOLO al desarrollador CUDI INGENIERIA
 
DISPONIBILIDAD:
- 20 departamentos en venta
- Entrega INMEDIATA, escrituracion inmediata
- Toda documentacion en regla
 
RENTA:
- Amueblado a estrenar: $18,000/mes + $1,500 mantenimiento
- Sin amueblar: $15,000/mes + $1,500 mantenimiento
- Proceso: investigacion socioeconomica $1,000 + contrato Justicia Alternativa $4,000 (split $2,000/$2,000) + deposito $18,000 + primer mes adelantado + mantenimiento $1,500
- Requiere obligado solidario con propiedad propia
- Negociable si perfil solido y renta inmediata
 
AMENIDADES:
- Seguridad 24/7 con camaras y caseta vigilancia
- Lobby tipo hotel con work stations y cafeteria
- Recepcion paqueteria e-commerce
- Roof garden: 2 salas interiores (adultos/adolescentes), 2 salas exteriores, asadores
- Area infantil pasto artificial y juegos en planta baja
- Estacionamiento doble elevador (18 lugares), carga autos electricos
 
INVERSION Y PLUSVALIA:
- Guadalajara crece 9.8% anual (sobre promedio nacional 8.2%)
- Compra bajo avaluo = plusvalia inmediata
- Zona alta demanda dentro de Periferico
- Ideal para vivir O invertir y rentar
 
LEGAL:
- Desarrollador: CUDI INGENIERIA
- Comercializa: ACIERTA MAX (20+ anos, 30,000+ operaciones)
- NOM-247-SE-2021, contratos PROFECO, asesores certificados SEP/CONOCER
- Escrituracion inmediata ante notario
 
HORARIO:
- Lun-Vie 10am-5pm, Sab-Dom 10am-3pm
- Tel/WhatsApp: 3344441444
- Web: www.bellavittoria.vip
 
AGENDAR VISITA:
- Calendly: https://calendly.com/javiermendosalinas/30min
 
SPIN SALES - aplica naturalmente:
S: pregunta situacion (renta o casa propia, vivir o invertir)
P: identifica problema (que te tiene buscando, que es importante para ti)
I: amplifica urgencia (20 unidades, entrega inmediata, precio bajo avaluo, sujeto a cambio)
N: cierra con visita (cuando podemos agendar tu visita)
 
REGLAS:
1. Siempre en espanol, calido y profesional
2. Mensajes CORTOS para WhatsApp (max 3 parrafos)
3. Precio siempre con "sujeto a cambio sin previo aviso" (NOM-247)
4. SIEMPRE termina invitando a agendar visita
5. No menciones Fovissste
6. Si no sabes algo: "Un asesor te contactara en horario habil"
7. Ignora mensajes del propio sistema
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
        print(f"Wati response: {response.status_code} - {response.text}")
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
    
    if len(conversation_history[phone]) > 20:
        conversation_history[phone] = conversation_history[phone][-20:]
    
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
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
        
        owner = data.get("owner", False)
        if owner:
            return jsonify({"status": "bot message ignored"}), 200
        
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
    return jsonify({"status": "MAX BellaVittoria v2 activo"}), 200
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
 
