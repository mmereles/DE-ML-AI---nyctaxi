# Databricks notebook source
# MAGIC %md
# MAGIC # NYC Yellow Taxi - Processing
# MAGIC
# MAGIC Convertido desde `nyctaxi - processing.ipynb` a formato `.py` de
# MAGIC Databricks - mismo formato que el resto de los notebooks del proyecto,
# MAGIC porque el Git folder (`databricks_repo`) no estaba sincronizando el
# MAGIC `.ipynb` (blob JSON) correctamente al workspace.

# COMMAND ----------

import pyspark.sql.functions as F
from pyspark.sql.types import *
from pyspark.sql import SparkSession
from datetime import datetime, timezone
import logging
import boto3

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CloudWatch client for business/data-quality metrics emitted by this notebook.
# Requires the job cluster to have an instance profile with cloudwatch:PutMetricData
# (see terraform/main.tf -> aws_iam_role.databricks_processing_instance_role).
cloudwatch = boto3.client("cloudwatch")


def send_metric(metric_name, value, unit="Count", extra_dimensions=None):
    """Push one metric to CloudWatch under the same namespace the trigger Lambda
    uses (NYCTaxiProcessing), tagged with the year/month being processed so runs
    can be filtered/graphed individually.

    Deliberately swallows errors: a failed metric push must never break the
    actual data pipeline (job success/failure is still tracked externally,
    not by this notebook - see conversation history for why).
    """
    dimensions = [
        {"Name": "Year", "Value": str(source_year)},
        {"Name": "Month", "Value": str(source_month)},
    ]
    if extra_dimensions:
        dimensions.extend(extra_dimensions)

    try:
        cloudwatch.put_metric_data(
            Namespace="NYCTaxiProcessing",
            MetricData=[{
                "MetricName": metric_name,
                "Value": float(value),
                "Unit": unit,
                "Dimensions": dimensions,
            }],
        )
    except Exception as e:
        logger.warning(f"Failed to send CloudWatch metric {metric_name}: {e}")

# COMMAND ----------

# Define parameters expected from the job
dbutils.widgets.text("source_year", "")
dbutils.widgets.text("source_month", "")
dbutils.widgets.text("source_bucket", "")
dbutils.widgets.text("source_key", "")

# Get parameters from  job
source_year = dbutils.widgets.get("source_year")
source_month = dbutils.widgets.get("source_month")
source_bucket = dbutils.widgets.get("source_bucket")
source_key = dbutils.widgets.get('source_key')

print(f"source_year: {source_year}")
print(f"source_month: {source_month}")
print(f"source_bucket: {source_bucket}")
print(f"source_key: {source_key}")

# COMMAND ----------

raw_path = f"s3://{source_bucket}/{source_key}"
df = spark.read.parquet(raw_path)

# COMMAND ----------

def load_and_validate(file_path):
    """Load raw data and perform initial validation"""
    try:
        # load and cache data (reused across multiple downstream actions)
        df = spark.read.parquet(file_path).cache()
        initial_count = df.count()
        if initial_count == 0:
            raise ValueError("No data found in the file")

        logger.info(f"METRIC: Initial data load: {initial_count} records from {file_path}")
        # Business metric: how many raw records came in for this run
        send_metric("InitialRecords", initial_count)

        return df, initial_count
    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        raise

df, initial_count = load_and_validate(raw_path)

# COMMAND ----------

def clean_data(df):
    """Apply data quality checks and clean data"""

    # Initial count
    start_count = df.count()

    # Remove records with invalid coordinates
    df_clean = df.filter(
        (F.col("PULocationID").isNotNull()) &
        (F.col("DOLocationID").isNotNull()) &
        (F.col("trip_distance") > 0) &
        (F.col("fare_amount") > 0) &
        (F.col("fare_amount") < 1000) &
        (F.col("total_amount") > 0) &
        (F.col("passenger_count") > 0) &
        (F.col("passenger_count") <= 6) &
        (F.col("tpep_pickup_datetime").isNotNull()) &
        (F.col("tpep_dropoff_datetime").isNotNull()) &
        (F.col("tpep_dropoff_datetime") > F.col("tpep_pickup_datetime")) &
        ((F.unix_timestamp("tpep_dropoff_datetime") - F.unix_timestamp("tpep_pickup_datetime")) < 86400)
    ).cache()

    final_count = df_clean.count()
    removed_count = start_count - final_count
    quality_pct = (final_count / start_count) * 100

    logger.info(f"Data cleaning: {start_count} -> {final_count} records")

    logger.info(f"METRIC: Data cleaning: {start_count} records -> {final_count} records (removed {removed_count})")
    logger.info(f"METRIC: Records removed {removed_count}")
    logger.info(f"METRIC: DataqualityStore {quality_pct} Percent")

    # Business/data-quality metrics for this run
    send_metric("RecordsRemoved", removed_count)
    send_metric("DataQualityPercent", quality_pct, unit="Percent")

    # raw df no longer needed once cleaned/cached version exists
    df.unpersist()

    return df_clean

cleaned_df = clean_data(df)

# COMMAND ----------

def load_zone_categories():
    """Load the official TLC zone lookup and derive Manhattan/airport zone ID
    lists dynamically, instead of a hand-maintained hardcoded list - that
    hardcoded approach previously misclassified zone 1 (EWR/Newark) as
    Manhattan, since a stale list fails silently with no warning.

    Plain S3 path (not a Unity Catalog table) on purpose: setup_unity_catalog()
    runs later in this notebook, so the catalog/schema don't exist yet here.
    """
    staging_path = f"s3://{source_bucket}/nyctaxi/staging/taxi_zone_lookup.csv"
    dbutils.fs.cp(
        "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv",
        staging_path,
    )
    zone_lookup = spark.read.option("header", True).csv(staging_path)

    manhattan = [
        row.LocationID for row in
        zone_lookup.filter(F.col("Borough") == "Manhattan").select("LocationID").distinct().collect()
    ]
    # service_zone (not Borough) is the official TLC field for airports -
    # JFK/LGA sit in Queens and EWR has its own Borough, so Borough alone
    # can't identify all three consistently.
    airports = [
        row.LocationID for row in
        zone_lookup.filter(F.col("service_zone") == "Airports").select("LocationID").distinct().collect()
    ]
    return manhattan, airports

manhattan_zones, airport_zones = load_zone_categories()

# COMMAND ----------

def engineer_features(df):
    """Create features for fare prediction model"""

    logger.info("Starting feature engineering...")

    # Calculate trip duration in minutes
    df_features = df.withColumn(
        "duration_minutes",
        (F.unix_timestamp(F.col("tpep_dropoff_datetime")) -
         F.unix_timestamp(F.col("tpep_pickup_datetime"))) / 60
    )

    # Temporal features
    df_features = df_features.withColumn("pickup_hour", F.hour("tpep_pickup_datetime")) \
        .withColumn("pickup_day_of_week", F.dayofweek("tpep_pickup_datetime")) \
        .withColumn("pickup_month", F.month("tpep_pickup_datetime")) \
        .withColumn("pickup_year", F.year("tpep_pickup_datetime"))

    # Derived temporal features
    df_features = df_features \
        .withColumn("is_weekend", F.when(F.col("pickup_day_of_week").isin([1, 7]), 1).otherwise(0)) \
        .withColumn("is_rush_hour", F.when((F.col("pickup_hour").between(7, 9)) |
                                           (F.col("pickup_hour").between(17, 19)), 1).otherwise(0)) \
        .withColumn("season",
                    F.when(F.col("pickup_month").isin([12, 1, 2]), "winter")
                    .when(F.col("pickup_month").isin([3, 4, 5]), "spring")
                    .when(F.col("pickup_month").isin([6, 7, 8]), "summer")
                    .otherwise("fall"))

    # Trip characteristics
    df_features = df_features \
        .withColumn("trip_distance_km", F.col("trip_distance") * 1.60934) \
        .withColumn("avg_speed_kmh",
                    F.when(F.col("duration_minutes") > 0,
                            (F.col("trip_distance_km") / F.col("duration_minutes")) * 60)
                    .otherwise(0)) \
        .withColumn("fare_per_mile", F.col("fare_amount") / F.col("trip_distance")) \
        .withColumn("fare_per_minute", F.col("fare_amount") / F.col("duration_minutes"))

    # Payments features
    df_features = df_features \
        .withColumn("tip_percentage",
                    F.when(F.col("fare_amount") > 0,
                           (F.col("tip_amount") / F.col("fare_amount")) * 100)
                    .otherwise(0)) \
        .withColumn("total_amount_per_passenger",
                    F.col("total_amount") / F.col("passenger_count"))


    # Location features - manhattan_zones/airport_zones come from the official
    # TLC zone lookup (load_zone_categories(), above), not a hardcoded list.
    df_features = df_features \
        .withColumn("pickup_manhattan",
                    F.when(F.col("PULocationID").isin(manhattan_zones), 1).otherwise(0)) \
        .withColumn("dropoff_manhattan",
                    F.when(F.col("DOLocationID").isin(manhattan_zones), 1).otherwise(0)) \
        .withColumn("manhattan_trip",
                    F.when((F.col("pickup_manhattan") == 1) | (F.col("dropoff_manhattan") == 1), 1).otherwise(0)) \
        .withColumn("is_airport_trip",
                    F.when(F.col("PULocationID").isin(airport_zones) |
                           F.col("DOLocationID").isin(airport_zones), 1).otherwise(0))

    # Add processing metadata
    df_features = df_features \
        .withColumn("processed_timestamp", F.current_timestamp()) \
        .withColumn("processed_year", F.lit(source_year)) \
        .withColumn("processing_month", F.lit(source_month))

    logger.info("Feature engineering completed.")

    return df_features

# Apply feature engineering and cache the result since it feeds validation,
# the Delta write, and the summary log (all separate Spark actions).
feature_df = engineer_features(cleaned_df).cache()
feature_df.count()
cleaned_df.unpersist()

# COMMAND ----------

def validate_features(df):
    """Validate engineered features"""

    validation_results = {}

    # Check for null values in key features
    key_features = ["duration_minutes", "avg_speed_kmh", "fare_per_mile", "tip_percentage"]

    for feature in key_features:
        null_count = df.filter(F.col(feature).isNull()).count()
        validation_results[f"{feature}_nulls"] = null_count

        if null_count > 0:
            logger.warning(f"Found {null_count} null values in {feature}")

    # Check for unreasonable values
    unreasonable_speed = df.filter(F.col("avg_speed_kmh") > 200).count()
    unreasonable_duration = df.filter(F.col("duration_minutes") > 1440).count()

    validation_results["unreasonable_speed_count"] = unreasonable_speed
    validation_results["unreasonable_duration_count"] = unreasonable_duration

    # Send validation metrics: one CloudWatch metric per check, tagged with a
    # "Check" dimension so they can be broken out individually on a dashboard,
    # in addition to the existing per-run log line.
    for metric_name, value in validation_results.items():
        logger.info(f"METRIC: Validation_{metric_name} = {value}")
        send_metric(
            "FeatureValidation",
            value,
            extra_dimensions=[{"Name": "Check", "Value": metric_name}],
        )

    logger.info(f"Feature validation completed: {validation_results}")
    return validation_results

# Validate features
validation_results = validate_features(feature_df)

# COMMAND ----------

def setup_unity_catalog():
    """Create the Unity Catalog schema if it doesn't exist."""

    try:
        # Create catalog with managed Location
        catalog_location = f"s3://{source_bucket}/unity-catalog/nyc_taxi_analytics/"

        spark.sql(f"""
            CREATE CATALOG IF NOT EXISTS nyc_taxi_analytics
            MANAGED LOCATION '{catalog_location}'
        """)
        logger.info("Catalog 'nyc_taxi_analytics' created or already exists.")

        # Create Schema
        spark.sql("CREATE SCHEMA IF NOT EXISTS nyc_taxi_analytics.fare_prediction")
        logger.info("Schema 'nyc_taxi_analytics.fare_prediction' created or already exists.")

        return True
    except Exception as e:
        logger.error(f"Failed to setup Unity Catalog: {e}")
        raise e

setup_unity_catalog()

# COMMAND ----------

def save_to_delta(df, table_path, table_name, year, month):
    """Save processed data to Delta Lake format"""

    try:
        record_count = df.count()
        logger.info(f"Saving {record_count} records to {table_path}")

        # Overwrite only the (processed_year, processing_month) partition being
        # processed, instead of blind append, so re-running the job for the
        # same source file doesn't duplicate records.
        df.write \
            .format("delta") \
            .mode("overwrite") \
            .option("mergeSchema", "true") \
            .option("replaceWhere", f"processed_year = '{year}' AND processing_month = '{month}'") \
            .partitionBy("processed_year", "processing_month") \
            .save(table_path)

        # Register/Update table in Unity catalog
        spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {table_name}
            USING DELTA
            LOCATION '{table_path}'
        """)

        # Send success metrics
        logger.info(f"METRIC: RecordsWritten {record_count}")
        send_metric("RecordsWritten", record_count)

        logger.info(f"Successfully saved data to {table_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to save data to Delta Lake: {e}")
        raise e

# Define paths and table names
processed_table_path = f"s3://{source_bucket}/nyctaxi/processed/yellow_taxi_features/"
catalog_table_name = "nyc_taxi_analytics.fare_prediction.yellow_taxi_features"

# Save features to Delta Lake
save_success = save_to_delta(feature_df, processed_table_path, catalog_table_name, source_year, source_month)

# COMMAND ----------

def log_processing_summary():
    """Log final processing summary"""

    summary = {
        "source_file": f"{source_bucket}/{source_key}",
        "processing_date": datetime.now(timezone.utc).isoformat(),
        "initial_records": initial_count,
        "final_records": feature_df.count(),
        "success": save_success
    }

    logger.info(f"Processing summary: {summary}")

    # Overall run outcome as a metric (1/0), separate from the per-stage
    # metrics above - lets a dashboard/alarm show "did this run's business
    # logic complete cleanly" alongside the external job-status signal.
    send_metric("ProcessingSuccess", 1 if save_success else 0)

    # Save processing log to S3
    log_path = f"s3://{source_bucket}/nyctaxi/processing_logs/success/{source_year}_{source_month}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"

    log_df = spark.createDataFrame([summary])
    log_df.coalesce(1).write.mode("overwrite").json(log_path)
    return summary

# Generate final summary
processing_summary = log_processing_summary()
feature_df.unpersist()

print("Processing completed successfully!")
print(f"Processed {processing_summary['final_records']} records")
print(f"Data saved to: {catalog_table_name}")
