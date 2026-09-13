# Checklist de producción — Agente de cobranza

## Render

Configurar como variables de entorno:

- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL` (opcional; por defecto `claude-haiku-4-5-20251001`)
- `TOKEN_META`
- `ID_NUMERO_TELEFONO`
- `TOKEN_VERIFICACION`
- `META_APP_SECRET` — obligatoria para aceptar webhooks de Meta
- `LIQUIDADOR_API_URL=https://gestionjudicial.onrender.com`
- `LIQUIDADOR_API_KEY` — misma clave configurada en el ERP
- `AGENT_SUPERVISION_KEY` — clave independiente y compartida con el proxy de supervisión del ERP

No reutilizar `LIQUIDADOR_API_KEY` como clave de supervisión.

## Seguridad

El webhook POST valida siempre `X-Hub-Signature-256` mediante HMAC-SHA256 sobre el cuerpo HTTP sin modificar.

El agente nunca calcula la deuda por su cuenta: obtiene los valores exclusivamente desde `/api/bot/liquidar` del ERP.

La identidad se valida antes de revelar valores financieros. Una consulta por inmueble puede localizar primero la unidad, pero requiere confirmar la cédula asociada antes de mostrar saldos.

## Experiencia de consulta

El usuario puede escribir referencias naturales como:

- `¿Cuál es la deuda del 25 203?`
- `Quiero saber la deuda de la torre 25 apto 203`
- `estado de cuenta torre 25 apartamento 203`
- `saldo 25-203`

El agente interpreta torre/apartamento, busca la unidad en Neon y, si es única, solicita únicamente la cédula para verificar la titularidad o relación con esa unidad.

Si la identidad ya fue validada en la conversación, no vuelve a pedir la cédula innecesariamente y puede consultar directamente la unidad solicitada.

## Persistencia, idempotencia y concurrencia

`memoria_chats` y `obligaciones_activas` están respaldados por PostgreSQL/Neon mediante `bot_estado_persistente`, por lo que sobreviven reinicios de Render y funcionan entre workers.

Los `message_id` de WhatsApp se registran en `bot_eventos_procesados`. Un evento procesado correctamente no se repite; si el procesamiento falla de verdad, el evento se libera para permitir el reintento de Meta.

Los mensajes de un mismo número se serializan mediante advisory locks de PostgreSQL para evitar respuestas cruzadas cuando llegan varios mensajes seguidos desde WhatsApp.

La migración está en `migrations/001_persistent_state.sql`. El agente también verifica/crea las tablas al arrancar.

## Supervisión humana

La capa de supervisión mantiene conversaciones, mensajes entrantes/salientes y acciones de toma/devolución de control en tablas separadas.

Cuando Neon no puede determinar con seguridad el modo de la conversación, la IA se bloquea y el evento se libera para reintento; no se continúa de forma insegura.

## Operación

`/health` devuelve `200` solamente cuando están presentes las variables críticas y `503` en configuración incompleta.

El webhook responde rápidamente y procesa el mensaje en segundo plano. Los valores financieros siguen siendo responsabilidad exclusiva del ERP/liquidador central.

## CI

El repositorio incluye pruebas unitarias para el reconocimiento de referencias de inmueble en `tests/` y un workflow de GitHub Actions que las ejecuta en cada push a `main` y en pull requests.
