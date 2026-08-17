import json
from unittest.mock import MagicMock
import pytest
import processing_trigger

def _s3_event(key, bucket="nyc-taxi-bucket-12313"):
    return {
        "detail": {
            "bucket": {"name": bucket},
             "object": {"key": key}
        }
    }

def test_lambda_handler_extracts_year_month_and_triggers_job(monkeypatch):
    monkeypatch.setattr(processing_trigger.boto3, "client", MagicMock())
    trigger_mock = MagicMock(return_value=999)
    monkeypatch.setattr(processing_trigger, "trigger_databricks_job", trigger_mock)

    event = _s3_event("nyctaxi/raw/year=2024/month=01/yellow_tripdata_2024-01.parquet")
    result = processing_trigger.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["run_id"] == 999
    trigger_mock.assert_called_once_with(
        "nyc-taxi-bucket-12313",
        "nyctaxi/raw/year=2024/month=01/yellow_tripdata_2024-01.parquet",
        "2024",
        "01",
    )

def test_lambda_handler_skips_non_yellow_taxi_file(monkeypatch):
    monkeypatch.setattr(processing_trigger.boto3, "client", MagicMock())
    trigger_mock = MagicMock()
    monkeypatch.setattr(processing_trigger, "trigger_databricks_job", trigger_mock)

    event = _s3_event("nyctaxi/raw/year=2024/month=01/some_other_file.parquet")
    result = processing_trigger.lambda_handler(event, None)

    assert result["statusCode"] == 200
    trigger_mock.assert_not_called()

def test_lambda_handler_missing_bucket_or_key(monkeypatch):
    monkeypatch.setattr(processing_trigger.boto3, "client", MagicMock())

    result = processing_trigger.lambda_handler({"detail": {}}, None)

    assert result["statusCode"] == 400

def test_lambda_handler_missing_year_month_in_key(monkeypatch):
    monkeypatch.setattr(processing_trigger.boto3, "client", MagicMock())

    event = _s3_event("nyctaxi/raw/yellow_tripdata_2024-01.parquet")
    result = processing_trigger.lambda_handler(event, None)

    assert result["statusCode"] == 400

def test_trigger_databricks_job_success(monkeypatch):
    monkeypatch.setattr(processing_trigger, "get_databricks_token", lambda secret_arn: "abc123")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("DATABRICKS_JOB_ID", "111")
    monkeypatch.setenv("DATABRICKS_SECRET_ARN", "arn:aws:secretsmanager:...:nyctaxi/databricks-token")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"run_id": 42}
    post_mock = MagicMock(return_value=fake_response)
    monkeypatch.setattr(processing_trigger.requests, "post", post_mock)

    run_id = processing_trigger.trigger_databricks_job("bucket", "key", "2024", "01")

    assert run_id == 42
    _, kwargs =  post_mock.call_args
    assert kwargs["json"]["job_id"] == 111
    assert kwargs["json"]["notebook_params"] == {
        "source_year": "2024",
        "source_month": "01",
        "source_bucket": "bucket",
        "source_key": "key",
    }

def test_trigger_databricks_job_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(processing_trigger, "get_databricks_token", lambda secret_arn: "abc123")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setenv("DATABRICKS_JOB_ID", "111")
    monkeypatch.setenv("DATABRICKS_SECRET_ARN", "arn:aws:secretsmanager:...:nyctaxi/databricks-token")

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "internal error"
    monkeypatch.setattr(processing_trigger.requests, "post", MagicMock(return_value=fake_response))

    with pytest.raises(Exception):
        processing_trigger.trigger_databricks_job("bucket", "key", "2024", "01")

