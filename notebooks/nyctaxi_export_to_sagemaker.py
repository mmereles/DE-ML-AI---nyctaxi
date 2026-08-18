# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC #### Empaquetar el champion y subirlo a S3 para sagemaker
# MAGIC
# MAGIC toma el modeo con alias `champion` en Unity Catalog Model Registry (`nyctaxi_register_model.py`, FASE 2.1) lo empaqueta junto con los artefactos que necesita inference.py para reconstruir features en el momento de servir y sube el resultado a S3

# COMMAND ----------

# MAGIC %pip install xgboost
dbutils.library.restartPython() 

# COMMAND ----------

import json
import os
import tarfile
import shutil

import mlflow
import pandas as pd
from mlflow import MlflowClient

mlflow.set_registry_uri("databricks-uc")

MODEL_NAME = "nyc_taxi_analytics.fare_prediction.fare_model"
BUCKET = "nyc-taxi-bucket-12313"
username = spark.sql("SELECT current_user()").collect()[0][0]
BUILD_DIR = f"/Workspace/Users/{username}/sagemaker_model_build"

# COMMAND ----------

# MAGIC %md ##### 1. Cargar el champion y extraer el booster crudo

# COMMAND ----------

client = MlflowClient()
champion = client.get_model_version_by_alias(MODEL_NAME, "champion")
model_version = champion.version
print(f"Empaquetando {MODEL_NAME} v{model_version} (champion)")

xgb_model = mlflow.xgboost.load_model(f"models:/{MODEL_NAME}@champion")

if os.path.exists(BUILD_DIR):
    shutil.rmtree(BUILD_DIR)
os.makedirs(f"{BUILD_DIR}/code")

xgb_model.get_booster().save_model(f"{BUILD_DIR}/xgboost-model.json")

# COMMAND ----------

booster = xgb_model.get_booster()
print(f"Árboles: {booster.num_boosted_rounds()}, features esperadas: {booster.num_features()}")

# COMMAND ----------

# MAGIC %md ##### 2. Derivar flags de zona (manhattan/aeropuerto) del lookup oficial de TLC

# COMMAND ----------

zone_lookup_pdf = pd.read_csv("https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv")

zone_flags = {
    str(int(row["LocationID"])): {
        "is_manhattan": bool(row["Borough"] == "Manhattan"),
        "is_airport": bool(row["service_zone"] == "Airports"),
        "borough": row["Borough"]
    }
    for _, row in zone_lookup_pdf.iterrows()
}

with open(f"{BUILD_DIR}/zone_flags.json", "w") as f:
    json.dump(zone_flags, f)

print(f"Zonas exportadas: {len(zone_flags)} "
      f"({sum(v['is_manhattan'] for v in zone_flags.values())} Manhattan, "
      f"{sum(v['is_airport'] for v in zone_flags.values())} aeropuerto)")

# COMMAND ----------

# MAGIC %md ##### 3 - Exportar zone_pair_stats (el reemplazo de trip distance)

# COMMAND ----------

zone_pair_stats_pdf = spark.table("nyc_taxi_analytics.fare_prediction.zone_pair_stats").toPandas()
zone_pair_stats_pdf.attrs.clear()
zone_pair_stats_pdf.to_parquet(f"{BUILD_DIR}/zone_pair_stats.parquet")

zone_pair_stats_pdf = spark.table("nyc_taxi_analytics.fare_prediction.zone_pair_stats_global").toPandas()
zone_pair_stats_pdf.attrs.clear()
zone_pair_stats_pdf.to_parquet(f"{BUILD_DIR}/zone_pair_stats_global.parquet")

# Fallback intermedio por (PUBorough, DOBorough) - ver nyctaxi_zone_pair_stats.py
# para el porque: mucho mas representativo que el fallback global para pares
# de zonas perifericos con poca o ninguna historia.
zone_pair_stats_borough_pdf = spark.table("nyc_taxi_analytics.fare_prediction.zone_pair_stats_borough").toPandas()
zone_pair_stats_borough_pdf.attrs.clear()
zone_pair_stats_borough_pdf.to_parquet(f"{BUILD_DIR}/zone_pair_stats_borough.parquet")

# COMMAND ----------

# MAGIC %md ##### 4 - Copiar el codigo de inference (script mode)

# COMMAND ----------

repo_sagemaker_dir = "../sagemaker"

shutil.copy(f"{repo_sagemaker_dir}/inference.py", f"{BUILD_DIR}/code/inference.py")
shutil.copy(f"{repo_sagemaker_dir}/requirements.txt", f"{BUILD_DIR}/code/requirements.txt")

# COMMAND ----------

# MAGIC %md ##### 5 - Armar model.tar.gz

# COMMAND ----------

tar_path = f"{BUILD_DIR}/model.tar.gz"
with tarfile.open(tar_path, "w:gz") as tar:
    for name in ["xgboost-model.json", "zone_flags.json", "zone_pair_stats.parquet",
                  "zone_pair_stats_global.parquet", "zone_pair_stats_borough.parquet"]:
        tar.add(f"{BUILD_DIR}/{name}", arcname=name)
    tar.add(f"{BUILD_DIR}/code", arcname="code")

print(f"model.tar.gz armado: {os.path.getsize(tar_path):,} bytes")

# COMMAND ----------

# MAGIC %md ##### 6 - Subir a S3

# COMMAND ----------

s3_path = f"s3://{BUCKET}/sagemaker/fare-quote-model/v{model_version}/model.tar.gz"
print(tar_path)
dbutils.fs.cp(f"file:{tar_path}", s3_path)

print(f"✅Subido a {s3_path}")
print(f"\nPegar en terraform.tfvars:\nmodel_version = \"{model_version}\"")
