"""Control humano y trazabilidad persistente del agente de cobranza."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import psycopg2
import requests
from flask import jsonify, request, send_file

DATABASE_URL = os.getenv("DATABASE_URL")
SUPERVISION_KEY = os.getenv("AGENT_SUPERVISION_KEY")
TOKEN_META = os.getenv("TOKEN_META")
_THREAD_STATE = threading.local()
_MEDIA_DIR = Path(os.getenv("BOT_MEDIA_DIR") or "/tmp/bot_media_supervision")
_MEDIA_TTL = int(os.getenv("BOT_MEDIA_URL_TTL", "604800"))  # 7 días
_MEDIA_SECRET = (
    os.getenv("BOT_MEDIA_PUBLIC_SECRET")
    or SUPERVISION_KEY
    or TOKEN_META
    or ""
).strip()

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


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def es_telefono_whatsapp_legible(value: Any) -> bool:
    """True si parece celular E.164 / CO; False para PSID u otros IDs opacos de Meta."""
    d = _digits(value)
    if not d:
        return False
    if len(d) >= 15:
        return False
    if d.startswith("57") and len(d) == 12:
        return True
    if len(d) == 10 and d.startswith("3"):
        return True
    if 11 <= len(d) <= 14:
        return True
    return False


def _json(value):
    return json.dumps(value or {}, ensure_ascii=False)


def _parse_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _public_base_url() -> str:
    return (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or "https://bot-cobranzas-ph.onrender.com"
    ).rstrip("/")


def _sign_media(filename: str, expires: int) -> str:
    payload = f"{filename}|{expires}".encode("utf-8")
    return hmac.new(_MEDIA_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_media_url(filename: str, base_url: Optional[str] = None) -> str:
    if not _MEDIA_SECRET:
        raise RuntimeError("BOT_MEDIA_PUBLIC_SECRET/AGENT_SUPERVISION_KEY no está configurada")
    expires = int(time.time()) + _MEDIA_TTL
    token = _sign_media(filename, expires)
    root = (base_url or _public_base_url()).rstrip("/")
    return f"{root}/control/media/{quote(filename, safe='')}?expires={expires}&token={token}"


def _ext_from_mime(mime: str) -> str:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
    }.get(mime, ".bin")


def descargar_media_meta(media_id: str) -> tuple[Optional[bytes], Optional[str]]:
    """Descarga bytes + mime desde Graph API. Retorna (None, None) si falla."""
    if not TOKEN_META or not media_id:
        return None, None
    try:
        headers = {"Authorization": f"Bearer {TOKEN_META}"}
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers=headers,
            timeout=30,
        )
        meta.raise_for_status()
        url = (meta.json() or {}).get("url")
        if not url:
            return None, None
        blob = requests.get(url, headers=headers, timeout=30)
        blob.raise_for_status()
        mime = blob.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
        content = blob.content
        if len(content) > 12 * 1024 * 1024:
            print(f"[CONTROL HUMANO] Media {media_id} supera 12MB; se omite persistencia local", flush=True)
            return None, mime
        return content, mime
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo descargar media {media_id}: {exc!r}", flush=True)
        return None, None


def persistir_media_local(media_id: str, content: bytes, mime: str) -> Optional[str]:
    """Guarda bytes en disco y retorna filename relativo, o None."""
    if not content or not media_id:
        return None
    try:
        _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(media_id))[:80]
        filename = f"{safe_id}{_ext_from_mime(mime)}"
        path = _MEDIA_DIR / filename
        if not path.is_file():
            path.write_bytes(content)
        return filename
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo guardar media local: {exc!r}", flush=True)
        return None


def resolver_identidad_whatsapp(message: dict, value: dict) -> tuple[str, dict]:
    """Elige clave de conversación: preferir wa_id E.164; PSID/`CO.` solo como fallback.

    Meta a veces omite `messages[].from` y solo trae `contacts[].wa_id` (incluso opaco
    tipo `CO.144278…`). Sin este fallback, persist_incoming no guarda ENTRANTE.
    """
    message = message or {}
    value = value or {}
    from_id = str(message.get("from") or message.get("from_user_id") or "").strip()
    contacts = value.get("contacts") or [{}]
    contact0 = contacts[0] if isinstance(contacts, list) and contacts else {}
    wa_id = str((contact0 or {}).get("wa_id") or "").strip()

    meta: dict[str, Any] = {}
    if from_id:
        meta["from_id"] = from_id
    if wa_id:
        meta["wa_id_raw"] = wa_id
        meta["wa_id"] = _digits(wa_id) or wa_id

    if es_telefono_whatsapp_legible(wa_id):
        clave = _normalizar_telefono(wa_id)
        if from_id and _digits(from_id) != _digits(wa_id):
            meta["psid"] = _digits(from_id) or from_id
        return clave, meta

    if es_telefono_whatsapp_legible(from_id):
        clave = _normalizar_telefono(from_id)
        meta["wa_id"] = _digits(from_id)
        return clave, meta

    # ID opaco (PSID / CO.<id>): clave = solo dígitos; se guarda crudo en metadata.
    raw = from_id or wa_id
    clave = _normalizar_telefono(raw)
    if not clave and raw:
        clave = _digits(raw) or raw
    if clave:
        meta["psid"] = _digits(clave) or clave
        if str(raw).upper().startswith("CO."):
            meta["meta_user_id"] = raw
    return clave, meta


def preview_contenido(contenido: str, tipo_mensaje: str = "text") -> str:
    """Texto corto para sidebar de supervisión."""
    tipo = str(tipo_mensaje or "text").lower()
    texto = str(contenido or "").strip()
    if tipo == "image" or texto.lower().startswith("[imagen") or "envio una imagen" in texto.lower():
        return "📷 Imagen / comprobante"
    if tipo in {"document", "documento"} or "pdf" in texto.lower()[:40]:
        return "📄 Documento PDF"
    if not texto:
        return ""
    return texto[:160]


def _conversation_id(cur, telefono, identificacion=None, metadata_extra=None):
    telefono = _normalizar_telefono(telefono)
    meta = metadata_extra or {}
    cur.execute(
        """
        INSERT INTO conversaciones_agente (telefono, identificacion, metadata)
        VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (telefono) DO UPDATE SET
            identificacion = COALESCE(EXCLUDED.identificacion, conversaciones_agente.identificacion),
            fecha_ultima_actividad = NOW(),
            metadata = CASE
                WHEN EXCLUDED.metadata = '{}'::jsonb THEN conversaciones_agente.metadata
                ELSE COALESCE(conversaciones_agente.metadata, '{}'::jsonb) || EXCLUDED.metadata
            END
        RETURNING id
        """,
        (telefono, identificacion, _json(meta)),
    )
    return int(cur.fetchone()[0])


def record_message(
    telefono,
    direccion,
    autor,
    contenido,
    mensaje_meta_id=None,
    tipo_mensaje="text",
    metadata=None,
    identificacion=None,
    conversacion_meta=None,
):
    try:
        with _connection() as conn:
            with conn.cursor() as cur:
                cid = _conversation_id(
                    cur,
                    telefono,
                    identificacion,
                    metadata_extra=conversacion_meta,
                )
                cur.execute(
                    """
                    INSERT INTO mensajes_agente
                        (conversacion_id, direccion, autor, contenido, mensaje_meta_id, tipo_mensaje, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (mensaje_meta_id) DO NOTHING
                    """,
                    (
                        cid,
                        direccion,
                        autor,
                        contenido,
                        mensaje_meta_id,
                        tipo_mensaje,
                        _json(metadata),
                    ),
                )
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
        print(f"[CONTROL HUMANO] No se pudo leer modo; se bloquea la IA por seguridad: {exc!r}", flush=True)
        return None


def _set_mode(telefono, usuario, modo, accion):
    telefono = _normalizar_telefono(telefono)
    usuario = str(usuario or "Supervisor ERP").strip()[:120] or "Supervisor ERP"
    with _connection() as conn:
        with conn.cursor() as cur:
            cid = _conversation_id(cur, telefono)
            if modo == "HUMANO":
                cur.execute(
                    "UPDATE conversaciones_agente SET modo_actual='HUMANO', usuario_humano=%s, tomada_en=NOW(), fecha_ultima_actividad=NOW() WHERE id=%s",
                    (usuario, cid),
                )
            else:
                cur.execute(
                    "UPDATE conversaciones_agente SET modo_actual='AGENTE', usuario_humano=NULL, tomada_en=NULL, fecha_ultima_actividad=NOW() WHERE id=%s",
                    (cid,),
                )
            cur.execute(
                "INSERT INTO control_agente (conversacion_id, usuario, accion) VALUES (%s, %s, %s)",
                (cid, usuario, accion),
            )
            conn.commit()


def _avisar_webhook_escalamiento(telefono: str, motivo: str) -> None:
    """POST opcional a SUPERVISION_IA_WEBHOOK_URL / BOT_ESCALATION_WEBHOOK_URL."""
    url = (
        os.getenv("SUPERVISION_IA_WEBHOOK_URL")
        or os.getenv("BOT_ESCALATION_WEBHOOK_URL")
        or ""
    ).strip()
    if not url:
        return
    try:
        import urllib.request

        payload = json.dumps(
            {
                "evento": "escalar_humano",
                "origen": "agentecobranza",
                "telefono": _normalizar_telefono(telefono),
                "motivo": motivo,
                "modo": "HUMANO",
                "mensaje": f"El bot escaló la conversación {telefono} a humano ({motivo})",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as exc:
        print(f"[CONTROL HUMANO] Webhook de escalamiento no disponible: {exc!r}", flush=True)


def escalar_humano(telefono: str, motivo: str = "ESCALAR_HUMANO", usuario: str = "Bot IA") -> bool:
    """Pasa la conversación a modo HUMANO y notifica si hay webhook."""
    try:
        _set_mode(telefono, usuario, "HUMANO", motivo)
        _avisar_webhook_escalamiento(telefono, motivo)
        return True
    except Exception as exc:
        print(f"[CONTROL HUMANO] No se pudo escalar a humano: {exc!r}", flush=True)
        return False


def _auth():
    if not SUPERVISION_KEY:
        return False, "AGENT_SUPERVISION_KEY no esta configurada"
    return request.headers.get("X-Agent-Supervision-Key", "") == SUPERVISION_KEY, "No autorizado"


def _fila_conversacion(row_tuple, nombres):
    fila = dict(zip(nombres, row_tuple))
    meta = _parse_json(fila.get("metadata"))
    fila["metadata"] = meta
    telefono = fila.get("telefono") or ""
    wa_id = meta.get("wa_id") or ""
    psid = meta.get("psid") or ""
    # phone/telefono = clave API (PSID o E.164). wa_id legible va aparte para el ERP.
    if es_telefono_whatsapp_legible(wa_id):
        phone_legible = _digits(wa_id)
    elif es_telefono_whatsapp_legible(telefono):
        phone_legible = _digits(telefono)
    else:
        phone_legible = ""
    fila["phone"] = telefono
    fila["telefono"] = telefono
    fila["conversation_key"] = telefono
    fila["wa_id"] = wa_id or (phone_legible or None)
    fila["phone_legible"] = phone_legible or None
    fila["psid"] = psid or (telefono if not es_telefono_whatsapp_legible(telefono) else None)
    tipo = fila.get("ultimo_tipo_mensaje") or "text"
    contenido = fila.get("ultimo_contenido") or ""
    preview = preview_contenido(contenido, tipo)
    fila["ultimo_mensaje"] = preview
    fila["last_message"] = preview
    fila["ultimo_tipo_mensaje"] = tipo
    return fila


def _fila_mensaje(row_tuple, nombres):
    fila = dict(zip(nombres, row_tuple))
    meta = _parse_json(fila.get("metadata"))
    fila["metadata"] = meta
    media_url = meta.get("media_url") or meta.get("url_media") or meta.get("image_url")
    if media_url:
        fila["media_url"] = media_url
    if meta.get("media_id"):
        fila["media_id"] = meta["media_id"]
    if meta.get("url_pdf"):
        fila["url_pdf"] = meta["url_pdf"]
    return fila


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
                               c.tomada_en, c.metadata, COUNT(m.id) AS total_mensajes,
                               (
                                 SELECT m2.contenido
                                 FROM mensajes_agente m2
                                 WHERE m2.conversacion_id = c.id
                                   AND COALESCE(m2.autor, '') <> 'SISTEMA'
                                 ORDER BY m2.fecha DESC
                                 LIMIT 1
                               ) AS ultimo_contenido,
                               (
                                 SELECT m2.tipo_mensaje
                                 FROM mensajes_agente m2
                                 WHERE m2.conversacion_id = c.id
                                   AND COALESCE(m2.autor, '') <> 'SISTEMA'
                                 ORDER BY m2.fecha DESC
                                 LIMIT 1
                               ) AS ultimo_tipo_mensaje
                        FROM conversaciones_agente c
                        LEFT JOIN mensajes_agente m ON m.conversacion_id=c.id
                    """
                    if buscar:
                        sql += """
                            WHERE c.telefono ILIKE %s
                               OR COALESCE(c.identificacion,'') ILIKE %s
                               OR COALESCE(c.metadata->>'wa_id','') ILIKE %s
                        """
                        params.extend([f"%{buscar}%", f"%{buscar}%", f"%{buscar}%"])
                    sql += " GROUP BY c.id ORDER BY c.fecha_ultima_actividad DESC LIMIT %s"
                    params.append(limit)
                    cur.execute(sql, params)
                    filas = cur.fetchall()
            nombres = [
                "id",
                "telefono",
                "identificacion",
                "estado",
                "modo_actual",
                "usuario_humano",
                "fecha_inicio",
                "fecha_ultima_actividad",
                "tomada_en",
                "metadata",
                "total_mensajes",
                "ultimo_contenido",
                "ultimo_tipo_mensaje",
            ]
            conversaciones = [_fila_conversacion(r, nombres) for r in filas]
            return jsonify({"status": "success", "conversaciones": conversaciones})
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
            clave = _normalizar_telefono(telefono)
            with _connection() as conn:
                with conn.cursor() as cur:
                    # Resolver también por wa_id guardado en metadata (hilos con PSID).
                    cur.execute(
                        """
                        SELECT m.id, m.direccion, m.autor, m.contenido, m.mensaje_meta_id,
                               m.tipo_mensaje, m.fecha, m.metadata, c.telefono,
                               c.identificacion, c.modo_actual, c.usuario_humano
                        FROM conversaciones_agente c
                        JOIN mensajes_agente m ON m.conversacion_id=c.id
                        WHERE c.telefono=%s
                           OR COALESCE(c.metadata->>'wa_id','')=%s
                           OR COALESCE(c.metadata->>'psid','')=%s
                        ORDER BY m.fecha ASC
                        LIMIT %s
                        """,
                        (clave, _digits(clave), _digits(clave), limit),
                    )
                    filas = cur.fetchall()
            nombres = [
                "id",
                "direccion",
                "autor",
                "contenido",
                "mensaje_meta_id",
                "tipo_mensaje",
                "fecha",
                "metadata",
                "telefono",
                "identificacion",
                "modo_actual",
                "usuario_humano",
            ]
            return jsonify({"status": "success", "mensajes": [_fila_mensaje(r, nombres) for r in filas]})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.get("/control/conversaciones/<path:telefono>/control")
    def control_historial(telefono):
        ok, reason = _auth()
        if not ok:
            return jsonify({"status": "error", "mensaje": reason}), 401
        try:
            ensure_schema()
            with _connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ca.usuario, ca.accion, ca.fecha, ca.metadata
                        FROM control_agente ca
                        JOIN conversaciones_agente c ON c.id=ca.conversacion_id
                        WHERE c.telefono=%s ORDER BY ca.fecha DESC LIMIT 100
                        """,
                        (_normalizar_telefono(telefono),),
                    )
                    filas = cur.fetchall()
            nombres = ["usuario", "accion", "fecha", "metadata"]
            return jsonify({"status": "success", "control": [dict(zip(nombres, r)) for r in filas]})
        except Exception as exc:
            return jsonify({"status": "error", "mensaje": str(exc)}), 500

    @module.app.get("/control/media/<path:filename>")
    def control_media(filename):
        """Sirve comprobantes con URL firmada (sin header; usable en <img> del ERP)."""
        if not _MEDIA_SECRET:
            return jsonify({"status": "error", "mensaje": "Servicio media no configurado"}), 503
        if not filename or Path(filename).name != filename or ".." in filename:
            return jsonify({"status": "error", "mensaje": "Archivo no encontrado"}), 404
        try:
            expires_int = int(request.args.get("expires", "0"))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "mensaje": "Enlace inválido"}), 403
        token = str(request.args.get("token") or "")
        if expires_int < int(time.time()):
            return jsonify({"status": "error", "mensaje": "Enlace expirado"}), 410
        expected = _sign_media(filename, expires_int)
        if not token or not hmac.compare_digest(expected, token):
            return jsonify({"status": "error", "mensaje": "Enlace no autorizado"}), 403
        path = _MEDIA_DIR / filename
        if not path.is_file():
            return jsonify({"status": "error", "mensaje": "Archivo no encontrado"}), 404
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".pdf": "application/pdf",
        }.get(path.suffix.lower(), "application/octet-stream")
        return send_file(path, mimetype=mime, conditional=True, max_age=3600)

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
            return jsonify(
                {
                    "status": "success",
                    "telefono": _normalizar_telefono(telefono),
                    "modo_actual": "HUMANO",
                    "usuario": usuario,
                }
            )
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
            return jsonify(
                {
                    "status": "success",
                    "telefono": _normalizar_telefono(telefono),
                    "modo_actual": "AGENTE",
                }
            )
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


def construir_metadata_media(message: dict, tipo: str) -> dict:
    """Arma metadata con media_id y, si es posible, media_url pública firmada."""
    metadata: dict[str, Any] = {}
    if tipo == "image":
        img = message.get("image") or {}
        media_id = img.get("id")
        mime = img.get("mime_type") or "image/jpeg"
        caption = img.get("caption") or ""
        if media_id:
            metadata["media_id"] = media_id
            metadata["mime_type"] = mime
            if caption:
                metadata["caption"] = caption[:500]
            content, mime_dl = descargar_media_meta(media_id)
            if mime_dl:
                metadata["mime_type"] = mime_dl
            if content:
                filename = persistir_media_local(media_id, content, metadata.get("mime_type") or mime)
                if filename:
                    try:
                        metadata["media_url"] = build_signed_media_url(filename)
                        metadata["media_filename"] = filename
                    except Exception as exc:
                        print(f"[CONTROL HUMANO] No se pudo firmar media_url: {exc!r}", flush=True)
    elif tipo in {"document", "documento"}:
        doc = message.get("document") or {}
        media_id = doc.get("id")
        if media_id:
            metadata["media_id"] = media_id
            metadata["mime_type"] = doc.get("mime_type") or "application/octet-stream"
            metadata["filename"] = doc.get("filename") or ""
            content, mime_dl = descargar_media_meta(media_id)
            if mime_dl:
                metadata["mime_type"] = mime_dl
            if content:
                filename = persistir_media_local(media_id, content, metadata.get("mime_type") or "application/octet-stream")
                if filename:
                    try:
                        metadata["media_url"] = build_signed_media_url(filename)
                        metadata["media_filename"] = filename
                    except Exception as exc:
                        print(f"[CONTROL HUMANO] No se pudo firmar media_url doc: {exc!r}", flush=True)
    return metadata


def persist_incoming(module, data):
    try:
        value = data["entry"][0]["changes"][0]["value"]
        message = (value.get("messages") or [{}])[0]
        telefono, conv_meta = resolver_identidad_whatsapp(message, value)
        if not telefono:
            return True
        mid = message.get("id")
        tipo = message.get("type", "desconocido")
        if tipo == "text":
            texto = message.get("text", {}).get("body", "")[:4000]
        elif tipo == "image":
            caption = str((message.get("image") or {}).get("caption") or "").strip()
            if caption:
                texto = caption[:4000]
            else:
                texto = (
                    "[Imagen recibida. Clasificar: comprobante / carta-cobro / "
                    "cedula / otro. No presumir comprobante.]"
                )
        elif tipo in {"document", "documento"}:
            filename = (message.get("document") or {}).get("filename") or "documento"
            texto = f"[Documento recibido: {filename}]"
        else:
            texto = f"[Mensaje tipo {tipo}]"
        identificacion = None
        try:
            from agente_cobranzas import extraer_cedula as _extraer_cedula

            identificacion = _extraer_cedula(texto)
        except Exception:
            match = re.search(r"(?<![0-9.,$])\d{6,10}(?![0-9.,])", texto or "")
            if match and not re.fullmatch(r"3\d{9}", match.group(0)):
                identificacion = match.group(0)
        metadata = construir_metadata_media(message, tipo)
        record_message(
            telefono,
            "ENTRANTE",
            "DEUDOR",
            texto,
            mensaje_meta_id=mid,
            tipo_mensaje=tipo,
            metadata=metadata or None,
            identificacion=identificacion,
            conversacion_meta=conv_meta,
        )
        result = agent_can_respond(telefono)
        return result
    except Exception as exc:
        print(
            f"[CONTROL HUMANO] No se pudo interceptar mensaje entrante; se bloquea la IA por seguridad y se permite reintento: {exc!r}",
            flush=True,
        )
        return None


def install(module):
    if getattr(module, "_HUMAN_CONTROL_INSTALLED", False):
        return
    ensure_schema()
    module._HUMAN_ORIGINAL_SEND = module.enviar_mensaje_whatsapp
    module._HUMAN_ORIGINAL_PDF = getattr(module, "enviar_pdf_whatsapp", None)
    module._HUMAN_ORIGINAL_LOOKUP = getattr(module, "buscar_deuda_en_neon", None)

    def enviar_mensaje_wrapped(telefono, texto, message_id=None):
        result = module._HUMAN_ORIGINAL_SEND(telefono, texto, message_id)
        autor = getattr(_THREAD_STATE, "autor", None) or "AGENTE"
        metadata = {}
        if getattr(_THREAD_STATE, "usuario", None):
            metadata["usuario"] = _THREAD_STATE.usuario
        record_message(telefono, "SALIENTE", autor, texto, metadata=metadata)
        return result

    module.enviar_mensaje_whatsapp = enviar_mensaje_wrapped

    if module._HUMAN_ORIGINAL_PDF:

        def enviar_pdf_wrapped(telefono, url_pdf, message_id=None):
            result = module._HUMAN_ORIGINAL_PDF(telefono, url_pdf, message_id)
            autor = getattr(_THREAD_STATE, "autor", None) or "AGENTE"
            record_message(
                telefono,
                "SALIENTE",
                autor,
                "[Documento PDF enviado]",
                tipo_mensaje="document",
                metadata={"url_pdf": url_pdf, "media_url": url_pdf},
            )
            return result

        module.enviar_pdf_whatsapp = enviar_pdf_wrapped

    if module._HUMAN_ORIGINAL_LOOKUP:

        def lookup_wrapped(cedula, numero_cliente=None):
            result = module._HUMAN_ORIGINAL_LOOKUP(cedula, numero_cliente)
            if numero_cliente:
                record_message(
                    numero_cliente,
                    "INTERNO",
                    "SISTEMA",
                    str(result or ""),
                    tipo_mensaje="estado_deuda",
                    identificacion=cedula,
                    metadata={"tipo": "estado_deuda"},
                )
            return result

        module.buscar_deuda_en_neon = lookup_wrapped

    _register_routes(module)
    module._HUMAN_CONTROL_INSTALLED = True
    print("[CONTROL HUMANO] Conversaciones persistentes, liquidaciones y control humano habilitados", flush=True)
