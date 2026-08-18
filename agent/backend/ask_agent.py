"""
Fase 4.2 - Lambda detras de una Function URL: agente de lenguaje natural
sobre los datos del pipeline, con BYOK (Bring Your Own Key).

POST { "question": "...", "openai_api_key": "sk-..." }
-> { "answer": "..." }

La key de OpenAI es del visitante: viaja en el request, se usa una sola vez
para esa pregunta puntual, y nunca se guarda ni se loguea - por eso esto no
usa API Gateway (techo duro de ~29s, no configurable) sino una Lambda
Function URL, cuyo limite real es el timeout de la propia Lambda (hasta
900s) - un loop de agente con varias vueltas de tool-calling puede pasarse
holgadamente de 29s.

Las credenciales de Databricks (para run_sql) y la URL de la API de
cotizacion (para get_fare_quote) siguen siendo del dueño del proyecto - viven
solo en variables de entorno de este Lambda, nunca se exponen al navegador.
"""

import os
import json
import logging

import boto3
import requests
from openai import OpenAI
from databricks import sql as databricks_sql

# basicConfig() no hace nada aca: el runtime de Lambda ya deja un handler
# puesto en el root logger antes de que corra este modulo, asi que
# basicConfig (que solo actua si NO hay handlers) queda de no-op - sin
# setLevel explicito, los logger.info() se pierden en silencio.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MODEL = "gpt-4o"  # cambiar al modelo mas capaz disponible

SYSTEM_PROMPT = """You are a data assistant for the NYC Yellow Taxi pipeline.
You answer in English, with concrete numbers, citing which table each figure came from.

Available tables (Unity Catalog, catalog nyc_taxi_analytics, schema fare_prediction):

- yellow_taxi_features: one record per processed trip. Key columns:
  PULocationID, DOLocationID (TLC zones 1-263), tpep_pickup_datetime,
  trip_distance, fare_amount, tip_amount, total_amount, passenger_count,
  pickup_hour, pickup_day_of_week, season, pickup_manhattan, dropoff_manhattan,
  is_airport_trip, processed_year, processing_month (partitions).
- taxi_zone_lookup: LocationID, Borough, Zone — to translate IDs to names.
- zone_pair_stats: median distance/tolls/fare by (PU, DO) pair.
- demand_forecast: PULocationID, ts, forecast_trips — demand forecast by
  zone/hour for the next 7 days (Phase 4.1).

Rules:
- To quote a future trip, ALWAYS use the get_fare_quote tool (queries the
  ML model) — never calculate the fare from SQL.
- For questions about historical data or future demand, use run_sql
  (SELECT only).
- If the user names a place ("Midtown", "JFK"), first resolve the
  LocationID with taxi_zone_lookup.
- Limit SQL results (LIMIT) — never pull entire tables."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Runs a SELECT (read-only) query against Unity Catalog and returns the rows as JSON. Use for historical data, zone lookups, and future demand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "SQL query (SELECT only, with LIMIT)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fare_quote",
            "description": "Quotes the estimated fare for a future trip using the served ML model. Always use this when asked how much a trip would cost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pickup_zone": {"type": "integer", "description": "Pickup LocationID (1-263)"},
                    "dropoff_zone": {"type": "integer", "description": "Dropoff LocationID (1-263)"},
                    "pickup_datetime": {"type": "string", "description": "Trip date/time, format YYYY-MM-DD HH:MM:SS"},
                    "passenger_count": {"type": "integer", "description": "Number of passengers (1-6), default 1"},
                },
                "required": ["pickup_zone", "dropoff_zone", "pickup_datetime"],
            },
        },
    },
]


def get_databricks_token(secret_arn):
    """Retrieve Databricks token from AWS Secrets Manager - mismo patron que processing_trigger.py"""
    try:
        secrets_client = boto3.client("secretsmanager")
        response = secrets_client.get_secret_value(SecretId=secret_arn)
        secret = json.loads(response["SecretString"])
        return secret["token"]
    except Exception as e:
        logger.error(f"Failed to retrieve Databricks token: {e}")
        raise Exception(f"Failed to retrieve Databricks token: {e}")


def run_sql(query: str) -> str:
    """Ejecuta la consulta en el SQL warehouse. Guarda: solo SELECT."""
    if not query.strip().lower().startswith("select"):
        return json.dumps({"error": "Solo se permiten consultas SELECT"})

    token = get_databricks_token(os.environ["DATABRICKS_SECRET_ARN"])

    with databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"].replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}",
        access_token=token,
        # Sin esto, el catalogo/schema por defecto de la sesion es
        # workspace.default - cuando el modelo escribe "FROM taxi_zone_lookup"
        # (nombre corto, sin calificar) en vez del nombre completo, la
        # consulta tira TABLE_OR_VIEW_NOT_FOUND aunque la tabla exista.
        catalog="nyc_taxi_analytics",
        schema="fare_prediction",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchmany(100)  # tope duro por si faltó el LIMIT

    return json.dumps([dict(zip(cols, r)) for r in rows], default=str)


def get_fare_quote(pickup_zone, dropoff_zone, pickup_datetime, passenger_count=1) -> str:
    """Llama a la API de cotización de la Fase 3 (publica, sin credenciales)."""
    resp = requests.post(
        os.environ["QUOTE_API_URL"],
        json={
            "pickup_zone": pickup_zone,
            "dropoff_zone": dropoff_zone,
            "pickup_datetime": pickup_datetime,
            "passenger_count": passenger_count,
        },
        timeout=30,
    )
    return json.dumps(resp.json())


def execute_tool(name: str, args: dict) -> str:
    """Despacha la herramienta que pidió el modelo; los errores vuelven al modelo como texto."""
    try:
        if name == "run_sql":
            return run_sql(args["query"])
        if name == "get_fare_quote":
            return get_fare_quote(**args)
        return json.dumps({"error": f"Herramienta desconocida: {name}"})
    except Exception as e:
        # Sin este log, un fallo de herramienta quedaba invisible en
        # CloudWatch: el modelo recibe el error como texto y sigue la
        # conversacion normalmente, pero del lado del servidor no quedaba
        # rastro de que algo salio mal.
        logger.error(f"Error ejecutando herramienta {name}({args}): {e}", exc_info=True)
        return json.dumps({"error": str(e)})


def ask(question: str, openai_api_key: str) -> str:
    """Loop agéntico: el modelo decide qué herramientas usar hasta tener la respuesta.
    La key de OpenAI es del visitante - se usa solo para este cliente, en este
    request, nunca se guarda."""
    client = OpenAI(api_key=openai_api_key)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    while True:
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=2048,
            tools=TOOLS,
            messages=messages,
        )
        message = response.choices[0].message

        if response.choices[0].finish_reason != "tool_calls":
            return message.content

        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ],
        })

        for tc in message.tool_calls:
            args = json.loads(tc.function.arguments)
            logger.info(f"herramienta: {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": execute_tool(tc.function.name, args),
            })


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body debe ser JSON válido"})

    question = body.get("question")
    openai_api_key = body.get("openai_api_key")

    if not question or not isinstance(question, str):
        return _response(400, {"error": "Falta el campo requerido: question"})
    if not openai_api_key or not isinstance(openai_api_key, str):
        return _response(400, {"error": "Falta el campo requerido: openai_api_key"})

    try:
        answer = ask(question, openai_api_key)
        return _response(200, {"answer": answer})
    except Exception as e:
        logger.error(f"Error en el loop del agente: {e}", exc_info=True)
        return _response(502, {"error": "No se pudo obtener una respuesta del agente"})


def _response(status, body):
    # Sin Access-Control-Allow-Origin aca: la Function URL ya lo agrega sola
    # (bloque cors en terraform/ask_agent.tf) para TODA respuesta, no solo
    # el preflight OPTIONS - agregarlo tambien aca duplicaba el header, y el
    # browser rechaza una respuesta con Access-Control-Allow-Origin repetido
    # (mismo sintoma que un CORS mal configurado: "Failed to fetch").
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }
