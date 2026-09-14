"""API M2M para integración con el agente inteligente de cobranza (WhatsApp)."""
import os
import io
import time
import secrets
import hashlib
import hmac
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse

import liquidador
import exportaciones

router = APIRouter()

BOT_API_KEY = (os.getenv("LIQUIDADOR_API_KEY") or "").strip().strip('"').strip("'")
_TOKEN_TTL_SECONDS = int(os.getenv("BOT_PDF_URL_TTL", "900"))
_PDF_SECRET = (
    os.getenv("BOT_PDF_PUBLIC_SECRET")
    or BOT_API_KEY
    or ""
).strip().strip('"').strip("'")


def _public_base_url() -> str:
    return (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")


def _require_api_key(request: Request) -> None:
    expected = BOT_API_KEY
    supplied = request.headers.get("X-API-Key")
    if not expected:
        raise HTTPException(status_code=503, detail="LIQUIDADOR_API_KEY no está configurada")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="No autorizado")


def _sign(filename: str, expires: int) -> str:
    payload = f"{filename}|{expires}".encode("utf-8")
    return hmac.new(_PDF_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def build_signed_pdf_url(filename: str, base_url: str) -> str:
    if not _PDF_SECRET:
        raise RuntimeError("BOT_PDF_PUBLIC_SECRET/LIQUIDADOR_API_KEY no está configurada")
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _sign(filename, expires)
    return f"{base_url.rstrip('/')}/api/bot/pdf/{filename}?expires={expires}&token={token}"


def _parse_payload(payload: Any) -> tuple[int, date]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON")

    try:
        inmueble_id = int(payload.get("inmueble_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="inmueble_id debe ser un entero")

    if inmueble_id <= 0:
        raise HTTPException(status_code=422, detail="inmueble_id debe ser mayor que cero")

    fecha_texto = str(payload.get("fecha_corte") or "")
    try:
        fecha_corte = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_corte debe tener formato YYYY-MM-DD")

    if fecha_corte > date.today():
        raise HTTPException(status_code=422, detail="fecha_corte no puede ser futura")

    return inmueble_id, fecha_corte


def _obtener_datos_liquidacion(inmueble_id: int, fecha_corte: date) -> tuple[list[dict], dict, tuple]:
    resultados, resumen, inm_info = liquidador.motor_calculo_judicial(
        inmueble_id,
        "usura",
        2.5,
        23.8,
        0.0,
        fecha_corte,
    )
    if not inm_info:
        raise HTTPException(status_code=404, detail="No existe información del inmueble")
    if not resultados:
        raise HTTPException(status_code=404, detail="No hay movimientos para liquidar")
    return resultados, resumen, inm_info


def _resumen_tasas(resultados: list[dict]) -> tuple[list[dict], str]:
    tasas = []
    vistos = set()
    for fila in resultados:
        periodo = fila.get("periodo") or fila.get("mes")
        desde = fila.get("desde")
        tasa_ea = fila.get("tasa_ea")
        tasa_mes = fila.get("tasa_mes")
        clave = (str(periodo), str(desde), str(tasa_ea), str(tasa_mes))
        if clave in vistos:
            continue
        vistos.add(clave)
        tasas.append({
            "periodo": periodo or desde or "",
            "fecha_desde": desde or "",
            "tasa_ea": tasa_ea or "",
            "tasa_mensual": tasa_mes or "",
            "fuente": "SFC/Neon validada" if not fila.get("tasa_personalizada") else "Tasa personalizada E.A.",
        })
    fuente = "SFC/Neon validada"
    if any(f.get("tasa_personalizada") for f in resultados):
        fuente = "Tasa personalizada E.A."
    return tasas, fuente


def _generar_pdf_unificado(inmueble_id: int, fecha_corte: date, resultados: list[dict], resumen: dict, inm_info: tuple) -> str:
    base = _public_base_url()
    if not base:
        raise HTTPException(status_code=500, detail="PUBLIC_BASE_URL/RENDER_EXTERNAL_URL no está configurada")
    pdf_path = exportaciones.generar_pdf_liquidacion(inmueble_id, fecha_corte, resultados, resumen, inm_info)
    filename = os.path.basename(pdf_path)
    return build_signed_pdf_url(filename, base)


@router.post("/api/bot/liquidar")
async def liquidar_para_bot(request: Request):
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON válido")

    inmueble_id, fecha_corte = _parse_payload(payload)

    try:
        resultados, resumen, inm_info = _obtener_datos_liquidacion(inmueble_id, fecha_corte)
        url_pdf = _generar_pdf_unificado(inmueble_id, fecha_corte, resultados, resumen, inm_info)
        tasas_aplicadas, fuente_tasas = _resumen_tasas(resultados)

        print(
            f"[BOT LIQUIDADOR] inmueble={inmueble_id} corte={fecha_corte} "
            f"capital={float(resumen.get('capital', 0)):.2f} "
            f"intereses={float(resumen.get('intereses', 0)):.2f} "
            f"total={float(resumen.get('gran_total', 0)):.2f} "
            f"fuente_tasas={fuente_tasas}",
            flush=True,
        )

        return JSONResponse({
            "status": "success",
            "mensaje": "Liquidación generada correctamente",
            "datos": {
                "inmueble_id": inmueble_id,
                "deudor": inm_info[2],
                "identificacion": inm_info[3],
                "capital": float(resumen["capital"]),
                "intereses": float(resumen["intereses"]),
                "honorarios_pct": float(resumen["honorarios_pct"]),
                "honorarios": float(resumen["honorarios"]),
                "gastos": float(resumen["gastos"]),
                "gran_total": float(resumen["gran_total"]),
                "total_exigible": float(resumen["gran_total"]),
                "tasas_aplicadas": tasas_aplicadas,
                "fuente_tasas": fuente_tasas,
                "detalle_mensual": resultados,
                "url_pdf": url_pdf,
            },
        })
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[BOT LIQUIDADOR] Error generando liquidación: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="No fue posible generar la liquidación")


@router.get("/api/bot/pdf/{filename}", include_in_schema=False)
def servir_pdf_bot(filename: str, expires: int, token: str):
    if not _PDF_SECRET:
        raise HTTPException(status_code=503, detail="Servicio PDF no configurado")
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Enlace inválido")
    if expires_int < int(time.time()):
        raise HTTPException(status_code=410, detail="Enlace expirado")
    expected = _sign(filename, expires_int)
    if not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Enlace no autorizado")

    path = Path("static") / "pdfs" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
        headers={"Cache-Control": "private, max-age=900"},
    )


@router.post("/api/bot/acuerdo")
async def registrar_acuerdo_bot(request: Request):
    """Permite al agente conversacional registrar compromisos y acuerdos de pago en el ERP."""
    _require_api_key(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="El cuerpo debe ser JSON válido")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Payload inválido")

    identificacion = str(payload.get("identificacion") or payload.get("cedula") or "").strip()
    if not identificacion:
        raise HTTPException(status_code=422, detail="identificacion es requerida")

    fecha_texto = str(payload.get("fecha_compromiso") or payload.get("fecha_pago") or "")
    try:
        fecha_compromiso = datetime.strptime(fecha_texto, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="fecha_compromiso debe tener formato YYYY-MM-DD")

    try:
        valor = float(payload.get("valor_acordado") or payload.get("valor") or 0.0)
    except (ValueError, TypeError):
        valor = 0.0

    inmueble_id = payload.get("inmueble_id")
    try:
        inmueble_id = int(inmueble_id) if inmueble_id else None
    except (ValueError, TypeError):
        inmueble_id = None

    telefono = str(payload.get("telefono") or "").strip()
    nombre_deudor = str(payload.get("nombre_deudor") or payload.get("nombre") or "").strip()
    observaciones = str(payload.get("observaciones") or payload.get("resumen") or "Acuerdo de pago pactado vía WhatsApp con Agente IA").strip()
    numero_cuotas = int(payload.get("numero_cuotas") or 1)
    cuota_actual = int(payload.get("cuota_actual") or 1)

    import db
    from psycopg2.extras import RealDictCursor

    conn = db.get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. Si no viene el nombre, buscarlo en contactos o inmueble
                if not nombre_deudor:
                    cur.execute("SELECT nombre, telefono FROM contactos WHERE identificacion=%s LIMIT 1", (identificacion,))
                    c_row = cur.fetchone()
                    if c_row:
                        nombre_deudor = c_row.get("nombre") or ""
                        if not telefono:
                            telefono = c_row.get("telefono") or ""

                # 2. Insertar en acuerdos_pago
                cur.execute("""
                    INSERT INTO acuerdos_pago (
                        inmueble_id, identificacion_deudor, nombre_deudor, telefono,
                        valor_acordado, numero_cuotas, cuota_actual, fecha_compromiso,
                        estado, origen, observaciones
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDIENTE', 'ROBOT_IA', %s)
                    RETURNING id
                """, (
                    inmueble_id, identificacion, nombre_deudor, telefono,
                    valor, numero_cuotas, cuota_actual, fecha_compromiso, observaciones
                ))
                row_ac = cur.fetchone()
                acuerdo_id = int(row_ac["id"]) if row_ac and str(row_ac.get("id","0")).isdigit() else 1

                # 3. Asentar anotación en gestiones_crm
                cur.execute("""
                    INSERT INTO gestiones_crm (
                        inmueble_id, identificacion_deudor, tipo_contacto,
                        resumen, promesa_pago_fecha, usuario, estado
                    ) VALUES (%s, %s, 'WhatsApp IA - Acuerdo', %s, %s, 'Bot Claude', 'ACTIVO')
                """, (
                    inmueble_id, identificacion,
                    f"🤝 [ACUERDO DE PAGO #{acuerdo_id}] Cuota {cuota_actual}/{numero_cuotas} por ${valor:,.0f} para el {fecha_compromiso}. {observaciones}",
                    fecha_compromiso
                ))

                # 4. Insertar en vencimientos para agenda judicial unificada
                radicado = "ACUERDO-PAGO"
                if inmueble_id:
                    cur.execute("SELECT radicado_interno FROM procesos WHERE inmueble_id=%s LIMIT 1", (inmueble_id,))
                    p_row = cur.fetchone()
                    if p_row and p_row.get("radicado_interno"):
                        radicado = p_row["radicado_interno"]

                cur.execute("""
                    INSERT INTO vencimientos (
                        radicado_interno, titulo, fecha_vencimiento, observaciones,
                        completado, tipo, valor, inmueble_id
                    ) VALUES (%s, %s, %s, %s, FALSE, 'ACUERDO_PAGO', %s, %s)
                """, (
                    radicado,
                    f"Cobro Cuota #{cuota_actual} ({nombre_deudor or identificacion}) - ${valor:,.0f}",
                    fecha_compromiso,
                    f"Acuerdo #{acuerdo_id}. Tel: {telefono}. Obs: {observaciones}",
                    valor,
                    inmueble_id
                ))

        print(f"[BOT ACUERDO] Registrado acuerdo #{acuerdo_id} para {identificacion} por ${valor:,.0f} al {fecha_compromiso}", flush=True)

        return JSONResponse({
            "status": "success",
            "mensaje": "Acuerdo de pago registrado y sincronizado en ERP",
            "acuerdo_id": acuerdo_id,
            "datos": {
                "identificacion": identificacion,
                "nombre_deudor": nombre_deudor,
                "fecha_compromiso": str(fecha_compromiso),
                "valor_acordado": valor,
                "estado": "PENDIENTE",
            }
        })
    except Exception as exc:
        print(f"[BOT ACUERDO] Error registrando acuerdo: {exc!r}", flush=True)
        raise HTTPException(status_code=500, detail="Error interno al registrar acuerdo en ERP")
    finally:
        conn.release()


@router.get("/api/bot/recordatorios/pendientes")
def consultar_recordatorios_pendientes(request: Request, dias_anticipacion: int = 1):
    """Consulta acuerdos de pago próximos para disparar recordatorios programados."""
    _require_api_key(request)
    import db
    from psycopg2.extras import RealDictCursor
    conn = db.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            hoy = date.today()
            limite = hoy + timedelta(days=dias_anticipacion)
            cur.execute("""
                SELECT a.id, a.inmueble_id, a.identificacion_deudor, a.nombre_deudor, a.telefono,
                       a.valor_acordado, a.fecha_compromiso, a.cuota_actual, a.numero_cuotas,
                       a.recordatorio_previo_enviado, a.recordatorio_dia_enviado, a.recordatorio_mora_enviado,
                       i.conjunto_residencial, i.torre_apto
                FROM acuerdos_pago a
                LEFT JOIN inmuebles_ph i ON a.inmueble_id = i.id
                WHERE a.estado = 'PENDIENTE'
                  AND a.fecha_compromiso BETWEEN %s AND %s
                ORDER BY a.fecha_compromiso ASC
            """, (hoy, limite))
            acuerdos = [dict(r) for r in cur.fetchall()]
            for a in acuerdos:
                if a.get("valor_acordado") is not None:
                    a["valor_acordado"] = float(a["valor_acordado"])
                if a.get("fecha_compromiso"):
                    a["fecha_compromiso"] = str(a["fecha_compromiso"])
        return JSONResponse({"status": "success", "total": len(acuerdos), "acuerdos": acuerdos})
    finally:
        conn.release()
