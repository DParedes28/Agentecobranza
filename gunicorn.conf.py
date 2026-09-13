# Ensure the PostgreSQL compatibility patch is loaded before Flask imports/uses psycopg2.
# Render/Gunicorn loads this config from the project root when using Gunicorn.
import sitecustomize  # noqa: F401

# Register the human-supervision routes explicitly in every Gunicorn worker.
# This is intentionally additive: the existing webhook/app flow remains unchanged.
try:
    import agente_cobranzas
    import control_humano

    control_humano.install(agente_cobranzas)
    print("[SUPERVISION] control_humano registrado en Gunicorn", flush=True)
except Exception as exc:
    # Do not prevent the existing WhatsApp service from starting if the optional
    # supervision layer cannot initialize. The error remains visible in Render logs.
    print(f"[SUPERVISION][ALERTA] No se pudo registrar control_humano: {exc!r}", flush=True)
