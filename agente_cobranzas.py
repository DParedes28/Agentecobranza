import os
import re
import requests
import threading
from flask import Flask, request, jsonify
from anthropic import Anthropic
import psycopg2

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
# 🗄️ BASE DE DATOS NEON (CONEXIONES REALES)
# ==========================================
def buscar_deuda_en_neon(cedula):
    """Busca las deudas activas cruzando las tablas de la firma"""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT o.capital, o.tipo_titulo, c.nombre 
                    FROM obligaciones o 
                    JOIN contactos c ON o.identificacion_deudor = c.identificacion 
                    WHERE o.identificacion_deudor = %s AND o.estado != 'Pagada'
                """, (cedula,))
                resultados = cur.fetchall()
                
                if resultados:
                    nombre = resultados[0][2]
                    total = sum(r[0] for r in resultados)
                    detalle = "\n".join([f"- {r[1]}: ${r[0]:,.0f}" for r in resultados])
                    return f"Deudor: {nombre}. Deudas activas:\n{detalle}\nTotal Adeudado: ${total:,.0f}."
        return ""
    except Exception as e:
        print(f"❌ Error en base de datos: {e}", flush=True)
        return ""

def guardar_auditoria(numero, remitente, mensaje):
    """Guarda el historial inmutable de chats"""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO auditoria_chats (numero_telefono, remitente, mensaje) VALUES (%s, %s, %s)", 
                    (numero, remitente, mensaje)
                )
    except Exception as e:
        print(f"❌ Error guardando auditoría: {e}", flush=True)

def guardar_anotacion_crm(numero, nota):
    """Guarda las etiquetas de la IA directamente en gestiones_cartera"""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Buscamos la obligación activa de este celular
                cur.execute("""
                    SELECT o.id FROM obligaciones o
                    JOIN contactos c ON o.identificacion_deudor = c.identificacion
                    WHERE c.telefono = %s AND o.estado != 'Pagada' LIMIT 1
                """, (numero,))
                res = cur.fetchone()
                
                if res:
                    id_obligacion = res[0]
                    # Extraemos fecha si el formato es AAAA-MM-DD
                    fecha_match = re.search(r'\d{4}-\d{2}-\d{2}', nota)
                    fecha_promesa = fecha_match.group(0) if fecha_match else None
                    
                    cur.execute("""
                        INSERT INTO gestiones_cartera (id_obligacion, tipo_contacto, resumen, promesa_pago_fecha, usuario) 
                        VALUES (%s, %s, %s, %s, %s)
                    """, (id_obligacion, 'WhatsApp IA', nota, fecha_promesa, 'Bot Claude'))
    except Exception as e:
        print(f"❌ Error guardando en CRM: {e}", flush=True)

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
    data = request.get_json()
    hilo = threading.Thread(target=procesar_y_responder, args=(data,))
    hilo.start()
    return jsonify({"status": "success"}), 200

def procesar_y_responder(data):
    try:
        valor = data['entry'][0]['changes'][0]['value']
        
        # Filtros de seguridad
        if valor.get('messaging_product') != 'whatsapp' or 'messages' not in valor:
            return
            
        mensaje_info = valor['messages'][0]
        contacto = valor.get('contacts', [{}])[0]
        numero_cliente = mensaje_info.get('from') or contacto.get('wa_id') or mensaje_info.get('from_user_id')
        
        if not numero_cliente:
            return
            
        id_mensaje_entrante = mensaje_info.get('id')
        tipo_mensaje = mensaje_info.get('type', 'desconocido')
        
        if tipo_mensaje != 'text':
            enviar_mensaje_whatsapp(numero_cliente, "Hola. Soy el asistente virtual del despacho. Por favor, escríbeme tu mensaje exclusivamente en texto. 🤖", id_mensaje_entrante)
            return
            
        texto_recibido = mensaje_info['text']['body']
        
        # 1. GUARDAR LO QUE DICE EL DEUDOR
        guardar_auditoria(numero_cliente, 'Deudor', texto_recibido)
        
        if numero_cliente not in memoria_chats:
            memoria_chats[numero_cliente] = []
            
        print(f"\n🗣️ DEUDOR ({numero_cliente}): {texto_recibido}", flush=True)
        
        # 2. BUSCAR CÉDULAS
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

        # 3. CONECTAR CON CLAUDE
        respuesta_ia = cliente_ia.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "Eres el asistente virtual de cobranzas del abogado Diego Alejandro Paredes. "
                "TUS REGLAS INQUEBRANTABLES:\n"
                "1. Exige siempre un abono inicial mínimo del 30%.\n"
                "2. El plazo máximo para diferir el saldo es de 3 meses.\n"
                "3. NUNCA apruebes condonaciones de capital ni intereses.\n"
                "4. Si no hay acuerdo, advierte el inicio o continuación del proceso jurídico.\n"
                "5. Mantén un tono corporativo, muy firme pero respetuoso. Usa respuestas cortas.\n"
                "INSTRUCCIÓN DE SISTEMA (ETIQUETAS CRM):\n"
                "Cuando el deudor acepte una promesa de pago con fecha, DEBES incluir OBLIGATORIAMENTE al final de tu respuesta la siguiente etiqueta exacta:\n"
                "[NOTA_CRM: Promesa para AAAA-MM-DD] (ejemplo: [NOTA_CRM: Promesa para 2026-09-30]).\n"
                "Si el deudor avisa que ya pagó, escribe: [NOTA_CRM: Reporta pago previo]."
            ),
            messages=[
                {"role": "user", "content": instruccion_secreta}
            ]
        )
        
        respuesta_cruda = respuesta_ia.content[0].text
        print(f"🤖 CLAUDE PENSÓ: {respuesta_cruda}", flush=True)
        
        # 4. EL FILTRO INTERCEPTOR
        etiqueta = re.search(r'\[NOTA_CRM:(.*?)\]', respuesta_cruda)
        if etiqueta:
            nota_secreta = etiqueta.group(1).strip()
            # Guardamos la etiqueta en el CRM y recortamos el mensaje para el usuario
            guardar_anotacion_crm(numero_cliente, nota_secreta)
            respuesta_limpia = re.sub(r'\[NOTA_CRM:.*?\]', '', respuesta_cruda).strip()
        else:
            respuesta_limpia = respuesta_cruda.strip()
            
        memoria_chats[numero_cliente].append(f"Tú respondiste: {respuesta_limpia}")
        
        # 5. GUARDAR LA SALIDA Y ENVIAR
        guardar_auditoria(numero_cliente, 'Bot IA', respuesta_limpia)
        enviar_mensaje_whatsapp(numero_cliente, respuesta_limpia, id_mensaje_entrante)
        
    except Exception as e:
        print(f"❌ Error interno procesando el mensaje: {e}", flush=True)

def enviar_mensaje_whatsapp(numero_destino, texto, id_mensaje_entrante=None):
    url = f"https://graph.facebook.com/v20.0/{ID_NUMERO_TELEFONO}/messages"
    headers = {"Authorization": f"Bearer {TOKEN_META}", "Content-Type": "application/json"}
    
    payload = {
        "messaging_product": "whatsapp", 
        "to": numero_destino, 
        "type": "text", 
        "text": {"body": texto}
    }
    if id_mensaje_entrante:
        payload["context"] = {"message_id": id_mensaje_entrante}
    
    respuesta = requests.post(url, headers=headers, json=payload)
    print(f"📡 RESPUESTA DE META AL ENVIAR: {respuesta.status_code}", flush=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
