import io
import json
from unittest.mock import MagicMock

import quote_api


def _event(body):
    return {"body": json.dumps(body)}


def _fake_sagemaker_response(fare_total):
    payload = json.dumps({"estimated_fare_total": fare_total}).encode()
    return {"Body": io.BytesIO(payload)}


def _valid_body(**overrides):
    body = {
        "pickup_zone": 132,
        "dropoff_zone": 230,
        "pickup_datetime": "2026-08-14 18:00:00",
        "passenger_count": 1,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------

def test_validate_accepts_a_well_formed_body():
    assert quote_api._validate(_valid_body()) is None


def test_validate_defaults_passenger_count_when_missing():
    body = _valid_body()
    del body["passenger_count"]
    assert quote_api._validate(body) is None


def test_validate_rejects_missing_required_field():
    body = _valid_body()
    del body["pickup_datetime"]
    error = quote_api._validate(body)
    assert error == "Falta el campo requerido: pickup_datetime"


def test_validate_rejects_zone_out_of_range():
    error = quote_api._validate(_valid_body(pickup_zone=264))
    assert "pickup_zone" in error


def test_validate_rejects_zone_of_wrong_type():
    error = quote_api._validate(_valid_body(dropoff_zone="230"))
    assert "dropoff_zone" in error


def test_validate_rejects_passenger_count_out_of_range():
    error = quote_api._validate(_valid_body(passenger_count=5))
    assert "passenger_count" in error


# ---------------------------------------------------------------------------
# _query_serving_endpoint
# ---------------------------------------------------------------------------

def test_query_serving_endpoint_translates_payload_and_parses_response(monkeypatch):
    monkeypatch.setenv("SAGEMAKER_ENDPOINT_NAME", "nyctaxi-fare-quote")
    invoke_mock = MagicMock(return_value=_fake_sagemaker_response(58.33))
    monkeypatch.setattr(quote_api._sagemaker_client, "invoke_endpoint", invoke_mock)

    fare = quote_api._query_serving_endpoint(_valid_body())

    assert fare == 58.33
    _, kwargs = invoke_mock.call_args
    assert kwargs["EndpointName"] == "nyctaxi-fare-quote"
    assert kwargs["ContentType"] == "application/json"
    sent_payload = json.loads(kwargs["Body"])
    assert sent_payload == {
        "PULocationID": 132,
        "DOLocationID": 230,
        "pickup_datetime": "2026-08-14 18:00:00",
        "passenger_count": 1,
    }


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------

def test_lambda_handler_returns_200_with_fare_on_success(monkeypatch):
    monkeypatch.setenv("SAGEMAKER_ENDPOINT_NAME", "nyctaxi-fare-quote")
    monkeypatch.setattr(
        quote_api._sagemaker_client,
        "invoke_endpoint",
        MagicMock(return_value=_fake_sagemaker_response(58.33)),
    )

    result = quote_api.lambda_handler(_event(_valid_body()), None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["estimated_fare_total"] == 58.33
    assert body["currency"] == "USD"


def test_lambda_handler_rejects_invalid_json_body():
    result = quote_api.lambda_handler({"body": "{not valid json"}, None)
    assert result["statusCode"] == 400


def test_lambda_handler_rejects_body_failing_validation():
    body = _valid_body(pickup_zone=999)
    result = quote_api.lambda_handler(_event(body), None)
    assert result["statusCode"] == 400


def test_lambda_handler_never_calls_sagemaker_when_validation_fails(monkeypatch):
    invoke_mock = MagicMock()
    monkeypatch.setattr(quote_api._sagemaker_client, "invoke_endpoint", invoke_mock)

    quote_api.lambda_handler(_event(_valid_body(passenger_count=99)), None)

    invoke_mock.assert_not_called()


def test_lambda_handler_returns_502_when_sagemaker_call_fails(monkeypatch):
    monkeypatch.setenv("SAGEMAKER_ENDPOINT_NAME", "nyctaxi-fare-quote")
    monkeypatch.setattr(
        quote_api._sagemaker_client,
        "invoke_endpoint",
        MagicMock(side_effect=RuntimeError("ModelError: 500 from container")),
    )

    result = quote_api.lambda_handler(_event(_valid_body()), None)

    assert result["statusCode"] == 502
