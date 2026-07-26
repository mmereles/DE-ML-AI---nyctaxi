import boto3
import requests
from datetime import datetime, timedelta
import time
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

logger = logging.getLogger(__name__)

s3_client = boto3.client('s3')
cloudwatch = boto3.client('cloudwatch')

def lambda_handler(event, context):
    logger.info("=== NYC Taxi Ingestion Started ===")
    logger.info(f"Event: {event}")
    logger.info(f"Context: {context}")

    s3_bucket = os.getenv('S3_BUCKET', 'nyc-taxi-bucket-12313')
    s3_prefix = os.getenv('S3_PREFIX', 'raw/')

    logger.info(f"Configuration - S3 Bucket: {s3_bucket}, Prefix: {s3_prefix}")

    # Find the target month - 2 months back (not 1), since TLC publishes
    # data with a lag and last month's file isn't available yet when this
    # runs (confirmed: a 1-month lag gets a 403 from TLC's CloudFront).
    now = datetime.now()
    target_date = now - timedelta(days=64)
    year = str(target_date.year)
    month = str(target_date.month).zfill(2)
    logger.info(f"Target month: {year}-{month}")

    try:
        # Check if already exists
        if file_exist_in_s3(s3_client, s3_bucket, s3_prefix, year, month):
            logger.warning(f"Data for {year}-{month} already exists in S3. Skipping download.")
            put_skipped_metric(cloudwatch, year, month)
            return {
                'statusCode': 200,
                'body': f"Data already exists for {year}-{month}"
            }

        # Try to download
        success = process_month(s3_client, cloudwatch, s3_bucket, s3_prefix, year, month)
        if not success:
            raise RuntimeError(f"Failed to download/upload data for {year}-{month} after retries")

        logger.info(f"Successfully processed {year}-{month}")
        put_success_metric(cloudwatch, year, month)
        return {
            'statusCode': 200,
            'body': f"Successfully processed {year}-{month}"
        }

    except Exception as e:
        logger.error(f"Unexpected error in lambda handler: {str(e)}", exc_info=True)
        put_failure_metric(cloudwatch, year, month)
        raise

def file_exist_in_s3(s3_client, bucket, prefix, year, month):
    """Check if file already exits in S3"""
    file_name = f"yellow_tripdata_{year}-{month}.parquet"
    s3_key = f"{prefix}year={year}/month={month}/{file_name}"

    logger.info(f"file_name: {file_name}, s3_key: {s3_key}")

    try:
        s3_client.head_object(Bucket=bucket, Key=s3_key)
        logger.info(f"File already exists: s3://{bucket}/{prefix}year={year}/month={month}/{file_name}")
        return True

    except s3_client.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            logger.info(f"File does not exist yet: s3://{bucket}/{s3_key}")
            return False
        else:
            # Other error (permissions, etc)
            logger.error(f"Error checking S3: {e}")
            return False

def process_month(s3_client, cloudwatch, bucket, prefix, year, month):
    """Process a specific month's data with performance"""
    file_name = f"yellow_tripdata_{year}-{month}.parquet"
    url = f"https://d37ci6vzurychx.cloudfront.net/trip-data/{file_name}"
    s3_key = f"{prefix}year={year}/month={month}/{file_name}"

    start_time = time.time()

    try:
        logger.info(f"Attempting download: {url}")

        for attempt in range(3):
            try:
                attempt_start = time.time()

                # Download with timeout
                response = requests.get(url, stream=True, timeout=30)
                response.raise_for_status()

                # Get file size from headers
                file_size_bytes = int(response.headers.get('content-length', 0))
                logger.info(f"File Size: {file_size_bytes} bytes ({file_size_bytes/1024/1024:.2f} MB)")

                # Upload to S3 and measure duration
                upload_start = time.time()
                s3_client.upload_fileobj(
                    response.raw,
                    bucket,
                    s3_key
                )

                upload_duration = time.time() - upload_start

                # calculate total download duration
                download_duration = time.time() - attempt_start

                logger.info(f"Successfully uploaded to s3://{bucket}/{s3_key}")
                logger.info(f"Download & upload completed in {download_duration:.2f} seconds")

                # Send performance metrics to Cloudwatch
                send_performance_metrics(
                    cloudwatch,
                    year,
                    month,
                    download_duration,
                    upload_duration,
                    file_size_bytes,
                    True
                )

                return True

            except requests.exceptions.RequestException as e:
                attempt_duration = time.time() - attempt_start
                logger.warning(f"Attempt {attempt + 1} failed in {attempt_duration:.2f}s: {e}")

        logger.error(f"All 3 attempts failed for {file_name}")
        return False

    except Exception as e:
        error_duration = time.time() - start_time
        logger.error(f"failed {file_name} after {error_duration:.2f}s: {str(e)}")
        return False

def put_skipped_metric(cloudwatch, year, month):
    """Record skipped download (already exists)"""
    try:
        cloudwatch.put_metric_data(
            Namespace='NYCTaxiDownload',
            MetricData=[{
                'MetricName': 'JobSkipped',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': 1.0,
                'Unit': 'Count'
            }]
        )
        logger.info(f"Recorded job skipped for {year}-{month}")
    except Exception as e:
        logger.error(f"Failed to record skipped metric: {e}")

def send_performance_metrics(cloudwatch, year, month, download_duration, upload_duration, file_size_bytes, success):
    """Send detailed performance metrics to Cloudwatch"""
    try:
        metric_data = []

        # Download duration metric
        metric_data.append({
            'MetricName': 'DownloadDuration',
            'Dimensions': [
                {'Name': 'Year', 'Value': year},
                {'Name': 'Month', 'Value': month},
                {'Name': 'Status', 'Value': 'Success' if success else 'Failure'}
            ],
            'Value': download_duration,
            'Unit': 'Seconds'
        })

        # Upload duration metric (only for successful downloads)
        if success and upload_duration > 0:
            metric_data.append({
                'MetricName': 'UploadDuration',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': upload_duration,
                'Unit': 'Seconds'
            })

        if success and file_size_bytes > 0:
            metric_data.append({
                'MetricName': 'FileSize',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': file_size_bytes,
                'Unit': 'Bytes'
            })

        if success and file_size_bytes > 0:
            metric_data.append({
                'MetricName': 'FileSizeMB',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': file_size_bytes / (1024 * 1024),
                'Unit': 'Megabytes'
            })

        # Throughput metric (MB/s)
        if success and download_duration > 0 and file_size_bytes > 0:
            throughput = file_size_bytes / download_duration / (1024 * 1024)
            metric_data.append({
                'MetricName': 'DownloadThroughput',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': throughput,
                'Unit': 'Megabytes/Second'
            })

        # Send all metric in one call (more efficient)
        if metric_data:
            cloudwatch.put_metric_data(
                Namespace='NYCTaxiDownload',
                MetricData=metric_data
            )
            logger.debug(f"Sent {len(metric_data)} performance metrics to Cloudwatch")

    except Exception as e:
        logger.error(f"Failed to send performance metrics to Cloudwatch: {e}")

def put_success_metric(cloudwatch, year, month):
    """Record overall job success"""
    try:
        cloudwatch.put_metric_data(
            Namespace='NYCTaxiDownload',
            MetricData=[{
                'MetricName': 'JobSuccess',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': 1.0,
                'Unit': 'Count'
            }]
        )
        logger.info(f"Recorded job success for {year}-{month}")
    except Exception as e:
        logger.error(f"Failed to record success metric: {e}")

def put_failure_metric(cloudwatch, year, month):
    """Record overall job failure"""
    try:
        cloudwatch.put_metric_data(
            Namespace='NYCTaxiDownload',
            MetricData=[{
                'MetricName': 'JobFailure',
                'Dimensions': [
                    {'Name': 'Year', 'Value': year},
                    {'Name': 'Month', 'Value': month}
                ],
                'Value': 1.0,
                'Unit': 'Count'
            }]
        )
        logger.info(f"Recorded job failure for {year}-{month}")
    except Exception as e:
        logger.error(f"Failed to record failure metric: {e}")
