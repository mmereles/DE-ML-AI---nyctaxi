from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import ingestion


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "HeadObject")


def test_file_exist_in_s3_true_when_head_object_succeeds():
    s3_client = MagicMock()
    s3_client.exceptions.ClientError = ClientError
    s3_client.head_object.return_value = {}

    assert ingestion.file_exist_in_s3(s3_client, "bucket", "raw/", "2024", "01") is True
    s3_client.head_object.assert_called_once_with(
        Bucket="bucket", Key="raw/year=2024/month=01/yellow_tripdata_2024-01.parquet"
    )


def test_file_exist_in_s3_false_on_404():
    s3_client = MagicMock()
    s3_client.exceptions.ClientError = ClientError
    s3_client.head_object.side_effect = _client_error("404")

    assert ingestion.file_exist_in_s3(s3_client, "bucket", "raw/", "2024", "01") is False


def test_file_exist_in_s3_false_on_other_error():
    # Un error que no sea "no existe" (p. ej. permisos) hoy también devuelve
    # False - documentado acá como comportamiento actual, no como lo ideal
    # (un 403 debería alertar distinto de un 404 legítimo).
    s3_client = MagicMock()
    s3_client.exceptions.ClientError = ClientError
    s3_client.head_object.side_effect = _client_error("403")

    assert ingestion.file_exist_in_s3(s3_client, "bucket", "raw/", "2024", "01") is False


def test_process_month_success(monkeypatch):
    fake_response = MagicMock()
    fake_response.headers = {"content-length": "1024"}
    fake_response.raise_for_status = MagicMock()
    fake_response.raw = MagicMock()

    monkeypatch.setattr(ingestion.requests, "get", MagicMock(return_value=fake_response))

    s3_client = MagicMock()
    cloudwatch = MagicMock()

    result = ingestion.process_month(s3_client, cloudwatch, "bucket", "raw/", "2024", "01")

    assert result is True
    s3_client.upload_fileobj.assert_called_once()
    cloudwatch.put_metric_data.assert_called_once()


def test_process_month_returns_false_after_three_failed_attempts(monkeypatch):
    get_mock = MagicMock(side_effect=ingestion.requests.exceptions.RequestException("timeout"))
    monkeypatch.setattr(ingestion.requests, "get", get_mock)

    s3_client = MagicMock()
    cloudwatch = MagicMock()

    result = ingestion.process_month(s3_client, cloudwatch, "bucket", "raw/", "2024", "01")

    assert result is False
    assert get_mock.call_count == 3
    s3_client.upload_fileobj.assert_not_called()


def test_lambda_handler_skips_when_file_already_exists(monkeypatch):
    monkeypatch.setattr(ingestion, "file_exist_in_s3", lambda *a, **k: True)
    put_skipped = MagicMock()
    monkeypatch.setattr(ingestion, "put_skipped_metric", put_skipped)
    monkeypatch.setattr(
        ingestion,
        "process_month",
        MagicMock(side_effect=AssertionError("no debería intentar descargar si ya existe")),
    )

    result = ingestion.lambda_handler({}, None)

    assert result["statusCode"] == 200
    assert "already exists" in result["body"]
    put_skipped.assert_called_once()


def test_lambda_handler_raises_when_download_fails(monkeypatch):
    monkeypatch.setattr(ingestion, "file_exist_in_s3", lambda *a, **k: False)
    monkeypatch.setattr(ingestion, "process_month", lambda *a, **k: False)
    monkeypatch.setattr(ingestion, "put_failure_metric", MagicMock())

    with pytest.raises(RuntimeError):
        ingestion.lambda_handler({}, None)