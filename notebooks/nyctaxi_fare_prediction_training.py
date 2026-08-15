# Databricks notebook source

# COMMAND ----------

# MAGIC %md ML Clasico

# COMMAND ----------

# MAGIC %md ### 1. Load the libraries

# COMMAND ----------

# MAGIC %pip install xgboost
dbutils.library.restartPython() 

# COMMAND ----------


import numpy as np
import pandas as pd
import mlflow
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import RandomizedSearchCV
from xgboost import XGBRegressor

mlflow.set_tracking_uri("databricks")
mlflow.set_registry_uri("databricks-uc")
EXPERIMENT_NAME = "/Shared/nyctaxi_fare_prediction"
mlflow.set_experiment(EXPERIMENT_NAME)

# autolog capture parameters and metrics
mlflow.sklearn.autolog()
mlflow.xgboost.autolog()

# COMMAND ----------

import pyspark.sql.functions as F
features_df = spark.table("nyc_taxi_analytics.fare_prediction.yellow_taxi_features")

features_df = features_df.withColumn(
    "target_fare_total",
    F.col("total_amount") - F.col("tip_amount")
)

# Sanity target
neg_targets = features_df.filter(F.col("target_fare_total") <= 0).count()
print(f"Registros con target <= 0: {neg_targets}")

# COMMAND ----------

# MAGIC %md ### Select pre-travel features

# COMMAND ----------

PRETRIP_FEATURES = [
    "PULocationID", "DOLocationID",      # dónde
    "trip_distance",                      # proxy de distancia estimada
    "passenger_count",                    # cuántos
    "pickup_hour", "pickup_day_of_week",  # cuándo
    "pickup_month", "is_weekend", "is_rush_hour", "season",
    "pickup_manhattan", "dropoff_manhattan", "manhattan_trip", "is_airport_trip",
]

model_df = features_df.select(
    *PRETRIP_FEATURES,
    "tpep_pickup_datetime",
    "target_fare_total"
)

# COMMAND ----------

# MAGIC %md ### Download one node with pandas

# COMMAND ----------

SAMPLE_FRACTION = 0.2

if SAMPLE_FRACTION < 1.0:
    model_df = model_df.sample(fraction=SAMPLE_FRACTION, seed=42)

pdf = model_df.toPandas()
print(f"Filas en pandas: {len(pdf):,}")

# COMMAND ----------

# MAGIC %md #### Split train/test

# COMMAND ----------

print(len(pdf))

# COMMAND ----------

pdf["tpep_pickup_datetime"] = pd.to_datetime(pdf["tpep_pickup_datetime"])
cutoff = pdf["tpep_pickup_datetime"].max() - pd.DateOffset(months=1)

train_pdf = pdf[pdf["tpep_pickup_datetime"] < cutoff].copy()
test_pdf = pdf[pdf["tpep_pickup_datetime"] >= cutoff].copy()

print(f"Corte temporal: {cutoff.date()}")
print(f"Train: {len(train_pdf):,} filas | Test: {len(test_pdf):,} filas")

assert len(train_pdf) + len(test_pdf), "Train mas chico que test, falta backfill historico"

# COMMAND ----------

# MAGIC %md
# MAGIC ##### PASO 5 - BASELINE NAIVE: mediana por par (origen, destino)
# MAGIC
# MAGIC El psio que cualquier modeo tiene que superar para justificar su existencia: "la tarifa de un viaje A-B sera la mediana historica de los viajes A-B"

# COMMAND ----------

pair_median = (
    train_pdf
    .groupby(["PULocationID", "DOLocationID"])["target_fare_total"]
    .median()
    .rename("naive_pred")
)

global_median = train_pdf["target_fare_total"].median()

naive_pred = (
    test_pdf[["PULocationID", "DOLocationID"]]
    .join(pair_median, on=["PULocationID", "DOLocationID"])
    ["naive_pred"]
    .fillna(global_median)
    .to_numpy()
)

unseen_pairs_pct = (
    test_pdf[["PULocationID", "DOLocationID"]]
    .join(pair_median, on=["PULocationID", "DOLocationID"])
    ["naive_pred"].isna().mean() * 100
)

print(f"Pares (PU,DO) de test no vistos en train: {unseen_pairs_pct:.1f}%")

# COMMAND ----------

# MAGIC %md #### PASO 6 - Funcion de evaluacion: tecnica + negocio

# COMMAND ----------

def evaluate(name, y_true, y_pred, log_to_mlflow=True):
    """Calcua y muestra metricas tecnicas y de negocio; las loguea a mlflow"""
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)

    metrics = {
        "mae": mean_absolute_error(y_true, y_pred),
        "rmse": np.sqrt(mean_squared_error(y_true, y_pred)),
        "r2": r2_score(y_true, y_pred),
        # metricas de negocio
        "pct_within_2usd": np.mean(np.abs(y_true - y_pred) <= 2.0) * 100,
        "pct_within_10pct": np.mean(np.abs(y_true - y_pred) / y_true <= 0.10) * 100,
    }

    print(
        f"{name}: MAE=${metrics['mae']:.2f}, RMSE=${metrics['rmse']:-2f} "
        f"R2={metrics['r2']:.3f} | ±$2: {metrics['pct_within_2usd']:.1f}% "
        f"10%: {metrics['pct_within_10pct']:.1f}%"
    )

    if log_to_mlflow and mlflow.active_run() is not None:
        mlflow.log_metrics({f"test_{k}": v for k, v in metrics.items()})
    
    return metrics

# Evaluar el baseline naive en si propio run de MLFLOW, asi aparece en la 
# misma tabla de experimentos que los modelos reales
with mlflow.start_run(run_name="naive_median_by_zone_pair"):
    mlflow.log_param("model_type", "naive_median")
    naive_metrics = evaluate("Naive (mediana por par)", test_pdf["target_fare_total"], naive_pred)

# COMMAND ----------

# MAGIC %md #### Preparacion de matrices X/y (comun a los paso 7-9)

# COMMAND ----------

drop_cols = ["tpep_pickup_datetime", "target_fare_total"]

X_train = pd.get_dummies(train_pdf.drop(columns=drop_cols), columns=["season"])
X_test = (
    pd.get_dummies(test_pdf.drop(columns=drop_cols), columns=["season"])
    .reindex(columns=X_train.columns, fill_value=0)
)
y_train = train_pdf["target_fare_total"]
y_test = test_pdf["target_fare_total"]

print(f"Matriz de entrenamiento: {X_train.shape}")
print(f"Matriz de test: {X_test.shape}")

# COMMAND ----------

# MAGIC %md #### PASO 7 - Ridge - referencia lineal

# COMMAND ----------

with mlflow.start_run(run_name="random_baseline"):
    ridge = Ridge(alpha=1.0)
    ridge.fit(X_train, y_train)
    ridge_metrics = evaluate("Ridge", y_test, ridge.predict(X_test))

# COMMAND ----------

# MAGIC %md #### PAOS 8 - XGBoost: modelo principal

# COMMAND ----------

with mlflow.start_run(run_name="xgboost_default") as xgb_run:
    xgb = XGBRegressor(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        n_jobs=1
    )
    xgb.fit(X_train, y_train)
    xgb_metrics = evaluate("XGBoost (default)", y_test, xgb.predict(X_test))

    importances = pd.Series(xgb.feature_importances_, index=X_train.columns).sort_values(ascending=False)
    print("\nTop 10 features por importancia:")
    print(importances.head(10).to_string())

    xgb_run_id = xgb_run.info.run_id
    # copiar al widget de nyc_taxi_register_model.py
    print(f"\nxgb_run_id: {xgb_run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC #### PASO 9 - Comparacion final y criterio de decision
# MAGIC
# MAGIC criterio de salida de la Fase 1 (del roadmap): **XGBoost debe ganarle al baseline naive con margen claro en el test temporal.**
# MAGIC Si no lo hace, el modelo no justifica su complejidad y hay que revisar features o datos antes de avanzar a la Fase 2 (Registro en Model Registry, retraining automatico)

# COMMAND ----------

comparison = pd.DataFrame(
    {
        "naive (Mediana por par)": naive_metrics,
        "ridge": ridge_metrics,
        "XGBoost (default)": xgb_metrics,
    }
).T[["mae", "rmse", "r2", "pct_within_2usd", "pct_within_10pct"]]

print(comparison.round(2).to_string())

mae_improvement = (naive_metrics["mae"] - xgb_metrics["mae"]) / naive_metrics["mae"] * 100
print(f"\nMejora de MAE de XGBoost vs naive: {mae_improvement:.1f}%")

if mae_improvement > 10:
    print("✅ XGBoost supera al naïve con margen — listo para Fase 2 (registrar en Model Registry).")
else:
    print("⚠️ Margen insuficiente sobre el naïve — revisar features/datos antes de avanzar.")

# COMMAND ----------

# MAGIC %md Publicar Outputs para la task de registro (fase 2.1)

# COMMAND ----------

with mlflow.start_run(run_id=xgb_run_id):
    mlflow.log_metric("mae_improvement_pct", mae_improvement)
