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

## Persistencia e idempotencia

`memoria_chats` y `obligaciones_activas` ahora están respaldados por PostgreSQL/Neon mediante `bot_estado_persistente`, por lo que sobreviven reinicios de Render y funcionan entre workers.

Los `message_id` recibidos de WhatsApp se registran en `bot_eventos_procesados`. Un mismo evento no se procesa dos veces, incluso si Meta lo reintenta.

La migración está en `migrations/001_persistent_state.sql`. El agente también verifica/crea las tablas al arrancar, por lo que el deploy no depende de ejecutar manualmente el SQL antes de levantar el servicio.

## Operación

El webhook responde rápidamente y procesa el mensaje en segundo plano. El estado financiero sigue siendo responsabilidad exclusiva del ERP/liquidador central; esta migración solo persiste contexto conversacional y la obligación seleccionada.
