import json
from unittest.mock import MagicMock

import pandas as pd

import inference

# Mismas 17 features con las que entrenó nyctaxi_fare_prediction_training.py
# (PRETRIP_FEATURES + season one-hot). El orden real no importa para el
# test - .reindex(columns=feature_cols) lo resuelve - pero el CONJUNTO sí:
# si predict_fn produjera una columna con un nombre distinto (como pasó hoy
# con "manhattan_trup"), reindex la descartaría en silencio y este test
# fallaría al no encontrarla en el DataFrame capturado.
FEATURE_NAMES = [
    "PULocationID", "DOLocationID", "trip_distance", "passenger_count",
    "pickup_hour", "pickup_day_of_week", "pickup_month", "is_weekend",
    "is_rush_hour", "season_fall", "season_spring", "season_summer",
    "season_winter", "pickup_manhattan", "dropoff_manhattan",
    "manhattan_trip", "is_airport_trip",
]


def _fake_model(monkeypatch, predicted_fare=42.0):
    """Arma un dict `model` como el que devuelve model_fn, sin necesitar
    archivos reales en disco ni un booster de XGBoost entrenado - solo lo
    que predict_fn necesita leer."""
    zone_stats = pd.DataFrame(
        [{
            "median_trip_distance": 17.61,
            "median_tolls": 6.94,
            "trip_count": 116061,
            "reliable": True,
        }],
        index=pd.MultiIndex.from_tuples([(132, 230)], names=["PULocationID", "DOLocationID"]),
    )

    zone_flags = {
        "132": {"is_manhattan": False, "is_airport": True, "borough": "Queens"},    # JFK
        "230": {"is_manhattan": True, "is_airport": False, "borough": "Manhattan"},  # Manhattan
    }

    borough_stats = pd.DataFrame(
        [{"median_trip_distance": 8.4, "median_tolls": 1.2, "trip_count": 5000}],
        index=pd.MultiIndex.from_tuples([("Queens", "Manhattan")], names=["PUBorough", "DOBorough"]),
    )

    booster = MagicMock()
    booster.feature_names = FEATURE_NAMES
    booster.predict.return_value = [predicted_fare]

    captured = {}

    def fake_dmatrix(df):
        captured["df"] = df.copy()
        return MagicMock()

    monkeypatch.setattr(inference.xgb, "DMatrix", fake_dmatrix)

    model = {
        "booster": booster,
        "zone_stats": zone_stats,
        "borough_stats": borough_stats,
        "global_distance": 1.7,
        "zone_flags": zone_flags,
    }
    return model, captured


# ---------------------------------------------------------------------------
# input_fn
# ---------------------------------------------------------------------------

def test_input_fn_parses_json_body():
    body = json.dumps({"PULocationID": 132, "DOLocationID": 230})
    assert inference.input_fn(body, "application/json") == {"PULocationID": 132, "DOLocationID": 230}


def test_input_fn_accepts_content_type_with_charset():
    body = json.dumps({"a": 1})
    assert inference.input_fn(body, "application/json; charset=utf-8") == {"a": 1}


def test_input_fn_rejects_unsupported_content_type():
    try:
        inference.input_fn("not json", "text/csv")
        assert False, "debería haber lanzado ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# output_fn
# ---------------------------------------------------------------------------

def test_output_fn_serializes_prediction_to_json():
    body, content_type = inference.output_fn({"estimated_fare_total": 42.0}, "application/json")
    assert content_type == "application/json"
    assert json.loads(body) == {"estimated_fare_total": 42.0}


# ---------------------------------------------------------------------------
# _estimate_distance
# ---------------------------------------------------------------------------

EMPTY_BOROUGH_STATS = pd.DataFrame(
    columns=["median_trip_distance"],
    index=pd.MultiIndex.from_tuples([], names=["PUBorough", "DOBorough"]),
)


def test_estimate_distance_uses_pair_median_when_reliable():
    zone_stats = pd.DataFrame(
        [{"median_trip_distance": 17.61, "reliable": True}],
        index=pd.MultiIndex.from_tuples([(132, 230)], names=["PULocationID", "DOLocationID"]),
    )
    distance = inference._estimate_distance(
        132, 230, zone_stats, EMPTY_BOROUGH_STATS, zone_flags={}, global_distance=1.7
    )
    assert distance == 17.61


def test_estimate_distance_falls_back_to_borough_when_pair_unreliable():
    zone_stats = pd.DataFrame(
        [{"median_trip_distance": 0.2, "reliable": False}],
        index=pd.MultiIndex.from_tuples([(1, 2)], names=["PULocationID", "DOLocationID"]),
    )
    borough_stats = pd.DataFrame(
        [{"median_trip_distance": 9.3}],
        index=pd.MultiIndex.from_tuples([("Bronx", "Brooklyn")], names=["PUBorough", "DOBorough"]),
    )
    zone_flags = {
        "1": {"borough": "Bronx"},
        "2": {"borough": "Brooklyn"},
    }
    distance = inference._estimate_distance(
        1, 2, zone_stats, borough_stats, zone_flags, global_distance=1.7
    )
    assert distance == 9.3


def test_estimate_distance_falls_back_to_global_when_pair_and_borough_missing():
    zone_stats = pd.DataFrame(
        [{"median_trip_distance": 17.61, "reliable": True}],
        index=pd.MultiIndex.from_tuples([(132, 230)], names=["PULocationID", "DOLocationID"]),
    )
    distance = inference._estimate_distance(
        999, 998, zone_stats, EMPTY_BOROUGH_STATS, zone_flags={}, global_distance=1.7
    )
    assert distance == 1.7


# ---------------------------------------------------------------------------
# predict_fn - el corazon de la logica, donde vivia el bug de manhattan_trip
# ---------------------------------------------------------------------------

def test_predict_fn_builds_correct_features_for_airport_to_manhattan_trip(monkeypatch):
    model, captured = _fake_model(monkeypatch, predicted_fare=42.0)

    input_data = {
        "PULocationID": 132,   # JFK - no es Manhattan, es aeropuerto
        "DOLocationID": 230,   # Manhattan - no es aeropuerto
        "pickup_datetime": "2026-08-14T18:00:00",  # viernes 18hs
        "passenger_count": 1,
    }

    result = inference.predict_fn(input_data, model)

    df = captured["df"]
    # Esta es exactamente la aserción que hoy hubiera fallado con el bug
    # "manhattan_trup": la columna no existiria (KeyError) o, si el nombre
    # no estuviera en FEATURE_NAMES, reindex la habria descartado y
    # rellenado con 0 en vez del valor real.
    assert df["pickup_manhattan"].iloc[0] == 0
    assert df["dropoff_manhattan"].iloc[0] == 1
    assert df["manhattan_trip"].iloc[0] == 1
    assert df["is_airport_trip"].iloc[0] == 1

    # Distancia: viene del par confiable (132, 230), no del fallback global
    assert df["trip_distance"].iloc[0] == 17.61

    # Features de tiempo: 2026-08-14 es viernes, 18hs -> hora pico, no finde
    assert df["pickup_hour"].iloc[0] == 18
    assert df["is_weekend"].iloc[0] == 0
    assert df["is_rush_hour"].iloc[0] == 1
    assert df["season_summer"].iloc[0] == 1

    # El resultado final viene de booster.predict(), no se inventa
    assert result == {"estimated_fare_total": 42.0}


def test_predict_fn_falls_back_to_global_distance_for_unknown_pair(monkeypatch):
    model, captured = _fake_model(monkeypatch)

    input_data = {
        "PULocationID": 1,
        "DOLocationID": 2,
        "pickup_datetime": "2026-08-16T10:00:00",  # domingo
        "passenger_count": 2,
    }

    inference.predict_fn(input_data, model)

    df = captured["df"]
    assert df["trip_distance"].iloc[0] == 1.7  # global_distance del fixture
    assert df["pickup_manhattan"].iloc[0] == 0
    assert df["dropoff_manhattan"].iloc[0] == 0
    assert df["manhattan_trip"].iloc[0] == 0
    assert df["is_airport_trip"].iloc[0] == 0


def test_predict_fn_clamps_negative_prediction_to_min_fare(monkeypatch):
    # Este es el bug real que se vio en produccion: para pares de zonas poco
    # vistos, el booster puede extrapolar a negativo. Ninguna tarifa de taxi
    # real es negativa - predict_fn tiene que pisarlo a MIN_FARE, nunca
    # devolver la salida cruda del regressor.
    model, _ = _fake_model(monkeypatch, predicted_fare=-7.84)

    input_data = {
        "PULocationID": 132,
        "DOLocationID": 230,
        "pickup_datetime": "2026-08-15T18:00:00",
        "passenger_count": 1,
    }

    result = inference.predict_fn(input_data, model)
    assert result == {"estimated_fare_total": inference.MIN_FARE}
