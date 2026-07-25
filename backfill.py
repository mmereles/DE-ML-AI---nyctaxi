"""
Backfill historico de datos de NYC Yellow Taxi (Fase 0.1 del roadmap).

Descarga N meses hacia atras desde el CloudFront oficial de TLC y los sube a
S3 bajo nyctaxi/historical/ - un prefix separado de nyctaxi/raw/ a proposito,
para NO disparar el EventBridge rule (scopeado solo a nyctaxi/raw/) ni el
processing automatico. Este historico se procesa manual, una vez, cuando se
arranque la Fase 1 de entrenamiento.

Uso:
    python backfill.py [--months 18] [--lag 2] [--bucket nyc-taxi-bucket-12313]
"""
import argparse
import logging
import time
from datetime import datetime

import boto3
import requests
from dateutil.relativedelta import relativedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TLC_BASE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data"
S3_PREFIX = "nyctaxi/historical/"


def file_exists_in_s3(s3_client, bucket, year, month):
    s3_key = f"{S3_PREFIX}year={year}/month={month}/yellow_tripdata_{year}-{month}.parquet"
    try:
        s3_client.head_object(Bucket=bucket, Key=s3_key)
        return True
    except s3_client.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def download_and_upload(s3_client, bucket, year, month):
    file_name = f"yellow_tripdata_{year}-{month}.parquet"
    url = f"{TLC_BASE_URL}/{file_name}"
    s3_key = f"{S3_PREFIX}year={year}/month={month}/{file_name}"

    for attempt in range(3):
        try:
            start = time.time()
            response = requests.get(url, stream=True, timeout=30)
            if response.status_code == 404:
                logger.warning(f"{year}-{month}: no publicado todavia en TLC (404), se salta")
                return "not_published"
            response.raise_for_status()

            size_mb = int(response.headers.get("content-length", 0)) / 1024 / 1024
            s3_client.upload_fileobj(response.raw, bucket, s3_key)
            logger.info(f"{year}-{month}: subido a s3://{bucket}/{s3_key} ({size_mb:.1f} MB, {time.time()-start:.1f}s)")
            return "uploaded"

        except requests.exceptions.RequestException as e:
            logger.warning(f"{year}-{month}: intento {attempt + 1} fallo: {e}")

    logger.error(f"{year}-{month}: fallaron los 3 intentos")
    return "failed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=18, help="Cuantos meses hacia atras bajar")
    parser.add_argument("--lag", type=int, default=2, help="Meses de margen desde hoy (TLC tarda en publicar)")
    parser.add_argument("--bucket", default="nyc-taxi-bucket-12313")
    args = parser.parse_args()

    s3_client = boto3.client("s3")
    anchor = datetime.now().replace(day=1) - relativedelta(months=args.lag)

    results = {"uploaded": 0, "skipped": 0, "not_published": 0, "failed": 0}

    for i in range(args.months):
        target = anchor - relativedelta(months=i)
        year, month = str(target.year), str(target.month).zfill(2)

        if file_exists_in_s3(s3_client, args.bucket, year, month):
            logger.info(f"{year}-{month}: ya existe en S3, se salta")
            results["skipped"] += 1
            continue

        outcome = download_and_upload(s3_client, args.bucket, year, month)
        results[outcome] += 1

    logger.info(f"=== Backfill terminado: {results} ===")


if __name__ == "__main__":
    main()
