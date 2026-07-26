# Databricks notebook source
# MAGIC %md
# MAGIC # Procesar el backfill histórico (loop mensual)
# MAGIC
# MAGIC `nyctaxi/historical/` (donde `backfill.py` sube los meses) es un prefix
# MAGIC separado a propósito de `nyctaxi/raw/` - no dispara el EventBridge rule
# MAGIC ni el processing automático. Este notebook llama a
# MAGIC `nyctaxi - processing.py` una vez por cada mes encontrado ahí, con los
# MAGIC mismos widgets (`source_year`, `source_month`, `source_bucket`,
# MAGIC `source_key`) que usaría el job normal - así el histórico termina en la
# MAGIC misma tabla `yellow_taxi_features` que alimenta el entrenamiento.
# MAGIC
# MAGIC Correr esto DESPUÉS de `backfill.py` y ANTES de la Fase 1.

# COMMAND ----------

dbutils.widgets.text("bucket", "nyc-taxi-bucket-12313")
dbutils.widgets.text("prefix", "nyctaxi/historical/")
dbutils.widgets.text("processing_notebook_path", "/Workspace/Repos/production/nyctaxi-pipeline/nyctaxi - processing.py")
dbutils.widgets.text("timeout_seconds", "3600")

bucket = dbutils.widgets.get("bucket")
prefix = dbutils.widgets.get("prefix")
processing_notebook_path = dbutils.widgets.get("processing_notebook_path")
timeout_seconds = int(dbutils.widgets.get("timeout_seconds"))

# COMMAND ----------

# MAGIC %md ## Descubrir los meses subidos por el backfill

# COMMAND ----------

year_dirs = [f for f in dbutils.fs.ls(f"s3://{bucket}/{prefix}") if f.name.startswith("year=")]

months = []
for year_dir in year_dirs:
    year = year_dir.name.replace("year=", "").rstrip("/")
    month_dirs = [f for f in dbutils.fs.ls(year_dir.path) if f.name.startswith("month=")]
    for month_dir in month_dirs:
        month = month_dir.name.replace("month=", "").rstrip("/")
        months.append((year, month))

months.sort()
print(f"Meses a procesar: {len(months)}")
for year, month in months:
    print(f"  - {year}-{month}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Procesar mes a mes
# MAGIC
# MAGIC Un mes fallido no aborta el resto - se loguea y se sigue con el
# MAGIC siguiente, para no perder el trabajo ya hecho por una falla puntual
# MAGIC (ej. un mes con el parquet corrupto).

# COMMAND ----------

results = {"success": [], "failed": []}

for year, month in months:
    file_name = f"yellow_tripdata_{year}-{month}.parquet"
    source_key = f"{prefix}year={year}/month={month}/{file_name}"

    print(f"\n=== Procesando {year}-{month} ===")
    try:
        dbutils.notebook.run(
            processing_notebook_path,
            timeout_seconds,
            {
                "source_year": year,
                "source_month": month,
                "source_bucket": bucket,
                "source_key": source_key,
            },
        )
        results["success"].append(f"{year}-{month}")
        print(f"{year}-{month}: OK")
    except Exception as e:
        results["failed"].append(f"{year}-{month}")
        print(f"{year}-{month}: FALLO - {e}")

# COMMAND ----------

# MAGIC %md ## Resumen

# COMMAND ----------

print(f"Procesados OK: {len(results['success'])}")
print(f"Fallidos: {len(results['failed'])}")
if results["failed"]:
    print("Meses a revisar/reintentar:", results["failed"])
