# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC #### Fase 2.4 - Ground truth loop: Medir al `champion` contra el mes recien llegado
# MAGIC
# MAGIC Corre inmediatamente después de `process`, **antes** de `train` — a propósito. La idea (del roadmap): las tarifas reales del mes N llegan recién en el mes N+1, así que apenas ese mes nuevo entra a `yellow_taxi_features`, es la primera vez que existe "verdad" contra la cual medir al modelo que **ya estaba sirviendo** — sin esperar a ninguna infraestructura de logging de predicciones en producción.

# COMMAND ----------

# MAGIC %pip install xgboost
dbutils.library.restartPython() 

# COMMAND ----------

import mlflow
import pandas as pd
import numpy as np
import boto3
import pyspark.sql.functions as F
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")

MODEL_NAME = "nyc_taxi_analytics.fare_prediction.fare_model"

# Mismo mecanismo que ya usa "nyctaxi - processing" para mandar metricas custom
cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")

# Debe coincidir exactamente con PRETRIP_FEATURES de nyctaxi_fare_prediction_training
PRETRIP_FEATURES = [
    "PULocationID", "DOLocationID",
    "trip_distance",
    "passenger_count",
    "pickup_hour", "pickup_day_of_week",
    "pickup_month", "is_weekend", "is_rush_hour", "season",
    "pickup_manhattan", "dropoff_manhattan", "manhattan_trip", "is_airport_trip",
]

# COMMAND ----------

# MAGIC %md #### Encontrar el mes mas reciente ya procesado

# COMMAND ----------

features_df = spark.table("nyc_taxi_analytics.fare_prediction.yellow_taxi_features")

latest = (
    features_df
    .select("processed_year", "processing_month")
    .distinct()
    .withColumn(
        "sort_key",
        F.col("processed_year").cast("int") * 100 + F.col("processing_month").cast("int"),
    )
    .orderBy(F.col("sort_key").desc())
    .first()
)

eval_year, eval_month = latest["processed_year"], latest["processing_month"]
print(f"Evaluando al champion sobre {eval_year}-{eval_month}")

# COMMAND ----------

# MAGIC %md #### Cargar el `champion` actual si existe

# COMMAND ----------

try:
    champion_model = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}@champion")
except Exception as e:
    print(e)
    print(f"⚠️ No hay champion registrado todavía, se saltea el ground truth check: {e}")
    dbutils.notebook.exit("no champion yet")

# COMMAND ----------

# MAGIC %md #### Preparar features + target del mes evaluado

# COMMAND ----------

month_df = (
    features_df
    .filter((F.col("processed_year") == eval_year) & (F.col("processing_month") == eval_month))
    .withColumn("target_fare_total", F.col("total_amount") - F.col("tip_amount"))
)

pdf = month_df.select(*PRETRIP_FEATURES, "target_fare_total").toPandas()
print(f"Filas evaluadas: {len(pdf):,}")

y_true = pdf["target_fare_total"]
X_eval = pd.get_dummies(pdf.drop(columns=["target_fare_total"]), columns=["season"])

# Alinear columnas contra lo que el modelo espera (mismo patron que el reindex de X_test en el training)
expected_columns = champion_model.metadata.get_input_schema().input_names()
input_schema = champion_model.metadata.get_input_schema()
expected_columns = input_schema.input_names()
X_eval = X_eval.reindex(columns=expected_columns, fill_value=0)

dtype_map = {inp.name: inp.type.to_pandas() for inp in input_schema.inputs}
X_eval = X_eval.astype(dtype_map)

# COMMAND ----------

# MAGIC %md #### Predecir, medir, publicar en cloudwatch

# COMMAND ----------

y_pred = champion_model.predict(X_eval)

mae = mean_absolute_error(y_true, y_pred)
rmse = np.sqrt(mean_squared_error(y_true, y_pred))
r2 = r2_score(y_true, y_pred)
pct_within_2usd = float(np.mean(np.abs(y_true - y_pred) <= 2.0) *100)

print(
    f"Champion sobre {eval_year}-{eval_month}: MAE=${mae:.2f}, RMSE=${rmse:.2f} "
    f"R2={r2:.3f} | ±$2: ${pct_within_2usd:.1f}%"
)

try:
    cloudwatch.put_metric_data(
        Namespace="NYCTaxiML",
        MetricData=[
            {
                "MetricName": "ChampionGroundTruthMAE",
                "Value": float(mae),
                "Unit": "None",
                "Dimensions": [
                    {
                        "Name": "Model",
                        "Value": "fare_model",
                    },
                ]
            },
            {
                "MetricName": "ChampionGroundTruthWithin2USD",
                "Value": pct_within_2usd,
                "Unit": "Percent",
                "Dimensions": [
                    {
                        "Name": "Model",
                        "Value": "fare_model",
                    },
                ]
            },
        ],
    )
    print("✅ Métricas publicadas en CloudWatch (namespace NYCTaxiML).")
except Exception as e:
    print(f"⚠️ No se pudo publicar en CloudWatch: {e}")
