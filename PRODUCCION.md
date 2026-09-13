# Checklist de producción — Agente de cobranza

## Render

Configurar como variables de entorno:

- `DATABASE_URL`
- `ANTHROPIC_API_KEY`
- `TOKEN_META`
- `ID_NUMERO_TELEFONO`
- `TOKEN_VERIFICACION`
- `LIQUIDADOR_API_URL=https://gestionjudicial.onrender.com`
- `LIQUIDADOR_API_KEY` — misma clave configurada en el ERP
- `META_APP_SECRET` — App Secret de Meta para validar `X-Hub-Signature-256`

## Seguridad

El webhook POST valida la firma de Meta cuando `META_APP_SECRET` está configurada. La validación usa HMAC-SHA256 sobre el cuerpo HTTP sin modificar.

El agente nunca calcula la deuda: obtiene los valores exclusivamente desde `/api/bot/liquidar` del ERP.

## Persistencia

`memoria_chats` y `obligaciones_activas` siguen siendo memoria local del proceso. La auditoría y las gestiones CRM se guardan en PostgreSQL. Para múltiples réplicas o recuperación completa de conversaciones, la siguiente evolución debe mover el estado conversacional a PostgreSQL/Redis.

## Operación

El webhook responde rápidamente y procesa el mensaje en segundo plano. Meta puede reintentar eventos; el siguiente endurecimiento recomendado es una tabla de idempotencia por `message_id` para garantizar procesamiento exactamente una vez.
