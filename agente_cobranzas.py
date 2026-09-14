import os
import re
import requests
import threading
import base64
import hmac
import hashlib
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from anthropic import Anthropic
import psycopg2

app = Flask(__name__)

TOKEN_VERIFICACION = os.getenv("TOKEN_VERIFICACION")
TOKEN_META = os.getenv("TOKEN_META")
META_APP_SECRET = os.getenv("META_APP_SECRET")
ID_NUMERO_TELEFONO = os.getenv("ID_NUMERO_TELEFONO")
DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
LIQUIDADOR_API_URL = os.getenv("LIQUIDADOR_API_URL")
LIQUIDADOR_API_KEY = os.getenv("LIQUIDADOR_API_KEY")

cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
memoria_chats = {}
obligaciones_activas = {}
REQUEST_TIMEOUT = 30
TZ_COLOMBIA = ZoneInfo("America/Bogota")

# ==============================================================================
# --- RUTA PING (DESPERTADOR PARA UPTIMEROBOT) ---
# ==============================================================================
@app.route('/', methods=['GET', 'HEAD'])
def ping():
    return "Agente de cobranzas activo", 200

def fecha_colombia():
    return datetime.now(TZ_COLOMBIA).date().isoformat()


def verificar_firma_meta(raw_body):
    """Valida X-Hub-Signature-256 y falla cerrado si falta el App Secret."""
    if not META_APP_SECRET:
        return False
    firma = request.headers.get("X-Hub-Signature-256", "")
    if not firma.startswith("sha256="):
        return False
    esperado = hmac.new(META_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma[7:], esperado)


def solicitar_liquidacion(inmueble_id, fecha_corte=None):
    if not LIQUIDADOR_API_URL:
        raise RuntimeError("LIQUIDADOR_API_URL no esta configurada en Render")
    fecha_corte = fecha_corte or fecha_colombia()
    url = f"{LIQUIDADOR_API_URL.rstrip('/')}/api/bot/liquidar"
    headers = {"Content-Type": "application/json"}
    if LIQUIDADOR_API_KEY:
        headers["X-API-Key"] = LIQUIDADOR_API_KEY
    payload = {"inmueble_id": int(inmueble_id), "fecha_corte": fecha_corte}
    respuesta = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    respuesta.raise_for_status()
    try:
        data = respuesta.json()
    except ValueError as exc:
        raise RuntimeError("El liquidador devolvio una respuesta que no es JSON") from exc
    if data.get("status") != "success":
        raise RuntimeError(data.get("mensaje") or "El liquidador devolvio un error")
    datos = data.get("datos") or {}
    if not datos:
        raise RuntimeError("El liquidador no devolvio datos de la liquidacion")
    return datos


def reportar_abono_al_erp(inmueble_id, valor, fecha_pago=None, banco="Transferencia", referencia="Comprobante WhatsApp", soporte_url=""):
    """Envía el comprobante detectado por el agente al ERP para asentar el abono."""
    if not LIQUIDADOR_API_URL:
        return None
    url = f"{LIQUIDADOR_API_URL.rstrip('/')}/api/recaudos/bot/abono"
    headers = {"Content-Type": "application/json"}
    if LIQUIDADOR_API_KEY:
        headers["X-API-Key"] = LIQUIDADOR_API_KEY
    payload = {
        "inmueble_id": int(inmueble_id),
        "valor": float(valor),
        "fecha_pago": fecha_pago or fecha_colombia(),
        "banco": str(banco),
        "referencia": str(referencia),
        "soporte_url": str(soporte_url)
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        return r.json()
    except Exception as exc:
        print(f"❌ Error reportando abono al ERP: {exc}", flush=True)
        return None


def buscar_deuda_en_neon(cedula, numero_cliente=None):
    try:
        cedula_limpia = re.sub(r"\D", "", str(cedula or ""))
        if not (6 <= len(cedula_limpia) <= 12):
            return "SISTEMA: No se recibio una cedula valida."
        if not DATABASE_URL:
            return "SISTEMA: Error de configuracion de la base de datos."
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT i.id, c.nombre, c.identificacion
                    FROM inmuebles_ph i
                    INNER JOIN contactos c ON c.id = i.contacto_id
                    WHERE REGEXP_REPLACE(COALESCE(c.identificacion::text, ''), '[^0-9]', '', 'g') = %s
                    ORDER BY i.id
                """, (cedula_limpia,))
                titulares = cur.fetchall()
                cur.execute("""
                    SELECT p.inmueble_id, COALESCE(c.nombre, 'Persona no registrada'),
                           pl.identificacion_demandado, p.radicado_interno,
                           pl.es_principal, p.estado
                    FROM procesos_litisconsorcio pl
                    INNER JOIN procesos p ON p.radicado_interno = pl.radicado_interno
                    LEFT JOIN contactos c ON REGEXP_REPLACE(COALESCE(c.identificacion::text, ''), '[^0-9]', '', 'g') =
                        REGEXP_REPLACE(COALESCE(pl.identificacion_demandado::text, ''), '[^0-9]', '', 'g')
                    WHERE REGEXP_REPLACE(COALESCE(pl.identificacion_demandado::text, ''), '[^0-9]', '', 'g') = %s
                    ORDER BY CASE WHEN LOWER(COALESCE(p.estado, '')) = 'activo' THEN 0 ELSE 1 END,
                             CASE WHEN COALESCE(pl.es_principal, false) THEN 0 ELSE 1 END,
                             p.inmueble_id
                """, (cedula_limpia,))
                codeudores = cur.fetchall()
                candidatos = []
                for r in titulares:
                    candidatos.append({"inmueble_id": r[0], "nombre": r[1], "identificacion": r[2], "tipo_relacion": "TITULAR", "radicado_interno": None, "es_principal": None, "estado": None})
                for r in codeudores:
                    candidatos.append({"inmueble_id": r[0], "nombre": r[1], "identificacion": r[2], "tipo_relacion": "CODEUDOR", "radicado_interno": r[3], "es_principal": r[4], "estado": r[5]})
                if not candidatos:
                    return f"SISTEMA: No encontramos obligaciones relacionadas con la cedula {cedula_limpia}."
                def prioridad(c):
                    activo = str(c.get("estado") or "").strip().lower() == "activo"
                    principal = bool(c.get("es_principal"))
                    if activo and principal: return 0
                    if activo: return 1
                    if c["tipo_relacion"] == "TITULAR": return 2
                    return 3
                seleccionado = sorted(candidatos, key=lambda c: (prioridad(c), int(c["inmueble_id"] or 0)))[0]
                inmueble_id = int(seleccionado["inmueble_id"])
                nombre = seleccionado["nombre"]
                identificacion = seleccionado["identificacion"]
                tipo_relacion = seleccionado["tipo_relacion"]
                radicado_interno = seleccionado["radicado_interno"]
                estado = seleccionado["estado"]
        if numero_cliente:
            obligaciones_activas[numero_cliente] = {"cedula": cedula_limpia, "inmueble_id": inmueble_id, "nombre": nombre, "identificacion": identificacion, "tipo_relacion": tipo_relacion, "radicado_interno": radicado_interno}
        try:
            datos = solicitar_liquidacion(inmueble_id, fecha_colombia())
        except Exception:
            return f"[SISTEMA INTERNO - IDENTIDAD CONFIRMADA]\n\nDeudor: {nombre}\nCedula: {identificacion}\nRelacion: {tipo_relacion}\nProceso: {radicado_interno or 'No aplica'}\n\nEl motor financiero central no esta disponible. NO informar valores de deuda; escalar a un asesor."
        capital = datos.get("capital", 0.0)
        intereses = datos.get("intereses", 0.0)
        honorarios = datos.get("honorarios", 0.0)
        gastos = datos.get("gastos", 0.0)
        gran_total = datos.get("gran_total", datos.get("total_exigible", 0.0))
        honorarios_pct = datos.get("honorarios_pct", 23.8)
        if numero_cliente:
            obligaciones_activas[numero_cliente]["ultima_liquidacion"] = datos
        return f"""[SISTEMA INTERNO - ESTADO DE CUENTA OFICIAL]\n\nDeudor/Codeudor: {nombre} (CC: {identificacion})\nRelacion: {tipo_relacion}\nInmueble ID: {inmueble_id}\nProceso: {radicado_interno or 'No aplica'}\nEstado proceso: {estado or 'No aplica'}\n\nSALDO DE CAPITAL: ${float(capital):,.0f}\nINTERESES DE MORA: ${float(intereses):,.0f}\nHONORARIOS DE ABOGADO ({float(honorarios_pct):g}\%):${float(honorarios):,.0f}\nGASTOS PROCESALES: ${float(gastos):,.0f}\n\nGRAN TOTAL LIQUIDADO A LA FECHA:${float(gran_total):,.0f}\n\nLa informacion financiera proviene exclusivamente del motor de liquidacion central."""
    except Exception as exc:
        print(f"❌ Error Neon: {repr(exc)}", flush=True)
        return "SISTEMA: No fue posible consultar la informacion."


def guardar_auditoria(numero, remitente, mensaje):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'America/Bogota';")
                cur.execute("INSERT INTO auditoria_chats (numero_telefono, remitente, mensaje) VALUES (%s, %s, %s)", (numero, remitente, mensaje))
    except Exception as exc:
        print(f"❌ Error guardando auditoria: {exc}", flush=True)


def guardar_anotacion_crm(cedula, nota):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'America/Bogota';")
                fecha_match = re.search(r"\d{4}-\d{2}-\d{2}", nota)
                cur.execute("""INSERT INTO gestiones_cartera (identificacion_deudor, tipo_contacto, resumen, promesa_pago_fecha, usuario) VALUES (%s, %s, %s, %s, %s)""", (cedula, "WhatsApp IA", nota, fecha_match.group(0) if fecha_match else None, "Bot Claude"))
    except Exception as exc:
        print(f"❌ Error CRM: {exc}", flush=True)


def obtener_imagen_base64(id_media):
    try:
        if not TOKEN_META or not id_media: return None, None
        headers = {"Authorization": f"Bearer {TOKEN_META}"}
        respuesta_url = requests.get(f"https://graph.facebook.com/v20.0/{id_media}", headers=headers, timeout=REQUEST_TIMEOUT)
        respuesta_url.raise_for_status()
        url_descarga = respuesta_url.json().get("url")
        if not url_descarga: return None, None
        respuesta_imagen = requests.get(url_descarga, headers=headers, timeout=REQUEST_TIMEOUT)
        respuesta_imagen.raise_for_status()
        mime_type = respuesta_imagen.headers.get("Content-Type", "image/jpeg").split(";", 1)[0].lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            return None, None
        max_bytes = 8 * 1024 * 1024
        contenido = respuesta_imagen.content
        if len(contenido) > max_bytes:
            raise RuntimeError("La imagen supera el limite permitido")
        return base64.b64encode(contenido).decode(), mime_type
    except Exception as exc:
        print(f"❌ Error descargando imagen: {exc}", flush=True)
        return None, None


def enviar_pdf_whatsapp(numero_destino, url_pdf, id_mensaje_entrante=None):
    if not TOKEN_META or not ID_NUMERO_TELEFONO:
        raise RuntimeError("Configuracion de Meta incompleta")
    url = f"https://graph.facebook.com/v20.0/{ID_NUMERO_TELEFONO}/messages"
    headers = {"Authorization": f"Bearer {TOKEN_META}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero_destino, "type": "document", "document": {"link": url_pdf, "caption": "📄 Aqui tiene su estado de cuenta oficial detallado.", "filename": "Liquidacion_Estado_Cuenta.pdf"}}
    if id_mensaje_entrante: payload["context"] = {"message_id": id_mensaje_entrante}
    r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"status_code": r.status_code}


def enviar_mensaje_whatsapp(numero_destino, texto, id_mensaje_entrante=None):
    if not TOKEN_META or not ID_NUMERO_TELEFONO:
        raise RuntimeError("Configuracion de Meta incompleta")
    texto = str(texto or "").strip()
    if not texto:
        raise ValueError("No se puede enviar un mensaje vacio")
    url = f"https://graph.facebook.com/v20.0/{ID_NUMERO_TELEFONO}/messages"
    headers = {"Authorization": f"Bearer {TOKEN_META}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero_destino, "type": "text", "text": {"body": texto}}
    if id_mensaje_entrante: payload["context"] = {"message_id": id_mensaje_entrante}
    r = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"status_code
