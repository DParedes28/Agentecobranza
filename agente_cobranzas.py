from flask import Flask, request, jsonify
import requests
import anthropic
import psycopg2
import re
import os
from dotenv import load_dotenv

# 🔒 Cargar los secretos desde la caja fuerte (.env)
load_dotenv()

app = Flask(__name__)

# ==========================================
# 🔐 ZONA DE LLAVES Y CREDENCIALES
# ==========================================
TOKEN_VERIFICACION = os.getenv("TOKEN_VERIFICACION")
TOKEN_META = os.getenv("TOKEN_META")
ID_NUMERO_TELEFONO = os.getenv("ID_NUMERO_TELEFONO")
DATABASE_URL = os.getenv("DATABASE_URL")

# Inicializamos el nuevo cerebro: Claude
cliente_ia = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ==========================================
# 🧠 LA LIBRETA DE MEMORIA (RAM)
# ==========================================
memoria_chats = {}

def buscar_deuda_en_neon(cedula):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        query = """
            SELECT c.nombre, i.conjunto_residencial, i.torre_apto, e.concepto, e.periodo_mes, e.periodo_anio, e.valor_capital
            FROM contactos c
            JOIN inmuebles_ph i ON c.id = i.contacto_id
            JOIN expensas_ph e ON i.id = e.inmueble_id
            WHERE c.identificacion = %s AND e.estado != 'Pagada'
        """
        cursor.execute(query, (cedula,))
        resultados = cursor.fetchall()
        conn.close()

        if resultados:
            nombre_deudor = resultados[0][0]
            conjunto = resultados[0][1]
            apto = resultados[0][2]
            
            deudas_texto = []
            total_capital = 0
            
            for row in resultados:
                deudas_texto.append(f"- {row[3]} ({row[4]}/{row[5]}): ${row[6]:,.0f}")
                total_capital += float(row[6])
                
            return f"Deudor: {nombre_deudor} | Inmueble: {conjunto} - {apto}\nDetalle:\n" + "\n".join(deudas_texto) + f"\nTotal Mora: ${total_capital:,.0f}."
        return f"No se encontraron expensas en mora para la cédula {cedula}."
    except Exception as e:
        print(f"❌ Error en BD: {e}")
        return ""

# ==========================================
# ⚙️ LÓGICA DEL SERVIDOR Y WHATSAPP
# ==========================================
@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token') == TOKEN_VERIFICACION:
        return request.args.get('hub.challenge'), 200
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def recibir_mensajes():
    try:
        mensaje_info = request.get_json()['entry'][0]['changes'][0]['value']['messages'][0]
        numero_cliente = mensaje_info['from']
        texto_recibido = mensaje_info['text']['body']
        
        if numero_cliente not in memoria_chats:
            memoria_chats[numero_cliente] = []
            
        print(f"\n🗣️ DEUDOR ({numero_cliente}): {texto_recibido}")
        
        posible_cedula = re.search(r'\b\d{7,11}\b', texto_recibido)
        contexto_financiero = buscar_deuda_en_neon(posible_cedula.group(0)) if posible_cedula else ""
            
        anotacion_usuario = f"Deudor dice: {texto_recibido}"
        if contexto_financiero:
            anotacion_usuario += f"\n[SISTEMA INTERNO: {contexto_financiero}]"
            
        memoria_chats[numero_cliente].append(anotacion_usuario)
        
        historial_reciente = "\n".join(memoria_chats[numero_cliente][-8:])
        
        instruccion_secreta = (
            "Historial de la conversación:\n"
            f"{historial_reciente}\n"
            "Genera la respuesta para el deudor basándote en este historial y tus reglas."
        )

        # Llamada al nuevo cerebro (Claude 3.5 Haiku)
        respuesta_ia = cliente_ia.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "Eres el asistente virtual de cobranzas del abogado Diego Alejandro Paredes en Pereira, "
                "especializado en Propiedad Horizontal. \n"
                "REGLAS INQUEBRANTABLES:\n"
                "1. Busca lograr un acuerdo de pago.\n"
                "2. Exige un abono inicial mínimo del 30%.\n"
                "3. El plazo máximo para diferir el saldo es de 3 meses.\n"
                "4. NUNCA apruebes condonaciones de capital ni intereses.\n"
                "5. Si no hay acuerdo, advierte cordialmente el inicio del proceso ejecutivo.\n"
                "6. Mantén un tono corporativo, muy firme pero respetuoso. Usa respuestas cortas para WhatsApp."
            ),
            messages=[
                {"role": "user", "content": instruccion_secreta}
            ]
        )
        
        respuesta_texto = respuesta_ia.content[0].text
        print(f"🤖 CLAUDE RESPONDE: {respuesta_texto}")
        
        memoria_chats[numero_cliente].append(f"Tú respondiste: {respuesta_texto}")
        enviar_mensaje_whatsapp(numero_cliente, respuesta_texto)
        
    except KeyError:
        pass
        
    return jsonify({"status": "success"}), 200

def enviar_mensaje_whatsapp(numero_destino, texto):
    url = f"https://graph.facebook.com/v17.0/{ID_NUMERO_TELEFONO}/messages"
    headers = {"Authorization": f"Bearer {TOKEN_META}", "Content-Type": "application/json"}
    
    # Guardamos la respuesta que nos da Meta
    respuesta = requests.post(url, headers=headers, json={
        "messaging_product": "whatsapp", 
        "to": numero_destino, 
        "type": "text", 
        "text": {"body": texto}
    })
    
    # 🚨 Imprimimos en la consola de Render el motivo exacto del rechazo
    print(f"📡 RESPUESTA DE META AL ENVIAR: {respuesta.status_code} - {respuesta.text}", flush=True)

if __name__ == '__main__':
    print("🚀 SERVIDOR CON CLAUDE 3.5 HAIKU Y DB NEON ENCENDIDO...")
    app.run(port=5000, debug=True)
