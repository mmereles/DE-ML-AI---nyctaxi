"""
Fase 4.2 — Asistente de lenguaje natural sobre los datos del pipeline.

Agente con OpenAI (Chat Completions API) que responde preguntas como:
  "¿Cuánto costaría un viaje de Midtown a JFK un viernes a las 18?"
  "¿Qué zona de Manhattan tuvo más viajes el mes pasado?"

Dos herramientas:
  - run_sql: consultas SELECT (solo lectura) contra las tablas de Unity
    Catalog vía Databricks SQL warehouse.
  - get_fare_quote: llama a la API de cotización de la Fase 3.

Variables de entorno requeridas:
  OPENAI_API_KEY, DATABRICKS_HOST, DATABRICKS_TOKEN,
  DATABRICKS_SQL_WAREHOUSE_ID, QUOTE_API_URL

Uso: python nyctaxi_assistant.py "tu pregunta"
"""

import os
import sys
import json
import requests
from openai import OpenAI
from databricks import sql as databricks_sql

MODEL = "gpt-4o"  # cambiar al modelo mas capaz disponible con tu key

# Esquema de las tablas embebido en el system prompt: el agente no necesita
# descubrirlo en cada conversación
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

# Formato de OpenAI: cada tool va envuelta en {"type": "function", "function": {...}}
# (Anthropic usa el mismo JSON Schema, pero sin el nivel extra de anidado).
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


def run_sql(query: str) -> str:
    """Ejecuta la consulta en el SQL warehouse. Guarda: solo SELECT."""
    # Defensa simple contra escrituras: el agente solo debe leer
    if not query.strip().lower().startswith("select"):
        return json.dumps({"error": "Solo se permiten consultas SELECT"})

    with databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"].replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}",
        access_token=os.environ["DATABRICKS_TOKEN"],
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchmany(100)  # tope duro por si faltó el LIMIT

    return json.dumps([dict(zip(cols, r)) for r in rows], default=str)


def get_fare_quote(pickup_zone, dropoff_zone, pickup_datetime, passenger_count=1) -> str:
    """Llama a la API de cotización de la Fase 3."""
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
        return json.dumps({"error": str(e)})


def ask(question: str) -> str:
    """Loop agéntico: el modelo decide qué herramientas usar hasta tener la respuesta."""
    client = OpenAI()
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

        # Si el modelo no pidió herramientas, terminó: devolver el texto
        if response.choices[0].finish_reason != "tool_calls":
            return message.content

        # OpenAI espera el mensaje del asistente con sus tool_calls tal cual
        # se lo devolvió, seguido de UN mensaje "tool" por cada llamada
        # (a diferencia de Anthropic, que agrupa todos los resultados en un
        # solo mensaje "user" con varios bloques tool_result).
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
            # A diferencia de Anthropic (block.input ya viene como dict),
            # OpenAI manda los argumentos como texto JSON - hay que parsearlos.
            args = json.loads(tc.function.arguments)
            print(f"  [herramienta: {tc.function.name}({json.dumps(args, ensure_ascii=False)})]")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": execute_tool(tc.function.name, args),
            })


if __name__ == "__main__":
    pregunta = " ".join(sys.argv[1:]) or "¿Qué zona de Manhattan tuvo más pickups en total?"
    print(f"Pregunta: {pregunta}\n")
    print(ask(pregunta))
