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


def _fecha_iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value).strip()
    return text or None


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
                           c.identificacion, pp.radicado_interno,
                           pp.es_principal, p.estado, pp.fecha_vinculacion
                    FROM proceso_partes pp
                    INNER JOIN procesos p ON p.radicado_interno = pp.radicado_interno
                    INNER JOIN contactos c ON c.id = pp.contacto_id
                    WHERE pp.rol = 'DEMANDADO'
                      AND REGEXP_REPLACE(COALESCE(c.identificacion::text, ''), '[^0-9]', '', 'g') = %s
                    ORDER BY CASE WHEN LOWER(COALESCE(p.estado, '')) = 'activo' THEN 0 ELSE 1 END,
                             CASE WHEN COALESCE(pp.es_principal, false) THEN 0 ELSE 1 END,
                             p.inmueble_id,
                             pp.fecha_vinculacion DESC NULLS LAST
                """, (cedula_limpia,))
                codeudores = cur.fetchall()
                candidatos = []
                for r in titulares:
                    candidatos.append({"inmueble_id": r[0], "nombre": r[1], "identificacion": r[2], "tipo_relacion": "TITULAR", "radicado_interno": None, "es_principal": None, "fecha_vinculacion": None, "estado": None})
                for r in codeudores:
                    candidatos.append({"inmueble_id": r[0], "nombre": r[1], "identificacion": r[2], "tipo_relacion": "CODEUDOR", "radicado_interno": r[3], "es_principal": r[4], "fecha_vinculacion": _fecha_iso(r[6]), "estado": r[5]})
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
                es_principal = seleccionado["es_principal"]
                fecha_vinculacion = seleccionado["fecha_vinculacion"]
                estado = seleccionado["estado"]
        if numero_cliente:
            obligaciones_activas[numero_cliente] = {"cedula": cedula_limpia, "inmueble_id": inmueble_id, "nombre": nombre, "identificacion": identificacion, "tipo_relacion": tipo_relacion, "radicado_interno": radicado_interno, "es_principal": es_principal, "fecha_vinculacion": fecha_vinculacion}
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
        return f"""[SISTEMA INTERNO - ESTADO DE CUENTA OFICIAL]\n\nDeudor/Codeudor: {nombre} (CC: {identificacion})\nRelacion: {tipo_relacion}\nInmueble ID: {inmueble_id}\nProceso: {radicado_interno or 'No aplica'}\nEstado proceso: {estado or 'No aplica'}\n\nSALDO DE CAPITAL: ${float(capital):,.0f}\nINTERESES DE MORA: ${float(intereses):,.0f}\nHONORARIOS DE ABOGADO ({float(honorarios_pct):g}%): ${float(honorarios):,.0f}\nGASTOS PROCESALES: ${float(gastos):,.0f}\n\nGRAN TOTAL LIQUIDADO A LA FECHA: ${float(gran_total):,.0f}\n\nLa informacion financiera proviene exclusivamente del motor de liquidacion central."""
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
        return {"status_code": r.status_code}


@app.route("/health", methods=["GET"])
def health():
    required = {
        "DATABASE_URL": DATABASE_URL,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "TOKEN_META": TOKEN_META,
        "ID_NUMERO_TELEFONO": ID_NUMERO_TELEFONO,
        "TOKEN_VERIFICACION": TOKEN_VERIFICACION,
        "META_APP_SECRET": META_APP_SECRET,
        "LIQUIDADOR_API_URL": LIQUIDADOR_API_URL,
        "LIQUIDADOR_API_KEY": LIQUIDADOR_API_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    status = 200 if not missing else 503
    return jsonify({"status": "ok" if not missing else "degraded", "modelo": ANTHROPIC_MODEL, "missing": missing}), status


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), TOKEN_VERIFICACION or ""):
        return request.args.get("hub.challenge", ""), 200
    return "Error", 403


@app.route("/webhook", methods=["POST"])
def recibir_mensajes():
    raw_body = request.get_data(cache=True)
    if not verificar_firma_meta(raw_body):
        return jsonify({"status": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"status": "ignored"}), 400
    hilo = threading.Thread(target=procesar_y_responder, args=(data,), daemon=True)
    hilo.start()
    return jsonify({"status": "success"}), 200


_EXPLICIT_CEDULA_RE = re.compile(
    r"(?:"
    r"mi\s+c[eé]dula\s+(?:es\s+)?"
    r"|c[eé]dula(?:\s+de\s+ciudadan[ií]a)?\s*(?:es|:|=)?"
    r"|n[uú]mero\s+de\s+c[eé]dula\s*(?:es|:|=)?"
    r"|cc\s*[:.\-]?\s*"
    r"|c\.?\s*c\.?\s*[:.\-]?\s*"
    r")"
    r"(\d[\d.\s]{4,14}\d)",
    re.IGNORECASE,
)
_AMOUNT_TOKEN_RE = re.compile(
    r"(?:\$|usd|cop)?\s*\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d+\s*(?:pesos|cop|usd)",
    re.IGNORECASE,
)
_CLASIFICACION_IMAGEN_RE = re.compile(
    r"\[CLASIFICACION_IMAGEN:\s*(comprobante|carta-cobro|cedula|otro)\]",
    re.IGNORECASE,
)
_REPORTAR_ABONO_RE = re.compile(
    r"\[ACCION:\s*REPORTAR_ABONO\s*\|\s*Monto=([\d.]+)\s*\|\s*Fecha=([\d-]+)\s*\|\s*Banco=([^|]+)\s*\|\s*Ref=([^\]]+)\]",
    re.IGNORECASE,
)
AUDIO_ESCALATION_THRESHOLD = 2


def es_celular_co(digits: str) -> bool:
    """Celulares CO típicos: 3XXXXXXXXX o 57 + 10 dígitos."""
    clean = re.sub(r"\D", "", str(digits or ""))
    if re.fullmatch(r"3\d{9}", clean):
        return True
    if re.fullmatch(r"57\d{10}", clean):
        return True
    return False


def _token_parece_monto(token: str, texto: str, start: int, end: int) -> bool:
    """True si el match parece un monto ($ / pesos / separadores de miles)."""
    raw = token or ""
    window = (texto or "")[max(0, start - 12) : min(len(texto or ""), end + 12)]
    lower = window.lower()
    if "$" in window or "peso" in lower or "cop" in lower:
        return True
    if _AMOUNT_TOKEN_RE.search(window):
        return True
    # Miles CO (745.901) o US (745,901) sin tratar el bloque entero como cédula.
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) or re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw):
        return True
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+[.,]\d{1,2}", raw):
        return True
    return False


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def cedula_explicita_en_texto(texto: str):
    """Extrae cédula solo si hay frase explícita (mi cédula es… / CC …)."""
    if not texto:
        return None
    for match in _EXPLICIT_CEDULA_RE.finditer(texto):
        digits = _digits_only(match.group(1))
        if 6 <= len(digits) <= 12 and not es_celular_co(digits):
            return digits
    return None


def extraer_cedula(texto, *, sesion_cedula=None, identidad_confirmada=False):
    """Parser contextual: ignora montos y celulares; respeta sesión anclada.

    Si la sesión ya tiene identidad confirmada, solo acepta una cédula nueva
    cuando el usuario la declara de forma explícita.
    """
    texto = texto or ""
    if identidad_confirmada and sesion_cedula:
        explicit = cedula_explicita_en_texto(texto)
        if explicit:
            return explicit
        return str(sesion_cedula)

    explicit = cedula_explicita_en_texto(texto)
    if explicit:
        return explicit

    # Candidatos por token (sin strip ciego de puntos sobre todo el texto).
    for match in re.finditer(r"\d[\d.,\s]{4,14}\d", texto):
        token = match.group(0)
        if _token_parece_monto(token, texto, match.start(), match.end()):
            continue
        digits = _digits_only(token)
        if not (6 <= len(digits) <= 12):
            continue
        if es_celular_co(digits):
            continue
        return digits
    return None


def texto_desde_imagen_meta(mensaje_info: dict) -> str:
    """Caption de Meta si existe; placeholder neutro (sin presumir comprobante)."""
    imagen = (mensaje_info or {}).get("image") or {}
    caption = str(imagen.get("caption") or "").strip()
    if caption:
        return caption[:4000]
    return (
        "[El usuario envio una imagen. Clasifica primero: comprobante, "
        "carta-cobro, cedula u otro. No asumas que es un comprobante de pago.]"
    )


def sesion_identidad_anclada(state: dict | None) -> bool:
    state = state or {}
    return bool(
        state.get("identidad_confirmada")
        and state.get("cedula")
        and state.get("inmueble_id")
    )


def contexto_sesion_activa(state: dict) -> str:
    return (
        "[SISTEMA INTERNO - SESION ACTIVA]\n"
        f"Cedula confirmada: {state.get('cedula')}\n"
        f"Inmueble ID: {state.get('inmueble_id')}\n"
        f"Nombre: {state.get('nombre') or 'N/D'}\n"
        f"Referencia: {state.get('property_reference') or 'N/D'}\n"
        "La identidad de esta conversacion ya esta confirmada. "
        "No vuelvas a pedir la misma cedula ni consultes otra cifra como cedula "
        "salvo que el usuario diga explicitamente 'mi cedula es…' o 'CC …'."
    )


def clasificacion_imagen_respuesta(texto: str) -> str | None:
    match = _CLASIFICACION_IMAGEN_RE.search(texto or "")
    return match.group(1).lower() if match else None


def debe_reportar_abono(respuesta: str) -> bool:
    """REPORTAR_ABONO solo si la clasificación es comprobante."""
    if not _REPORTAR_ABONO_RE.search(respuesta or ""):
        return False
    return clasificacion_imagen_respuesta(respuesta) == "comprobante"


def escalar_a_humano(telefono: str, motivo: str = "ESCALAR_HUMANO") -> bool:
    """Pasa la conversación a modo HUMANO y avisa si hay webhook."""
    try:
        import control_humano

        return bool(control_humano.escalar_humano(telefono, motivo=motivo))
    except Exception as exc:
        print(f"⚠️ No se pudo escalar a humano: {exc!r}", flush=True)
        return False


def _manejar_audio_o_no_soportado(numero_cliente, tipo_mensaje, id_mensaje_entrante) -> bool:
    """Primer audio: pedir texto. Segundo audio: escalar. Otros tipos: aviso breve."""
    state = obligaciones_activas.get(numero_cliente, {}) or {}
    if tipo_mensaje == "audio":
        count = int(state.get("audio_count") or 0) + 1
        state["audio_count"] = count
        obligaciones_activas[numero_cliente] = state
        if count >= AUDIO_ESCALATION_THRESHOLD:
            enviar_mensaje_whatsapp(
                numero_cliente,
                "Un asesor del despacho continuara la gestion en este mismo chat. Gracias por tu paciencia.",
                id_mensaje_entrante,
            )
            escalar_a_humano(numero_cliente, motivo="AUDIO_REPETIDO")
            return True
        enviar_mensaje_whatsapp(
            numero_cliente,
            "Por protocolos de seguridad y auditoria no puedo procesar notas de voz. "
            "Por favor escribe tu mensaje en texto. Si prefieres, un asesor humano puede continuar.",
            id_mensaje_entrante,
        )
        return True
    enviar_mensaje_whatsapp(
        numero_cliente,
        "Por ahora puedo procesar mensajes de texto e imagenes. "
        "Si necesitas un asesor humano, indicalo por texto.",
        id_mensaje_entrante,
    )
    return True


def procesar_y_responder(data):
    try:
        valor = data["entry"][0]["changes"][0]["value"]
        if valor.get("messaging_product") != "whatsapp" or "messages" not in valor: return False
        mensaje_info = valor["messages"][0]
        contacto = valor.get("contacts", [{}])[0]
        numero_cliente = mensaje_info.get("from") or contacto.get("wa_id") or mensaje_info.get("from_user_id")
        if not numero_cliente: return False
        id_mensaje_entrante = mensaje_info.get("id")
        if not id_mensaje_entrante: return False
        tipo_mensaje = mensaje_info.get("type", "desconocido")
        if tipo_mensaje not in ["text", "image"]:
            return _manejar_audio_o_no_soportado(numero_cliente, tipo_mensaje, id_mensaje_entrante)
        texto_recibido = ""
        imagen_b64 = None
        mime_type = None
        if tipo_mensaje == "text":
            texto_recibido = mensaje_info.get("text", {}).get("body", "")[:4000]
        else:
            id_media = mensaje_info.get("image", {}).get("id")
            imagen_b64, mime_type = obtener_imagen_base64(id_media)
            texto_recibido = texto_desde_imagen_meta(mensaje_info)
        guardar_auditoria(numero_cliente, "Deudor", texto_recibido)
        memoria_chats.setdefault(numero_cliente, [])
        state = obligaciones_activas.get(numero_cliente, {}) or {}
        anclada = sesion_identidad_anclada(state)
        cedula_explicita = cedula_explicita_en_texto(texto_recibido)
        cedula_detectada = extraer_cedula(
            texto_recibido,
            sesion_cedula=state.get("cedula"),
            identidad_confirmada=anclada,
        )
        # Con sesión anclada no re-escanear memoria ni re-consultar, salvo cédula explícita nueva.
        if anclada and not cedula_explicita:
            contexto_financiero = contexto_sesion_activa(state)
            cedula_detectada = str(state.get("cedula"))
        else:
            if not cedula_detectada and not anclada:
                cedula_detectada = extraer_cedula("\n".join(memoria_chats[numero_cliente]))
            contexto_financiero = (
                buscar_deuda_en_neon(cedula_detectada, numero_cliente) if cedula_detectada else ""
            )
        anotacion_usuario = f"Deudor dice: {texto_recibido}"
        if contexto_financiero: anotacion_usuario += f"\n[SISTEMA INTERNO: {contexto_financiero}]"
        memoria_chats[numero_cliente].append(anotacion_usuario)
        historial_reciente = "\n".join(memoria_chats[numero_cliente][-8:])
        contenido_usuario = [{"type": "text", "text": "Historial de la conversacion:\n" + historial_reciente + "\nGenera la respuesta basandote en este historial."}]
        if imagen_b64:
            contenido_usuario.insert(0, {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": imagen_b64}})
            contenido_usuario[-1]["text"] += (
                "\nClasifica la imagen con [CLASIFICACION_IMAGEN: comprobante|carta-cobro|cedula|otro]. "
                "Solo si es comprobante puedes extraer Monto/Fecha/Banco/Ref y, preferiblemente tras "
                "confirmacion del usuario, usar [ACCION: REPORTAR_ABONO | ...]. "
                "Si es carta-cobro u otro, NO declares pago ni uses REPORTAR_ABONO."
            )

        if not cliente_ia: raise RuntimeError("ANTHROPIC_API_KEY no esta configurada")

        # Fuente unica: prompt_policy (sitecustomize tambien lo inyecta; evita divergencia).
        from prompt_policy import SYSTEM_PROMPT as system_prompt

        respuesta_ia = cliente_ia.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": contenido_usuario}]
        )
        respuesta_cruda = respuesta_ia.content[0].text

        if "[ACCION: ESCALAR_HUMANO]" in respuesta_cruda:
            escalar_a_humano(numero_cliente, motivo="ESCALAR_HUMANO")
            respuesta_cruda = respuesta_cruda.replace("[ACCION: ESCALAR_HUMANO]", "").strip()

        # Procesar abono solo si la clasificación de imagen es comprobante.
        match_abono = _REPORTAR_ABONO_RE.search(respuesta_cruda)
        if match_abono:
            if debe_reportar_abono(respuesta_cruda):
                monto_val = match_abono.group(1).strip()
                fecha_val = match_abono.group(2).strip()
                banco_val = match_abono.group(3).strip()
                ref_val = match_abono.group(4).strip()
                obligacion = obligaciones_activas.get(numero_cliente)
                inm_id = obligacion.get("inmueble_id") if obligacion else None
                if inm_id:
                    reportar_abono_al_erp(inm_id, monto_val, fecha_val, banco_val, ref_val)
            else:
                print(
                    "ℹ️ REPORTAR_ABONO ignorado: clasificacion de imagen no es comprobante",
                    flush=True,
                )
            respuesta_cruda = _REPORTAR_ABONO_RE.sub("", respuesta_cruda).strip()
        respuesta_cruda = _CLASIFICACION_IMAGEN_RE.sub("", respuesta_cruda).strip()

        quiere_pdf = "[ACCION: ENVIAR_PDF]" in respuesta_cruda
        respuesta_cruda = respuesta_cruda.replace("[ACCION: ENVIAR_PDF]", "").strip()
        etiqueta_crm = re.search(r"\[RESUMEN_FINAL:(.*?)\]", respuesta_cruda, re.DOTALL)
        if etiqueta_crm:
            nota_secreta = etiqueta_crm.group(1).strip()
            if cedula_detectada: guardar_anotacion_crm(cedula_detectada, nota_secreta)
            respuesta_cruda = re.sub(r"\[RESUMEN_FINAL:.*?\]", "", respuesta_cruda, flags=re.DOTALL).strip()
            memoria_chats[numero_cliente] = []
        respuesta_limpia = respuesta_cruda.strip()
        memoria_chats.setdefault(numero_cliente, []).append(f"Tu respondiste: {respuesta_limpia}")
        guardar_auditoria(numero_cliente, "Bot IA", respuesta_limpia)
        enviar_mensaje_whatsapp(numero_cliente, respuesta_limpia, id_mensaje_entrante)
        if quiere_pdf:
            obligacion = obligaciones_activas.get(numero_cliente)
            if not obligacion:
                enviar_mensaje_whatsapp(numero_cliente, "⚠️ Para generar el documento oficial necesito identificar primero la obligacion asociada a su cedula.", id_mensaje_entrante)
                return True
            try:
                datos_pdf = solicitar_liquidacion(obligacion["inmueble_id"], fecha_colombia())
                enlace_pdf = datos_pdf.get("url_pdf")
                if enlace_pdf: enviar_pdf_whatsapp(numero_cliente, enlace_pdf, id_mensaje_entrante)
                else: enviar_mensaje_whatsapp(numero_cliente, "⚠️ El documento oficial aun no tiene una URL disponible. Un asesor continuara la gestion.", id_mensaje_entrante)
            except Exception as exc:
                print(f"❌ Error PDF: {repr(exc)}", flush=True)
                enviar_mensaje_whatsapp(numero_cliente, "⚠️ Hubo un inconveniente generando el documento oficial. Un asesor continuara la gestion.", id_mensaje_entrante)
        return True
    except Exception as exc:
        print(f"❌ Error interno procesando mensaje: {repr(exc)}", flush=True)
        return False


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

