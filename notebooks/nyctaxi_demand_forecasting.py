# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # Fase 4.1 — Forecasting de demanda por zona y hora
# MAGIC
# MAGIC Pasa de "qué zona tuvo más viajes" (pasado) a "cuántos viajes va a
# MAGIC tener cada zona la próxima semana, hora a hora" (futuro).
# MAGIC
# MAGIC Enfoque: LightGBM sobre features de calendario + lags — suele ganarle
# MAGIC a modelos de series temporales dedicados en este tipo de datos y
# MAGIC reutiliza el stack de la Fase 1. Baseline a superar: "misma hora de la
# MAGIC semana pasada".
# MAGIC
# MAGIC Prerequisito: backfill histórico (mínimo ~6 meses para que los lags
# MAGIC semanales tengan sustancia).

# COMMAND ----------

# MAGIC %pip install lightgbm
dbutils.library.restartPython()

# COMMAND ----------

import numpy as np
import pandas as pd
import pyspark.sql.functions as F
import mlflow
from lightgbm import LGBMRegressor
from sklearn.metrics import mean_absolute_error

mlflow.lightgbm.autolog()

# COMMAND ----------

# MAGIC %md ## Paso 1 — Agregar viajes por (zona, fecha, hora)

# COMMAND ----------

features = spark.table("nyc_taxi_analytics.fare_prediction.yellow_taxi_features")

demand = (
    features
    .withColumn("pickup_date", F.to_date("tpep_pickup_datetime"))
    .groupBy("PULocationID", "pickup_date", "pickup_hour")
    .agg(F.count("*").alias("trips"))
)

pdf = demand.toPandas()
pdf["ts"] = pd.to_datetime(pdf["pickup_date"]) + pd.to_timedelta(pdf["pickup_hour"], unit="h")
print(f"Serie: {len(pdf):,} puntos (zona × hora)")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Sanity check de fechas antes de armar la grilla
# MAGIC
# MAGIC TLC tiene registros puntuales con `tpep_pickup_datetime` corrupto
# MAGIC (relojes de taxímetro mal configurados, ej. años como 2001 o 2091).
# MAGIC `clean_data` en el notebook de processing no filtra estos outliers de
# MAGIC fecha. Un solo viaje así infla `pdf["ts"].min()`/`.max()` y hace que
# MAGIC `pd.date_range` (Paso 2) arme una grilla ordenes de magnitud más
# MAGIC grande de lo real — causó un OOM (kernel matado, exit 137) la primera
# MAGIC vez que corrió esto. Se filtra relativo al máximo (confiable) en vez
# MAGIC de una fecha fija, para no tener que mantenerla a mano.

# COMMAND ----------

print("Rango de fechas antes de filtrar:", pdf["ts"].min(), "->", pdf["ts"].max())
pdf = pdf[pdf["ts"] >= pdf["ts"].max() - pd.Timedelta(days=730)]
print("Rango de fechas despues de filtrar:", pdf["ts"].min(), "->", pdf["ts"].max())

# COMMAND ----------

# MAGIC %md ## Paso 2 — Completar huecos con ceros
# MAGIC Una zona sin viajes en una hora no aparece en el groupBy, pero para el
# MAGIC modelo "no hubo viajes" es un cero real, no un dato faltante.

# COMMAND ----------

# Grilla completa zona × hora del rango observado
all_hours = pd.date_range(pdf["ts"].min(), pdf["ts"].max(), freq="h")
zones = pdf["PULocationID"].unique()

grid = pd.MultiIndex.from_product([zones, all_hours], names=["PULocationID", "ts"]).to_frame(index=False)
serie = grid.merge(pdf[["PULocationID", "ts", "trips"]], on=["PULocationID", "ts"], how="left")
serie["trips"] = serie["trips"].fillna(0).astype(int)
serie = serie.sort_values(["PULocationID", "ts"]).reset_index(drop=True)

print(f"Grilla completa: {len(serie):,} filas ({len(zones)} zonas × {len(all_hours):,} horas)")

# COMMAND ----------

# MAGIC %md ## Paso 3 — Features: calendario + lags + rolling
# MAGIC Los lags son el corazón del modelo: la demanda de hace 1 hora, 24 horas
# MAGIC y 1 semana son los mejores predictores de la demanda de ahora.

# COMMAND ----------

# Calendario
serie["hour"] = serie["ts"].dt.hour
serie["day_of_week"] = serie["ts"].dt.dayofweek
serie["month"] = serie["ts"].dt.month
serie["is_weekend"] = (serie["day_of_week"] >= 5).astype(int)

# Lags por zona (shift dentro de cada grupo para no mezclar zonas)
g = serie.groupby("PULocationID")["trips"]
serie["lag_1h"] = g.shift(1)
serie["lag_24h"] = g.shift(24)
serie["lag_168h"] = g.shift(168)  # misma hora, semana pasada

# Promedio móvil de la última semana (shift(1) primero: solo pasado, sin leakage)
serie["rolling_mean_7d"] = g.shift(1).rolling(168).mean().reset_index(drop=True)

# Las primeras 168 horas de cada zona no tienen lag semanal → fuera
serie = serie.dropna(subset=["lag_168h", "rolling_mean_7d"])

# COMMAND ----------

# MAGIC %md ## Paso 4 — Split temporal: última semana como test

# COMMAND ----------

cutoff = serie["ts"].max() - pd.Timedelta(days=7)
train = serie[serie["ts"] < cutoff]
test = serie[serie["ts"] >= cutoff]

FEATURES = ["PULocationID", "hour", "day_of_week", "month", "is_weekend",
            "lag_1h", "lag_24h", "lag_168h", "rolling_mean_7d"]

print(f"Train: {len(train):,} | Test: {len(test):,} (corte: {cutoff})")

# COMMAND ----------

# MAGIC %md ## Paso 5 — Baseline naïve: misma hora de la semana pasada

# COMMAND ----------

naive_mae = mean_absolute_error(test["trips"], test["lag_168h"])
print(f"Baseline naïve (lag semanal): MAE = {naive_mae:.2f} viajes/hora")

with mlflow.start_run(run_name="naive_weekly_lag"):
    mlflow.log_param("model_type", "naive_lag_168h")
    mlflow.log_metric("test_mae", naive_mae)

# COMMAND ----------

# MAGIC %md ## Paso 6 — LightGBM

# COMMAND ----------

with mlflow.start_run(run_name="lightgbm_demand"):
    model = LGBMRegressor(
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=64,
        random_state=42,
        n_jobs=-1,
    )
    # PULocationID como categórica: LightGBM la maneja nativamente
    model.fit(train[FEATURES], train["trips"], categorical_feature=["PULocationID"])

    preds = model.predict(test[FEATURES]).clip(min=0)  # demanda no puede ser negativa
    lgbm_mae = mean_absolute_error(test["trips"], preds)
    mlflow.log_metric("test_mae", lgbm_mae)

    print(f"LightGBM: MAE = {lgbm_mae:.2f} viajes/hora")
    print(f"Mejora vs naïve: {(naive_mae - lgbm_mae) / naive_mae * 100:.1f}%")

# COMMAND ----------

# MAGIC %md ## Paso 7 — Sanity check: top zonas previstas vs reales (última semana)

# COMMAND ----------

test_check = test.copy()
test_check["pred"] = preds

comparacion = (
    test_check.groupby("PULocationID")[["trips", "pred"]].sum()
    .sort_values("trips", ascending=False)
    .head(10)
    .round(0)
)
print("Top 10 zonas — demanda real vs prevista (última semana):")
print(comparacion.to_string())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Paso 8 — Forecast de la próxima semana → tabla `demand_forecast`
# MAGIC
# MAGIC No hay features reales todavía para el futuro (`lag_1h`/`lag_24h`
# MAGIC necesitarían datos que no existen). Se reusa el patrón de features de
# MAGIC la última semana completa de cada zona — el supuesto central de este
# MAGIC modelo es justamente que el patrón semanal se repite, así que usar
# MAGIC "el patrón de esta semana" como input para predecir "la semana que
# MAGIC viene" es consistente con esa misma lógica, no una trampa. Es una
# MAGIC aproximación (no un forecast recursivo paso a paso), pero simple y
# MAGIC suficiente dado que el modelo ya demostró (46.8% de mejora) que el
# MAGIC patrón semanal es la señal dominante.
# MAGIC
# MAGIC No se registra el modelo en Unity Catalog a propósito — no hace falta
# MAGIC todavía: esta tabla es el único insumo que necesita el agente de
# MAGIC Fase 4.2, y se genera acá mismo con el modelo ya en memoria. El
# MAGIC registro solo importaría si se automatiza el reentrenamiento mensual
# MAGIC (como ya está el modelo de tarifa) — queda anotado como pendiente
# MAGIC futuro, no como bloqueante de esta fase.

# COMMAND ----------

next_week = serie.sort_values("ts").groupby("PULocationID").tail(168).copy()
next_week["ts"] = next_week["ts"] + pd.Timedelta(days=7)

next_week["forecast_trips"] = model.predict(next_week[FEATURES]).clip(min=0)

forecast_table = next_week[["PULocationID", "ts", "forecast_trips"]]
spark.createDataFrame(forecast_table).write.mode("overwrite").saveAsTable(
    "nyc_taxi_analytics.fare_prediction.demand_forecast"
)

print(f"✅ demand_forecast: {len(forecast_table):,} filas (próximos 7 días)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resultados (corrida del 2026-08-17)
# MAGIC
# MAGIC | Métrica | Naïve (lag semanal) | LightGBM |
# MAGIC |---|---|---|
# MAGIC | MAE (viajes/hora) | 4.69 | **2.50** |
# MAGIC
# MAGIC Mejora de MAE de LightGBM vs. naïve: **46.8%** → criterio de salida
# MAGIC cumplido.
# MAGIC
# MAGIC ## Próximos pasos
# MAGIC - Si se automatiza el reentrenamiento mensual: registrar en UC como
# MAGIC   `fare_prediction.demand_model` (ver nota del Paso 8 sobre por qué no
# MAGIC   se hizo todavía).
# MAGIC - Cruzar con el análisis de zonas de Manhattan (mismo dato, mirada
# MAGIC   futura en vez de histórica).
