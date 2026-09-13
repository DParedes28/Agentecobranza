"""Runtime compatibility and production hardening hooks for the debt agent."""

import builtins
import hashlib
import hmac
import os
import re

try:
    import psycopg2
except Exception:
    psycopg2 = None


if psycopg2 is not None:
    _original_connect = psycopg2.connect

    class _CursorProxy:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, query, vars=None):
            if (
                isinstance(query, str)
                and "procesos_litisconsorcio" in query
                and "SELECT DISTINCT" in query
                and "ORDER BY" in query
                and "LOWER(COALESCE(p.estado" in query
            ):
                query = query.replace("SELECT DISTINCT", "SELECT", 1)
            return self._cursor.execute(query, vars)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

        def __enter__(self):
            self._cursor.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._cursor.__exit__(exc_type, exc_value, traceback)

    class _ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self, *args, **kwargs):
            return _CursorProxy(self._connection.cursor(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._connection.__exit__(exc_type, exc_value, traceback)

    def _patched_connect(*args, **kwargs):
        return _ConnectionProxy(_original_connect(*args, **kwargs))

    psycopg2.connect = _patched_connect


_state_hook_installed = False
_original_import = builtins.__import__


def _safe_text(data):
    try:
        value = data["entry"][0]["changes"][0]["value"]
        message = (value.get("messages") or [{}])[0]
        if message.get("type") == "text":
            return str(message.get("text", {}).get("body", ""))[:4000]
    except Exception:
        return ""
    return ""


def _normalizar_meta_secret(value):
    """Tolera espacios/comillas accidentales sin relajar la validacion criptografica."""
    secret = str(value or "").strip()
    if len(secret) >= 2 and secret[0] == secret[-1] and secret[0] in {"'", '"'}:
        secret = secret[1:-1].strip()
    return secret


def _instalar_validador_firma_meta(module):
    """Reemplaza solo la verificacion Meta por una version robusta y fail-closed."""
    secret = _normalizar_meta_secret(getattr(module, "META_APP_SECRET", None))
    module.META_APP_SECRET = secret

    def verificar_firma_meta_robusta(raw_body):
        if not secret:
            print("[META][ERROR] META_APP_SECRET no esta configurado", flush=True)
            return False

        firma = module.request.headers.get("X-Hub-Signature-256", "").strip()
        if not firma.lower().startswith("sha256="):
            print("[META][WARN] Webhook rechazado: falta X-Hub-Signature-256", flush=True)
            return False

        proporcionada = firma[7:].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", proporcionada):
            print(
                f"[META][WARN] Webhook rechazado: firma con formato invalido (longitud={len(proporcionada)})",
                flush=True,
            )
            return False

        esperada = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        valida = hmac.compare_digest(proporcionada, esperada)
        if not valida:
            print(
                "[META][WARN] Firma invalida. Verificar que META_APP_SECRET en Render "
                "sea exactamente el App Secret de Meta Developers.",
                flush=True,
            )
        return valida

    module.verificar_firma_meta = verificar_firma_meta_robusta
    print(
        "[META] Validacion X-Hub-Signature-256 endurecida | "
        f"APP_SECRET={'CONFIGURADO' if secret else 'FALTANTE'}",
        flush=True,
    )


def _append_internal_context(module, numero, context):
    if not context:
        return
    memory = module.memoria_chats.setdefault(numero, [])
    if not memory or memory[-1] != context:
        memory.append(context)


def _prepare_property_context(module, data, property_identity):
    """Detecta torre/apto antes de la IA sin revelar informacion financiera."""
    numero = None
    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        numero = message.get("from")
    except Exception:
        return
    if not numero:
        return

    text = _safe_text(data)
    ref = property_identity.extract_property_reference(text)
    if not ref:
        return

    state = module.obligaciones_activas.get(numero, {}) or {}
    verified_cedula = state.get("cedula") if state.get("identidad_confirmada") else None
    cedula_in_message = module.extraer_cedula(text) if hasattr(module, "extraer_cedula") else None
    cedula = cedula_in_message or verified_cedula

    try:
        matches = property_identity.find_property_matches(ref["torre"], ref["apto"], cedula=cedula)
        if len(matches) == 1:
            match = matches[0]
            state["inmueble_id"] = match["inmueble_id"]
            state["property_pending"] = True
            state["property_reference"] = match.get("torre_apto")
            state["conjunto_residencial"] = match.get("conjunto_residencial")
            state["property_cedula_pending"] = True
            if cedula:
                context = property_identity.liquidation_context(
                    module, numero, match["inmueble_id"], cedula,
                    metadata={"torre_apto": match.get("torre_apto"), "conjunto_residencial": match.get("conjunto_residencial")},
                )
                state["property_pending"] = False
                state["property_cedula_pending"] = False
                state["identidad_confirmada"] = "IDENTIDAD NO COINCIDE" not in context
                _append_internal_context(module, numero, context)
            else:
                _append_internal_context(module, numero, property_identity.property_context(matches))
        else:
            state.pop("property_pending", None)
            state.pop("property_cedula_pending", None)
            _append_internal_context(module, numero, property_identity.property_context(matches))
        module.obligaciones_activas[numero] = state
    except Exception as exc:
        print(f"⚠️ No se pudo resolver el inmueble solicitado: {exc!r}", flush=True)
        _append_internal_context(
            module, numero,
            "[SISTEMA INTERNO - CONSULTA DE INMUEBLE NO DISPONIBLE]\n"
            "No fue posible verificar la unidad en este momento. No revelar valores financieros y escalar si persiste.",
        )


def _conversation_lock(numero, callback):
    """Serializa mensajes del mismo WhatsApp entre workers Gunicorn."""
    if not psycopg2 or not _original_connect or not os.environ.get("DATABASE_URL"):
        return callback()
    conn = None
    cur = None
    try:
        conn = _original_connect(os.environ["DATABASE_URL"])
        conn.autocommit = True
        cur = conn.cursor()
        key = str(numero or "")
        cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        return callback()
    finally:
        try:
            if cur is not None:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(numero or ""),))
                cur.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _persist_agent_state(module):
    global _state_hook_installed
    if getattr(module, "_PERSISTENT_STATE_INSTALLED", False):
        return

    try:
        from persistent_state import PersistentState, registrar_evento, eliminar_evento
        import control_humano
        import property_identity

        _instalar_validador_firma_meta(module)

        module.memoria_chats = PersistentState("memoria_chat")
        module.obligaciones_activas = PersistentState("obligacion_activa")
        control_humano.install(module)

        original_lookup = module.buscar_deuda_en_neon

        def buscar_deuda_contextual(cedula, numero_cliente=None):
            numero = numero_cliente
            try:
                state = module.obligaciones_activas.get(numero, {}) if numero else {}
                if state and state.get("property_pending") and state.get("inmueble_id"):
                    inmueble_id = int(state["inmueble_id"])
                    if not property_identity.verify_identity_for_property(cedula, inmueble_id):
                        return (
                            "[SISTEMA INTERNO - IDENTIDAD NO COINCIDE]\n"
                            "La cedula suministrada no esta asociada a la unidad solicitada. "
                            "No revelar ningun valor financiero. Solicitar revisar la cedula o la unidad."
                        )
                    context = property_identity.liquidation_context(
                        module, numero, inmueble_id, cedula,
                        metadata={"torre_apto": state.get("property_reference"), "conjunto_residencial": state.get("conjunto_residencial")},
                    )
                    state["property_pending"] = False
                    state["property_cedula_pending"] = False
                    state["identidad_confirmada"] = "IDENTIDAD NO COINCIDE" not in context
                    module.obligaciones_activas[numero] = state
                    return context

                result = original_lookup(cedula, numero_cliente)
                if numero_cliente:
                    try:
                        state = module.obligaciones_activas.get(numero_cliente, {}) or {}
                        clean = re.sub(r"\D", "", str(cedula or ""))
                        if clean and re.fullmatch(r"\d{6,12}", clean):
                            state["identidad_confirmada"] = "[SISTEMA INTERNO - ESTADO DE CUENTA OFICIAL]" in str(result)
                            module.obligaciones_activas[numero_cliente] = state
                    except Exception:
                        pass
                return result
            except Exception as exc:
                print(f"⚠️ No se pudo procesar consulta contextual: {exc!r}", flush=True)
                return "SISTEMA: No fue posible validar la unidad solicitada en este momento."

        module.buscar_deuda_en_neon = buscar_deuda_contextual
        original_processor = module.procesar_y_responder

        def procesar_y_responder_persistente(data):
            message_id = None
            numero = None
            try:
                value = data["entry"][0]["changes"][0]["value"]
                message = (value.get("messages") or [{}])[0]
                message_id = message.get("id")
                numero = message.get("from")
                if not message_id:
                    return False
                if not registrar_evento(message_id, numero):
                    print(f"ℹ️ Mensaje duplicado ignorado: {message_id}", flush=True)
                    return True

                def _run():
                    control_result = control_humano.persist_incoming(module, data)
                    if control_result is False:
                        print(f"👤 Conversacion {numero} bajo control humano; IA no responde", flush=True)
                        return True
                    if control_result is None:
                        print(f"⚠️ No se pudo validar el control humano para {numero}; se libera el evento para reintento", flush=True)
                        eliminar_evento(message_id)
                        return False
                    _prepare_property_context(module, data, property_identity)
                    result = original_processor(data)
                    if result is False:
                        eliminar_evento(message_id)
                        return False
                    return True

                return _conversation_lock(numero, _run)
            except Exception as exc:
                print(f"❌ No se pudo procesar mensaje de forma segura: {repr(exc)}", flush=True)
                if message_id:
                    try:
                        eliminar_evento(message_id)
                    except Exception as cleanup_exc:
                        print(f"❌ No se pudo liberar el evento para reintento: {repr(cleanup_exc)}", flush=True)
                return False

        module.procesar_y_responder = procesar_y_responder_persistente
        module._PERSISTENT_STATE_INSTALLED = True
        print("✅ Estado persistente PostgreSQL, identidad por inmueble y serializacion habilitados", flush=True)
    except Exception as exc:
        print(f"❌ No se pudo habilitar estado persistente: {repr(exc)}", flush=True)
        raise


def _import_with_state_hook(name, globals=None, locals=None, fromlist=(), level=0):
    module = _original_import(name, globals, locals, fromlist, level)
    target = name.split(".")[0]
    if target == "agente_cobranzas" and hasattr(module, "procesar_y_responder"):
        _persist_agent_state(module)
    return module


if not _state_hook_installed:
    builtins.__import__ = _import_with_state_hook
    _state_hook_installed = True
