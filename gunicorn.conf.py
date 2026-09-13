# Ensure the PostgreSQL compatibility patch is loaded before Flask imports/uses psycopg2.
# Render/Gunicorn loads this config from the project root when using Gunicorn.
import sitecustomize  # noqa: F401
