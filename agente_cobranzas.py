import os
import re
import requests
import threading
from flask import Flask, request, jsonify
from anthropic import Anthropic
import psycopg2  # O la librería que uses para conectarte a Neon

app = Flask(__name__)

# ==========================================
# 🔐 CREDENCIALES Y CONFIGURACIÓN (Desde Render .env)
# ==========================================
TOKEN_VERIFICACION = os.getenv("TOKEN_VERIFICACION")
TOKEN_META = os.getenv("TOKEN_META")
ID_NUMERO_TELEFONO = os.getenv("ID_NUMERO_TELEFONO")
DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY)

# Memoria temporal para los chats en RAM
memoria_chats = {}

# ==========================================
# 🗄️ BASE DE DATOS NEON
# ==========================================
def buscar_deuda_en_neon(cedula):
    try:
        # Aquí va tu lógica real de conexión a Neon
        # conn = psycopg2.connect(DATABASE_URL)
        # ...
        return "" # Retorna vacío temporalmente si no hay deudas o falla
    except Exception as e:
        print(f"❌ Error en base de datos: {e}", flush=True)
        return ""

# ==========================================
# ⚙️ LÓGICA DEL SERVIDOR Y WHATSAPP
# ==========================================
@app.route('/webhook', methods=['GET'])
def verificar_webhook():
    """Ruta para que Meta confirme la conexión (Solo se usa una vez)."""
    if request.args.get('hub.mode') == 'subscribe' and request.args.get('hub.verify_token') == TOKEN_VERIFICACION:
        return request.args.get('hub.challenge'), 200
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def recibir_mensajes():
    """Ruta principal que recibe mensajes de WhatsApp en vivo."""
    # 1. Recibir los datos crudos de Meta
    data = request.get_json()
    
    # 🚨 EL RADAR MAESTRO: Imprime todo lo que entra sin filtros
    print(f"\n🚨 PAQUETE CRUDO DE META: {data}", flush=True)
    
    # 2. Hilos: Enviamos el trabajo a "segundo plano" para no dejar a Meta esperando
    hilo = threading.Thread(target=procesar_y_responder, args=(data,))
    hilo.start()
    
    # 3. ¡Responder INMEDIATAMENTE a Meta con código 200 para que no repita el mensaje!
    return jsonify({"status": "success"}), 200

def procesar_y_responder(data):
    """Función de segundo plano que lee, piensa y responde."""
    try:
        # 1. Extraemos la información del JSON
        valor = data['entry'][0]['changes'][0]['value']
        
        # Filtro 1: Si Meta solo nos avisa que el mensaje fue "leído" o "entregado", lo ignoramos
        if 'messages' not in valor:
            return
            
        mensaje_info = valor['messages'][0]
        numero_cliente = mensaje_info.get('from') or mensaje_info.get('from_user_id')
        tipo_mensaje = mensaje_info.get('type', 'desconocido')
        
        # Filtro 2: Si envían audios, imágenes o stickers, avisamos que no los leemos
        if tipo_mensaje != 'text':
            print(f"⚠️ El {numero_cliente} envió formato no soportado: {tipo_mensaje}", flush=True)
            enviar_mensaje_whatsapp(numero_cliente, "Hola. Soy el asistente virtual del despacho. Por favor, escríbeme tu mensaje exclusivamente en texto. 🤖")
            return
            
        # 2. Si es texto, sacamos el cuerpo del mensaje
        texto_recibido = mensaje_info['text']['body']
        
        # Creamos historial si es un cliente nuevo en esta sesión
        if numero_cliente not in memoria_chats:
            memoria_chats[numero_cliente] = []
            
        print(f"\n🗣️ DEUDOR ({numero_cliente}): {texto_recibido}", flush=True)
        
        # 3. Buscamos cédulas en el texto para revisar deudas en Neon
        posible_cedula = re.search(r'\b\d{7,11}\b', texto_recibido)
        contexto_financiero = buscar_deuda_en_neon(posible_cedula.group(0)) if posible_cedula else ""
            
        anotacion_usuario = f"Deudor dice: {texto_recibido}"
        if contexto_financiero:
            anotacion_usuario += f"\n[SISTEMA INTERNO: {contexto_financiero}]"
            
        # Guardamos en la memoria RAM
        memoria_chats[numero_cliente].append(anotacion_usuario)
        
        # Tomamos solo los últimos 8 mensajes para no saturar a Claude
        historial_reciente = "\n".join(memoria_chats[numero_cliente][-8:])
        
        instruccion_secreta = (
            "Historial de la conversación:\n"
            f"{historial_reciente}\n"
            "Genera la respuesta para el deudor basándote en este historial y tus reglas."
        )

        # 4. Hablamos con Claude 3.5 Haiku
        respuesta_ia = cliente_ia.messages.create(
            model="claude-3-5-haiku-20241022",
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
        print(f"🤖 CLAUDE RESPONDE: {respuesta_texto}", flush=True)
        
        # 5. Guardamos nuestra respuesta y la enviamos a WhatsApp
        memoria_chats[numero_cliente].append(f"Tú respondiste: {respuesta_texto}")
        enviar_mensaje_whatsapp(numero_cliente, respuesta_texto)
        
    except Exception as e:
        print(f"❌ Error interno procesando el mensaje: {e}", flush=True)

def enviar_mensaje_whatsapp(numero_destino, texto):
    """Envía el texto generado de vuelta al WhatsApp del cliente."""
    url = f"https://graph.facebook.com/v17.0/{ID_NUMERO_TELEFONO}/messages"
    headers = {"Authorization": f"Bearer {TOKEN_META}", "Content-Type": "application/json"}
    
    respuesta = requests.post(url, headers=headers, json={
        "messaging_product": "whatsapp", 
        "to": numero_destino, 
        "type": "text", 
        "text": {"body": texto}
    })
    
    # 🚨 RADAR DE SALIDA: Imprime la excusa que nos dé Meta si bloquea el envío
    print(f"📡 RESPUESTA DE META AL ENVIAR: {respuesta.status_code} - {respuesta.text}", flush=True)

if __name__ == '__main__':
    # Render asigna dinámicamente un puerto a través de la variable de entorno PORT
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
