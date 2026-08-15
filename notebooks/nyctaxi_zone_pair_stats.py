# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fase 3.1 - Estadisticas por par de zonas (origen, destino)
# MAGIC
# MAGIC Este Notebook precomputa la **mediana historica por par (PULocationID, DOLocation)** y la guarda como tabla Delta

# COMMAND ----------

import pyspark.sql.functions as F

# COMMAND ----------

# MAGIC %md Leo la tabla de features ya procesada

# COMMAND ----------

features = spark.table("nyc_taxi_analytics.fare_prediction.yellow_taxi_features")
features.head(10)

# COMMAND ----------

# MAGIC %md Mediana de distancia/peajes/tarifa por par de zonas + conteo de viajes.

# COMMAND ----------

zone_pair_stats = (
    features
    .groupBy("PULocationID", "DOLocationID")
    .agg(
        F.percentile_approx("trip_distance", 0,5).alias("medium_trip_distance"),
        F.percentile_approx("tolls_amount", 0.5).alias("median_tolls"),
        F.percentile_approx(F.col("Total_amount") - F.col("tip_amount"), 0.5).alias("median_fare_total"),
        F.count("*").alias("trip_count")
    )
)
zone_pair_stats.withColumn("medium_trip_distance", F.round("medium_trip_distance", 2)).display()

# COMMAND ----------

# MAGIC %md Pares con menos de 10 viajes: la mediana es ruido, se marcan para que el serving use el fallback global en vez de confiar en ellas

# COMMAND ----------

MIN_TRIPS = 10
zone_pair_stats = zone_pair_stats.withColumn(
    "reliable", (F.col("trip_count") >= MIN_TRIPS).cast("boolean")
)
zone_pair_stats.display()

# COMMAND ----------

# MAGIC %md Fallback global (una sola fila) para pares nunca vistos o poco confiables

# COMMAND ----------

global_stats = features.agg(
    F.percentile_approx("trip_distance", 0.5).alias("global_median_distance"),
    F.percentile_approx("tolls_amount", 0.5).alias("global_median_tolls"),
).collect()[0]

print(f"Fallback global - distance: {global_stats['global_median_distance']}, peajes: {global_stats['global_median_tolls']}")

# COMMAND ----------

# MAGIC %md guardo como tabla Delta (overwrite completo: es una tabla derivada chica)

# COMMAND ----------

zone_pair_stats.write.mode("overwrite").saveAsTable("nyc_taxi_analytics.fare_prediction.zone_pair_stats")

# COMMAND ----------

# MAGIC %md El fallback global tmabien se persiste

# COMMAND ----------

spark.createDataFrame([global_stats.asDict()]).write.mode("overwrite").saveAsTable("nyc_taxi_analytics.fare_prediction.zone_pair_stats_global")

# COMMAND ----------

print(f"✅ zone_pair_stats: {zone_pair_stats.count():,} pares guardados")
