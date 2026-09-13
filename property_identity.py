"""Resolucion flexible de inmuebles y verificacion de identidad del agente."""

import os
import re
from typing import Any, Dict, List, Optional

import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no esta configurada")
    return psycopg2.connect(DATABASE_URL)


def extract_property_reference(text: str) -> Optional[Dict[str, str]]:
    """Reconoce referencias naturales como 'torre 25 apto 203' o '25 203'."""
    text = " ".join(str(text or "").lower().split())
    if not text:
        return None

    patterns = [
        r"\btorre\s*([a-z0-9]{1,6})\s*(?:apt(?:o|\.)?|apartamento|unidad)\s*([a-z0-9-]{1,8})\b",
        r"\bt\s*([a-z0-9]{1,6})\s*a(?:pt(?:o|\.)?)?\s*([a-z0-9-]{1,8})\b",
        r"\b([0-9]{1,4})\s*(?:apt(?:o|\.)?|apartamento)\s*([a-z0-9-]{1,8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return {"torre": match.group(1), "apto": match.group(2)}

    intent = re.search(r"\b(deuda|saldo|estado\s+de\s+cuenta|cuanto\s+debo|cu[aá]nto\s+debo|obligaci[oó]n|cuenta)\b", text, re.I)
    if not intent:
        return None
    pair = re.search(r"(?<!\d)([0-9]{1,4})\s*(?:-|/|,|\s)\s*([0-9]{1,5})(?!\d)", text)
    if pair:
        return {"torre": pair.group(1), "apto": pair.group(2)}
    return None


def _normalized_ref(torre: str, apto: str) -> str:
    torre = re.sub(r"[^a-z0-9]+", "", str(torre or "").strip().lower())
    apto = re.sub(r"[^a-z0-9]+", "", str(apto or "").strip().lower())
    return f"{torre}-{apto}"


def find_property_matches(torre: str, apto: str, conjunto: Optional[str] = None,
                          cedula: Optional[str] = None) -> List[Dict[str, Any]]:
    """Busca inmuebles sin exigir un formato exacto de torre_apto."""
    ref = _normalized_ref(torre, apto)
    ref_variants = [ref]
    if torre.isdigit() and apto.isdigit():
        ref_variants.append(f"{int(torre)}-{int(apto)}")

    sql = """
        SELECT DISTINCT i.id, i.conjunto_residencial, i.torre_apto,
                        c.identificacion, c.nombre
        FROM inmuebles_ph i
        JOIN contactos c ON c.id = i.contacto_id
        WHERE regexp_replace(lower(coalesce(i.torre_apto, '')), '[^a-z0-9]+', '-', 'g') = ANY(%s)
    """
    params: List[Any] = [list(dict.fromkeys(ref_variants))]
    if conjunto:
        sql += " AND lower(coalesce(i.conjunto_residencial, '')) LIKE lower(%s) "
        params.append(f"%{str(conjunto).strip()}%")
    if cedula:
        clean = _digits(cedula)
        sql += """
          AND (
                regexp_replace(coalesce(c.identificacion::text, ''), '[^0-9]', '', 'g') = %s
                OR EXISTS (
                    SELECT 1
                    FROM procesos p
                    JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
                    WHERE p.inmueble_id = i.id
                      AND regexp_replace(coalesce(pl.identificacion_demandado::text, ''), '[^0-9]', '', 'g') = %s
                )
          )
        """
        params.extend([clean, clean])
    sql += " ORDER BY i.id "

    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [
        {
            "inmueble_id": int(row[0]),
            "conjunto_residencial": row[1],
            "torre_apto": row[2],
            "identificacion": row[3],
            "nombre": row[4],
        }
        for row in rows
    ]


def verify_identity_for_property(cedula: str, inmueble_id: int) -> bool:
    clean = _digits(cedula)
    if not (6 <= len(clean) <= 12):
        return False
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM inmuebles_ph i
                    JOIN contactos c ON c.id = i.contacto_id
                    WHERE i.id = %s
                      AND regexp_replace(coalesce(c.identificacion::text, ''), '[^0-9]', '', 'g') = %s
                ) OR EXISTS (
                    SELECT 1
                    FROM procesos p
                    JOIN procesos_litisconsorcio pl ON pl.radicado_interno = p.radicado_interno
                    WHERE p.inmueble_id = %s
                      AND regexp_replace(coalesce(pl.identificacion_demandado::text, ''), '[^0-9]', '', 'g') = %s
                )
                """,
                (int(inmueble_id), clean, int(inmueble_id), clean),
            )
            return bool(cur.fetchone()[0])


def property_context(matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return "[SISTEMA INTERNO - INMUEBLE NO ENCONTRADO]\nNo se encontro una unidad con la referencia indicada."
    if len(matches) > 1:
        unidades = ", ".join(
            f"{m.get('torre_apto') or 'unidad'} ({m.get('conjunto_residencial') or 'conjunto no informado'})"
            for m in matches[:5]
        )
        return (
            "[SISTEMA INTERNO - INMUEBLE AMBIGUO]\n"
            f"Hay {len(matches)} coincidencias para la referencia indicada: {unidades}. "
            "No revelar saldos. Solicitar el nombre del conjunto residencial."
        )
    m = matches[0]
    return (
        "[SISTEMA INTERNO - INMUEBLE IDENTIFICADO]\n"
        f"Inmueble encontrado: {m.get('conjunto_residencial') or 'Conjunto no informado'} / {m.get('torre_apto') or 'Unidad no informada'}.\n"
        "La identidad del consultante aun no esta confirmada. No revelar valores financieros. "
        "Solicitar unicamente la cedula para validar la titularidad o relacion con esta unidad."
    )


def liquidation_context(module, numero_cliente: str, inmueble_id: int, cedula: str,
                        metadata: Optional[Dict[str, Any]] = None) -> str:
    """Liquida una unidad concreta despues de verificar que la cedula tiene relacion con ella."""
    if not verify_identity_for_property(cedula, inmueble_id):
        return (
            "[SISTEMA INTERNO - IDENTIDAD NO COINCIDE]\n"
            "La cedula suministrada no esta asociada a la unidad solicitada. "
            "No revelar ningun valor financiero y solicitar que revise el numero de cedula o la unidad."
        )

    datos = module.solicitar_liquidacion(int(inmueble_id), module.fecha_colombia())
    capital = datos.get("capital", 0.0)
    intereses = datos.get("intereses", 0.0)
    honorarios = datos.get("honorarios", 0.0)
    gastos = datos.get("gastos", 0.0)
    gran_total = datos.get("gran_total", datos.get("total_exigible", 0.0))
    honorarios_pct = datos.get("honorarios_pct", 23.8)

    estado = {
        "cedula": _digits(cedula),
        "inmueble_id": int(inmueble_id),
        "identidad_confirmada": True,
        "tipo_consulta": "INMUEBLE",
    }
    if metadata:
        estado.update(metadata)
    module.obligaciones_activas[numero_cliente] = estado

    return f"""[SISTEMA INTERNO - ESTADO DE CUENTA OFICIAL]\n\nConsulta validada para el inmueble ID {int(inmueble_id)}.\n\nSALDO DE CAPITAL: ${float(capital):,.0f}\nINTERESES DE MORA: ${float(intereses):,.0f}\nHONORARIOS DE ABOGADO ({float(honorarios_pct):g}%): ${float(honorarios):,.0f}\nGASTOS PROCESALES: ${float(gastos):,.0f}\n\nGRAN TOTAL LIQUIDADO A LA FECHA: ${float(gran_total):,.0f}\n\nLa informacion financiera proviene exclusivamente del motor de liquidacion central."""
