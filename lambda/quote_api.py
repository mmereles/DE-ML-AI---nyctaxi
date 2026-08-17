"""
Fase 3.3 - Lmabda detras de API Gateway: API publica de cotizacion

POST /quote  {"pickup_zone": 161, "dropoff_zone": 132,
              "pickup_datetime": "2026-07-15 18:00:00", "passenger_count": 2}
→ {"estimated_fare_total": 68.40}
"""

import json
import os
import logging

import boto3
from botocore.config import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# read_timeout explicito: un cold start del endpoint serverless puede tardar
# mas que el default de botocore y el techo real de todos modos lo pone el
# Integration timeout de API Gateway
_sagemaker_client = boto3.client(
    "sagemaker-runtime",
    config=Config(read_timeout=25, connect_timeout=5)
)

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Body debe ser JSON valido"})

    # Validacion de input: campos requeridos y rangos razonables
    error = _validate(body)
    if error:
        return _response(400, {"error": error})

    try:
        fare = _query_serving_endpoint(body)
        return _response(200, {
            "estimated_fare_total": round(fare, 2),
            "currency": "USD",
            "disclaimer": "Estimacion basada en datos historicos; no incluye propina",
        })
    except Exception as e:
        logger.error(f"Error consultando el endpoint de SageMaker: {e}", exc_info=True)
        return _response(502, {"error": "No se pudo obtener cotizacion"})

def _validate(body):
    """Chequeos minimos antes de gastar una llamada al endpoint"""
    for field in ("pickup_zone", "dropoff_zone", "pickup_datetime"):
        if field not in body:
            return f"Falta el campo requerido: {field}"

    # Zones TLC validas: 1 a 263
    for field in ("pickup_zone", "dropoff_zone"):
        if not isinstance(body[field], int) or not 1 <= body[field] <= 263:
            return f"{field} debe ser un numero entero entre 1 y 263"

    pc = body.get("passenger_count", 1)
    if not isinstance(pc, int) or not 1 <= pc <= 4:
        return "passenger_count debe ser un numero entero entre 1 y 4"

    return None

def _query_serving_endpoint(body):
    """Traduce el request publico al formato que espera inference.py y llama al endpoint de SageMkaer."""
    payload = {
        "PULocationID": body["pickup_zone"],
        "DOLocationID": body["dropoff_zone"],
        "pickup_datetime": body["pickup_datetime"],
        "passenger_count": body.get("passenger_count", 1),
    }

    response = _sagemaker_client.invoke_endpoint(
        EndpointName=os.environ["SAGEMAKER_ENDPOINT_NAME"],
        ContentType="application/json",
        Body=json.dumps(payload),
    )

    result = json.loads(response["Body"].read())
    return float(result["estimated_fare_total"])

def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }