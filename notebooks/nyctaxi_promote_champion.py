# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC #### Fase 2.3 - Promover challenger a `champion`
# MAGIC
# MAGIC Corre despues de `nyctaxi_register_model.py`. Toma la ultima version registrada de `fare_model` (el challenger recien creado) y decide si le mueve el alias `champion`

# COMMAND ----------

import mlflow

mlflow.set_registry_uri("databricks-uc")

MODEL_NAME = "nyc_taxi_analytics.fare_prediction.fare_model"

# COMMAND ----------

# MAGIC %md #### Encontrar el challenger (ultima version registrada)

# COMMAND ----------

client = mlflow.tracking.MlflowClient()

all_versions = client.search_model_versions(f"name='{MODEL_NAME}'")
if not all_versions:
    raise ValueError(f"No hay ninguna version registrada de {MODEL_NAME} todavia. ")

challenger_version = max(all_versions, key=lambda v: int(v.version))
print(f"Challenger: version {challenger_version.version}")

# COMMAND ----------

# MAGIC %md #### Comparar contra el `champion` actual (si existe) y decidir

# COMMAND ----------

try:
    champion_version = client.get_model_version_by_alias(MODEL_NAME, "champion")
except mlflow.exceptions.MlflowException:
    champion_version = None

if champion_version is None:
    client.set_registered_model_alias(MODEL_NAME, "champion", challenger_version.version)
    print(f"✅ Primer champion: version {challenger_version.version} (no habia uno previo)")
elif champion_version.version == challenger_version.version:
    print(f"✅ Champion: version {challenger_version.version} (ya es el champion actual)")
else:
    champion_run = mlflow.get_run(champion_version.run_id)
    challenger_run = mlflow.get_run(challenger_version.run_id)
    champion_test_mae = champion_run.data.metrics.get("test_mae")
    challenger_test_mae = challenger_run.data.metrics.get("test_mae")

    print(f"Champion actual: version {champion_version.version}, test_mae={champion_test_mae:.2f}")
    print(f"Challenger: version {challenger_version.version}, test_mae={challenger_test_mae:.2f}")

    if champion_test_mae is None or challenger_test_mae < champion_test_mae:
        client.set_registered_model_alias(MODEL_NAME, "champion", challenger_version.version)
        print(f"✅ El challenger gana - champion pasa a ser la version {challenger_version.version}")
    else:
        print(
            f"El challenger (v{challenger_version.version}) no supera al champion actual "
            f"(v{champion_version.version}) - se mantiene sin cambios"
        )
