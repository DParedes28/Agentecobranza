"""Control humano y trazabilidad persistente del agente de cobranza."""

import json
import os
import re
import threading

import psycopg2
from flask import jsonify, request

DATABASE_URL = os.getenv("DATABASE_URL")
SUPERVISION_KEY = os.getenv("AGENT_SUPERVISION_KEY") or os.getenv("LIQUIDADOR_API_KEY")
_THREAD_STATE = threading.local()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversaciones_agente (
    id BIGSERIAL PRIMARY KEY,
    telefono TEXT NOT NULL UNIQUE,
    identificacion TEXT,
    estado TEXT NOT NULL DEFAULT 'ACTIVA',
    modo_actual TEXT NOT NULL DEFAULT 'AGENTE',
    usuario_humano TEXT,
    fecha_inicio TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fecha_ultima_actividad TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tomada_en TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_conversaciones_agente_modo ON conversaciones_agente (modo_actual);
CREATE INDEX IF NOT EXISTS idx_conversaciones_agente_actividad ON conversaciones_agente (fecha_ultima_actividad DESC);
CREATE TABLE IF NOT EXISTS mensajes_agente (
    id BIGSERIAL PRIMARY KEY,
    conversacion_id BIGINT NOT NULL REFERENCES conversaciones_agente(id) ON DELETE CASCADE,
    direccion TEXT NOT NULL,
    autor TEXT NOT NULL,
    contenido TEXT,
    mensaje_meta_id TEXT UNIQUE,
    tipo_mensaje TEXT,
    fecha TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_mensajes_agente_conversacion ON mensajes_agente (conversacion_id, fecha DESC);
CREATE TABLE IF NOT EXISTS control_agente (
    id BIGSERIAL PRIMARY KEY,
    conversacion_id BIGINT NOT NULL REFERENCES conversaciones_agente(id) ON DELETE CASCADE,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    fecha TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_control_agente_conversacion ON control_agente (conversacion_id, fecha DESC);
"""


def _connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no esta configurada")
    return psycopg2.connect(DATABASE_URL)


def ensure_schema():
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def _normalizar_telefono(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit() or ch == "+")


def _json(value):
    return json.dumps(value or {}, ensure_ascii=False)


def _conversation_id(cur, telefono, identificacion=None):
    telefono = _normalizar_telefono(telefono)
    cur.execute("""
        INSERT INTO conversaciones_agente (telefono, identificacion)
        VALUES (%s, %s)
        ON CONFLICT (telefono) DO UPDATE SET
            identificacion = COALESCE(EXCLUDED.identificacion, conversaciones_agente.identificacion),
            fecha_ultima_actividad = NOW()
        RETURNING id
    """, (telefono, identificacion))
    return int(cur.fetchone()[0])


def record_message(telefono, direccion, autor, contenido, mensaje_meta_id=None,
                   tipo_mensaje="text", metadata=None, identificacion=None):
    try:
        with _connection() as conn:
            with conn.cursor() as cur:
                cid = _conversation_id(cur, telefono, identificacion)
                cur.execute("""
                    INSERT INTO mensajes_agente
                        (conversacion_id, direccion, autor, contenido, mensaje_meta_id, tipo_mensaje, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (mensaje_meta_id) DO NOTHING
                """, (cid, direccion, autor, contenido, mensaje_meta_id, tipo_mensaje, _json(metadata)))
                conn.commit()
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo persistir mensaje: {exc!r}", flush=True)


def _mode(telefono):
    telefono = _normalizar_telefono(telefono)
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT modo_actual FROM conversaciones_agente WHERE telefono=%s", (telefono,))
            row = cur.fetchone()
            return str(row[0]).upper() if row else "AGENTE"


def agent_can_respond(telefono):
    try:
        return _mode(telefono) != "HUMANO"
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo leer modo; se mantiene AGENTE: {exc!r}", flush=True)
        return True


def _set_mode(telefono, usuario, modo, accion):
    telefono = _normalizar_telefono(telefono)
    usuario = str(usuario or "Supervisor ERP").strip()[:120] or "Supervisor ERP"
    with _connection() as conn:
        with conn.cursor() as cur:
            cid = _conversation_id(cur, telefono)
            if modo == "HUMANO":
                cur.execute("UPDATE conversaciones_agente SET modo_actual='HUMANO', usuario_humano=%s, tomada_en=NOW(), fecha_ultima_actividad=NOW() WHERE id=%s", (usuario, cid))
            else:
                cur.execute("UPDATE conversaciones_agente SET modo_actual='AGENTE', usuario_humano=NULL, tomada_en=NULL, fecha_ultima_actividad=NOW() WHERE id=%s", (cid,))
            cur.execute("INSERT INTO control_agente (conversacion_id, usuario, accion) VALUES (%s, %s, %s)", (cid, usuario, accion))
            conn.commit()


def _auth():
    if not SUPERVISION_KEY:
        return False, "AGENT_SUPERVISION_KEY no esta configurada"
    return request.headers.get("X-Agent-Supervision-Key", "") == SUPERVISION_KEY, "No autorizado"


def _register_routes(module):
    @module.app.get("/control/health")
    def control_health():
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        try:
            ensure_schema()
            return jsonify({"status": "ok", "modo": "control_humano"})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.get("/control/conversaciones")
    def control_conversaciones():
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        try:
            ensure_schema()
            limit = min(max(int(request.args.get("limit", 100)), 1), 200)
            buscar = str(request.args.get("buscar", "")).strip()
            with _connection() as conn:
                with conn.cursor() as cur:
                    params = []
                    sql = """
                        SELECT c.id, c.telefono, c.identificacion, c.estado, c.modo_actual,
                               c.usuario_humano, c.fecha_inicio, c.fecha_ultima_actividad,
                               c.tomada_en, COUNT(m.id) AS total_mensajes
                        FROM conversaciones_agente c
                        LEFT JOIN mensajes_agente m ON m.conversacion_id=c.id
                    """
                    if buscar:
                        sql += " WHERE c.telefono ILIKE %s OR COALESCE(c.identificacion,'') ILIKE %s "
                        params.extend([f"%{buscar}%", f"%{buscar}%"])
                    sql += " GROUP BY c.id ORDER BY c.fecha_ultima_actividad DESC LIMIT %s"
                    params.append(limit)
                    cur.execute(sql, params)
                    filas = cur.fetchall()
            nombres = ["id", "telefono", "identificacion", "estado", "modo_actual", "usuario_humano", "fecha_inicio", "fecha_ultima_actividad", "tomada_en", "total_mensajes"]
            return jsonify({"status": "success", "conversaciones": [dict(zip(nombres, r)) for r in filas]})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.get("/control/conversaciones/<path:telefono>/mensajes")
    def control_mensajes(telefono):
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        try:
            ensure_schema()
            limit = min(max(int(request.args.get("limit", 500)), 1), 1000)
            with _connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT m.id, m.direccion, m.autor, m.contenido, m.mensaje_meta_id,
                               m.tipo_mensaje, m.fecha, m.metadata, c.telefono,
                               c.identificacion, c.modo_actual, c.usuario_humano
                        FROM conversaciones_agente c
                        JOIN mensajes_agente m ON m.conversacion_id=c.id
                        WHERE c.telefono=%s ORDER BY m.fecha ASC LIMIT %s
                    """, (_normalizar_telefono(telefono), limit))
                    filas = cur.fetchall()
            nombres = ["id", "direccion", "autor", "contenido", "mensaje_meta_id", "tipo_mensaje", "fecha", "metadata", "telefono", "identificacion", "modo_actual", "usuario_humano"]
            return jsonify({"status": "success", "mensajes": [dict(zip(nombres, r)) for r in filas]})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.post("/control/tomar")
    def control_tomar():
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        payload = request.get_json(silent=True) or {}
        telefono = payload.get("telefono")
        usuario = payload.get("usuario") or "Supervisor ERP"
        if not telefono:
            return jsonify({"status": "error", "mensaje": "telefono requerido"}), 400
        try:
            ensure_schema()
            _set_mode(telefono, usuario, "HUMANO", "TOMAR_CONTROL")
            return jsonify({"status": "success", "telefono": _normalizar_telefono(telefono), "modo_actual": "HUMANO", "usuario": usuario})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.post("/control/devolver")
    def control_devolver():
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        payload = request.get_json(silent=True) or {}
        telefono = payload.get("telefono")
        usuario = payload.get("usuario") or "Supervisor ERP"
        if not telefono:
            return jsonify({"status": "error", "mensaje": "telefono requerido"}), 400
        try:
            ensure_schema()
            _set_mode(telefono, usuario, "AGENTE", "DEVOLVER_AL_AGENTE")
            return jsonify({"status": "success", "telefono": _normalizar_telefono(telefono), "modo_actual": "AGENTE"})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.post("/control/mensaje")
    def control_mensaje():
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        payload = request.get_json(silent=True) or {}
        telefono = payload.get("telefono")
        texto = str(payload.get("texto") or "").strip()
        usuario = str(payload.get("usuario") or "Supervisor ERP").strip()
        if not telefono or not texto:
            return jsonify({"status": "error", "mensaje": "telefono y texto requeridos"}), 400
        try:
            if _mode(telefono) != "HUMANO":
                return jsonify({"status": "error", "mensaje": "La conversacion no esta bajo control humano"}), 409
            _THREAD_STATE.autor = "HUMANO"
            _THREAD_STATE.usuario = usuario
            module.enviar_mensaje_whatsapp(_normalizar_telefono(telefono), texto, None)
            return jsonify({"status": "success"})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500
        finally:
            _THREAD_STATE.autor = None
            _THREAD_STATE.usuario = None


def persist_incoming(module, data):
    try:
        value = data["entry"][0]["changes"][0]["value"]
        message = (value.get("messages") or [{}])[0]
        telefono = message.get("from")
        if not telefono:
            return True
        mid = message.get("id")
        tipo = message.get("type", "desconocido")
        if tipo == "text":
            texto = message.get("text", {}).get("body", "")[:4000]
        elif tipo == "image":
            texto = "[Imagen recibida; presumiblemente comprobante de pago]"
        else:
            texto = f"[Mensaje tipo {tipo}]"
        match = re.search(r"\d{6,12}", texto or "")
        identificacion = match.group(0) if match else None
        record_message(telefono, "ENTRANTE", "DEUDOR", texto, mensaje_meta_id=mid, tipo_mensaje=tipo, identificacion=identificacion)
        return agent_can_respond(telefono)
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo interceptar mensaje entrante: {exc!r}", flush=True)
        return True


def install(module):
    if getattr(module, "_HUMAN_CONTROL_INSTALLED", False):
        return
    ensure_schema()
    module._HUMAN_ORIGINAL_SEND = module.enviar_mensaje_whatsapp
    module._HUMAN_ORIGINAL_PDF = getattr(module, "enviar_pdf_whatsapp", None)

    def enviar_mensaje_wrapped(telefono, texto, message_id=None):
        result = module._HUMAN_ORIGINAL_SEND(telefono, texto, message_id)
        autor = getattr(_THREAD_STATE, "autor", None) or "AGENTE"
        metadata = {}
        if getattr(_THREAD_STATE, "usuario", None):
            metadata["usuario"] = _THREAD_STATE.usuario
        record_message(telefono, "SALIENTE", autor, texto, metadata=metadata)
        return result

    module.enviar_mensaje_whatsapp = enviar_mensaje_wrapped
    _register_routes(module)
    module._HUMAN_CONTROL_INSTALLED = True
    print("[CONTROL HUMANO] Conversaciones persistentes y control humano habilitados", flush=True)
