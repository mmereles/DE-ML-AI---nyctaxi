import json
import os

import pandas as pd
import xgboost as xgb

# Ninguna tarifa real de taxi es negativa - piso de seguridad para el
# regressor, que no tiene garantia de salida no-negativa (ver
# _estimate_distance: cuando extrapola fuera de la distribucion de
# entrenamiento puede devolver cualquier cosa).
MIN_FARE = 3.00

def model_fn(model_dir):
    booster = xgb.Booster()
    booster.load_model(os.path.join(model_dir, "xgboost-model.json"))

    zone_stats = pd.read_parquet(
        os.path.join(model_dir, "zone_pair_stats.parquet")
    ).set_index(["PULocationID", "DOLocationID"])

    borough_stats = pd.read_parquet(
        os.path.join(model_dir, "zone_pair_stats_borough.parquet")
    ).set_index(["PUBorough", "DOBorough"])

    global_stats = pd.read_parquet(
        os.path.join(model_dir, "zone_pair_stats_global.parquet")
    ).iloc[0]

    with open(os.path.join(model_dir, "zone_flags.json")) as f:
        zone_flags = json.load(f)

    return {
        "booster": booster,
        "zone_stats": zone_stats,
        "borough_stats": borough_stats,
        "global_distance": float(global_stats["global_median_distance"]),
        "zone_flags": zone_flags
    }

def input_fn(request_body, request_content_type):
    if "application/json" not in (request_content_type or ""):
        raise ValueError("This model only supports application/json input")
    return json.loads(request_body)

def _estimate_distance(pu, do, zone_stats, borough_stats, zone_flags, global_distance):
    # 1. Mediana historica del par exacto, si es confiable (>= 10 viajes)
    try:
        row = zone_stats.loc[(pu, do)]
        if bool(row["reliable"]):
            return float(row["median_trip_distance"])
    except KeyError:
        pass

    # 2. Fallback por par de boroughs (ej. Bronx -> Brooklyn): mucho mas
    # representativo que el global para pares perifericos con poca o
    # ninguna historia - el global sale corto porque el dataset esta
    # dominado por viajes cortos en/cerca de Manhattan, y esa distancia
    # corta puesta en un par sin ninguna punta en Manhattan es una
    # combinacion de features que el modelo casi no vio entrenar (ver
    # nyctaxi_zone_pair_stats.py para el detalle).
    try:
        pu_borough = zone_flags[str(pu)]["borough"]
        do_borough = zone_flags[str(do)]["borough"]
        row = borough_stats.loc[(pu_borough, do_borough)]
        return float(row["median_trip_distance"])
    except KeyError:
        pass

    # 3. Fallback global: ningun dato para esta combinacion de boroughs
    return global_distance

def predict_fn(input_data, model):
    df = pd.DataFrame([input_data])
    dt = pd.to_datetime(df["pickup_datetime"])

    # Reconstruyo las mismas features temporales qeu usa el modelo de Fase 1
    df["pickup_hour"] = dt.dt.hour
    # pandas: lunes=0... domingo=6 -> convertir a la convencion de Spark
    # (F.dayofweek: doming=1... sabado=7)
    df["pickup_day_of_week"] = dt.dt.dayofweek + 2
    df["pickup_day_of_week"] = df["pickup_day_of_week"].where(df["pickup_day_of_week"] <= 7, 1)
    df["pickup_month"] = dt.dt.month
    df["is_weekend"] = df["pickup_day_of_week"].isin([1, 7]).astype(int)
    df["is_rush_hour"] = (
        df["pickup_hour"].between(7, 9) | df["pickup_hour"].between(17, 19)
    ).astype(int)

    season_map = {12: "winter", 1:"winter", 2:"winter", 3:"spring", 4:"spring", 5:"spring", 6:"summer", 7:"summer", 8:"summer"}
    df["season"] = df["pickup_month"].map(lambda m: season_map.get(m, "fall"))

    # Distancia estimada desde zone_pair_stats (el reemplazo del taximetro)
    zone_stats = model["zone_stats"]
    borough_stats = model["borough_stats"]
    zone_flags = model["zone_flags"]
    global_distance = model["global_distance"]
    df["trip_distance"] = [
        _estimate_distance(pu, do, zone_stats, borough_stats, zone_flags, global_distance)
        for pu, do in zip(df["PULocationID"], df["DOLocationID"])
    ]

    # Flags de zona desde zone_flags.json (Borough/service_zone reales de TLC),
    # no hardcodeadas - mismo criterio de load_zone_categories() en
    # 'nyctaxi - processing.py'
    def is_manhattan(zone_id):
        return bool(zone_flags.get(str(zone_id), {}).get("is_manhattan", False))
    
    def is_airport(zone_id):
        return bool(zone_flags.get(str(zone_id), {}).get("is_airport", False))
    
    df["pickup_manhattan"] = df["PULocationID"].map(is_manhattan).astype(int)
    df["dropoff_manhattan"] = df["DOLocationID"].map(is_manhattan).astype(int)
    df["manhattan_trip"] = (
        (df["pickup_manhattan"] ==1) | (df["dropoff_manhattan"] == 1)
    ).astype(int)
    df["is_airport_trip"] = (
        df["PULocationID"].map(is_airport) | df["DOLocationID"].map(is_airport)
    ).astype(int)

    # One-hot de season + alineado a las columnas exactas con las que entreno
    # el modelo (nombre y orden) - las toma del propio booster, no de una
    # lista hardcodeada aca, para no desincronizarse si el entrenamiento cambia
    df = pd.get_dummies(df, columns=["season"])
    feature_cols = model["booster"].feature_names
    df = df.reindex(columns=feature_cols, fill_value=0).astype(float)

    # Modelo XGBoost
    dmatrix = xgb.DMatrix(df)
    prediction = max(float(model["booster"].predict(dmatrix)[0]), MIN_FARE)
    return {"estimated_fare_total": prediction}

def output_fn(prediction, accept):
    return json.dumps(prediction), "application/json"
















