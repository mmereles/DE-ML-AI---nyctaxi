# NYC Yellow Taxi — Data Pipeline + ML + Serving

Pipeline de datos e-2-e de **NYC Yellow Taxi** (TLC Trip Record Data): ingesta mensual automatizada hacia S3, procesamiento y feature engineering en Databricks (Delta Lake + Unity Catalog), entrenamiento y gobierno del modelo de predicción de tarifa (MLOps con champion/challenger), y serving en producción vía AWS SageMaker. Infraestructura como código con Terraform (providers `aws` + `databricks` en el mismo state).

## Arquitectura

Diagrama completo y editable: [`nyctaxi_architecture.drawio`](nyctaxi_architecture.drawio) (abrir en [app.diagrams.net](https://app.diagrams.net) o con la extensión de draw.io de VS Code — GitHub también lo renderiza al ver el archivo). Distingue visualmente lo implementado (sólido) de lo pendiente (punteado): hoy eso es SNS/email de alertas, `ci.yml`, y la Lambda `quote_api` + API Gateway de la API pública.

Versión resumida en texto:

```
                         ┌────────────────────────┐
  EventBridge (cron,     │  Lambda: ingestion      │
  día 5 de cada mes) ───▶│  descarga el parquet    │
                         │  mensual desde TLC y lo  │
                         │  sube a S3               │
                         └───────────┬─────────────┘
                                     │
                                     ▼
                     s3://<bucket>/nyctaxi/raw/year=YYYY/month=MM/
                                     │
                         (EventBridge: Object Created)
                                     │
                                     ▼
                         ┌────────────────────────┐
                         │  Lambda:                │
                         │  processing_trigger     │
                         │  dispara el job de      │
                         │  Databricks vía API     │
                         └───────────┬─────────────┘
                                     │
                                     ▼
        ┌───────────────────────────────────────────────────────────┐
        │  Databricks Job (Terraform, provider databricks)            │
        │                                                               │
        │  process → ground_truth_eval → train → register_model       │
        │                                              │                │
        │                                              ▼                │
        │                                     promote_champion          │
        │                                                               │
        │  (zone_pair_stats / export_to_sagemaker: hoy manuales,        │
        │   ver "Estado del proyecto")                                  │
        └───────────────────────────┬───────────────────────────────┘
                                     │  export_to_sagemaker sube
                                     │  model.tar.gz a S3
                                     ▼
                    ┌─────────────────────────────────────┐
                    │  AWS SageMaker (Serverless Inference)  │
                    │  Model + EndpointConfig + Endpoint     │
                    │  Script Mode: sagemaker/inference.py    │
                    └───────────────────┬─────────────────┘
                                        │  invoke_endpoint (IAM)
                                        ▼
                    ┌─────────────────────────────────────┐
                    │  Lambda: quote_api (Fase 3, en curso)  │
                    │  API Gateway → POST /quote              │
                    └─────────────────────────────────────┘
```

Todas las Lambdas publican métricas custom en CloudWatch (`NYCTaxiDownload`, `NYCTaxiProcessing`) con alarmas asociadas ante errores de ejecución.

## Componentes

### `lambda/`

- **`ingestion.py`** — cron mensual (EventBridge, día 5 08:00 UTC). Calcula el mes anterior, evita redescargar si el archivo ya existe en S3 (`head_object`), descarga desde el CloudFront de TLC (`d37ci6vzurychx.cloudfront.net`) y sube a `s3://<bucket>/nyctaxi/raw/year=YYYY/month=MM/`. Reintenta hasta 3 veces, reporta duración/tamaño/throughput a CloudWatch.
- **`processing_trigger.py`** — disparado por EventBridge ante un objeto nuevo bajo `nyctaxi/raw/`. Extrae `year`/`month` del key, obtiene el token de Databricks desde Secrets Manager y llama `POST /api/2.1/jobs/run-now` para lanzar el job de Databricks.
- **`quote_apy.py`** *(Fase 3, en curso — pendiente de renombrar a `quote_api.py` y de `terraform/quote_api.tf`)* — Lambda detrás de API Gateway: valida el pedido público (`pickup_zone`, `dropoff_zone`, `pickup_datetime`, `passenger_count`), lo traduce al formato que espera `sagemaker/inference.py`, y llama al endpoint de SageMaker vía `sagemaker-runtime.invoke_endpoint` (IAM, sin token/secreto).
- **`requirements.txt`** — dependencias de `ingestion.py`/`processing_trigger.py` (`requests`; `boto3` viene con el runtime). No se versionan las instaladas — se generan en build/deploy apuntando al runtime real de Lambda (ver sección Despliegue).

### `notebooks/` (formato `.py` de Databricks — sincronizados vía `databricks_repo`/Git Folder)

| Notebook | Fase | Qué hace |
|---|---|---|
| `nyctaxi - processing` | Pipeline base | Bronze → silver → gold: limpieza con reglas de calidad, ~20 features de ingeniería (incluye flags de Manhattan/aeropuerto vía `taxi_zone_lookup` real, no listas hardcodeadas), escritura idempotente a Delta (`replaceWhere` por partición) en `yellow_taxi_features`. |
| `nyctaxi_historical_processing_loop` | 0.1 | Corre después de `backfill.py`: llama a `nyctaxi - processing` una vez por cada mes bajo `nyctaxi/historical/` (prefix separado a propósito, no dispara el trigger automático), para que el histórico entre a `yellow_taxi_features` antes de entrenar. |
| `nyctaxi_schema_audit` | 0.5 | Lee solo el *schema* (no los datos) de cada mes del backfill y arma una matriz columna × mes, para detectar columnas que TLC agregó a mitad de camino (`congestion_surcharge`, `airport_fee`) antes de asumir un dataset homogéneo. |
| `nyctaxi_eda` | 0.6 | EDA manual sobre `yellow_taxi_features`: filas por mes, distribución del target, outliers que `clean_data` no filtra, nulls en las features pre-viaje — el último chequeo antes de entrenar. |
| `nyctaxi_fare_prediction_training` | 1 | Target = `total_amount − tip_amount`; solo features pre-viaje (sin leakage); split temporal; baseline naïve por par origen-destino; Ridge de referencia; XGBoost; tuning opcional; tracking completo en MLflow. Corrida de referencia: XGBoost supera al naïve por 26.3% de MAE. |
| `nyctaxi_register_model` | 2.1 | Busca el run `xgboost_default` más reciente en el experimento de MLflow, y si supera el umbral de mejora sobre el naïve, lo registra en Unity Catalog Model Registry (`nyc_taxi_analytics.fare_prediction.fare_model`) con alias `champion`. |
| `nyctaxi_ground_truth_eval` | 2.4 | Corre justo después de `process`, antes de re-entrenar: compara las predicciones que el champion actual hizo el mes pasado contra las tarifas reales que recién llegaron — "ground truth gratis" sin infraestructura de logging extra. |
| `nyctaxi_promote_champion` | 2.3 | Compara el challenger recién registrado contra el champion actual por `test_mae`; si gana, mueve el alias `champion` a la nueva versión. |
| `nyctaxi_zone_pair_stats` | 3.1 | Precomputa la mediana histórica de distancia/peajes/tarifa por cada par `(PULocationID, DOLocationID)` — el reemplazo de `trip_distance` (que no existe antes del viaje) para servir cotizaciones. Pares con <10 viajes se marcan `reliable=false` para usar el fallback global. |
| `nyctaxi_export_to_sagemaker` | 3.4 | Empaqueta el champion (booster XGBoost en JSON) + `zone_pair_stats` + flags de zona + `sagemaker/inference.py` en un `model.tar.gz` con layout de Script Mode, y lo sube a S3 vía `dbutils.fs.cp` (el workspace es serverless, sin instance profile clásico). |

### `sagemaker/`

- **`inference.py`** — código Script Mode para el contenedor oficial `sagemaker-xgboost:1.7-1`. Implementa el contrato de 4 funciones (`model_fn`, `input_fn`, `predict_fn`, `output_fn`): reconstruye las features de entrenamiento a partir del input mínimo (`PULocationID`, `DOLocationID`, `pickup_datetime`, `passenger_count`), estimando distancia vía `zone_pair_stats` y calculando flags de Manhattan/aeropuerto desde el lookup real de TLC.
- **`requirements.txt`** — `pyarrow`, necesario para que `pd.read_parquet` funcione dentro del contenedor (no viene por defecto).

### `terraform/`

Infraestructura como código con dos providers en el mismo state (`aws` ~> 5.9, `databricks` ~> 1.55), región `us-east-1`:

- **`main.tf`** — bucket S3, ambas Lambdas de ingesta/trigger, IAM, reglas de EventBridge, alarmas CloudWatch (sin `alarm_actions` todavía — falta SNS), secret de Databricks token.
- **`databricks.tf`** — credencial Git + `databricks_repo` (sincroniza este repo al workspace) + `databricks_job.processing`, el job con las 5 tasks encadenadas (`process → ground_truth_eval → train → register_model → promote_champion`), con `environment` serverless (sin cluster spec, este workspace es Free Edition).
- **`sagemaker.tf`** — rol IAM de ejecución (solo lee el artifact + logs), `aws_sagemaker_model`, `aws_sagemaker_endpoint_configuration` (Serverless Inference, `create_before_destroy` para poder forzar recarga del artifact sin downtime), `aws_sagemaker_endpoint`. Desplegado y validado — ver "Estado del proyecto".
- **`backend.tf`** — state remoto en S3 (`nyctaxi-tfstate-98741313131`), lock nativo (`use_lockfile`, sin DynamoDB).
- **`variables.tf`** / **`output.tf`** — `databricks_host`, `git_repo_url`, `model_version`, `sagemaker_xgboost_image`; outputs de ARNs/nombres de recursos clave.

### `backfill.py` (raíz)

Descarga N meses hacia atrás desde el CloudFront de TLC y los sube a `nyctaxi/historical/` — prefix separado a propósito de `nyctaxi/raw/` para no disparar el processing automático. Se procesa manualmente, una vez, con `nyctaxi_historical_processing_loop`.

## Requisitos

- Cuenta AWS con permisos para los recursos de Terraform.
- Terraform >= 1.5.0 (subir a >= 1.10 si se usa el lock nativo de `backend.tf` con una versión anterior).
- Workspace de Databricks (Free Edition, serverless) con Unity Catalog habilitado.
- Personal Access Token de Databricks en Secrets Manager (`nyctaxi/databricks-token`), y el token del provider Terraform disponible como `DATABRICKS_TOKEN` al aplicar (no se hardcodea en ningún `.tf`).
- External Location de Unity Catalog cubriendo `s3://<bucket>/` (o al menos `/nyctaxi/` y `/sagemaker/`) para que los notebooks puedan leer/escribir en S3.

## Despliegue

```bash
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --abi cp313 \
  --only-binary=:all: \
  --target lambda/ \
  -r lambda/requirements.txt

cd terraform
terraform init
terraform plan
terraform apply
```

> Las dependencias instaladas en `lambda/` no se versionan en git (ver `.gitignore`) — se generan en cada build/deploy.

## Monitoreo

- **CloudWatch Logs**: `/aws/lambda/nyctaxi-data-ingestion`, `/aws/lambda/nyctaxi-processing-trigger`, `/aws/sagemaker/Endpoints/nyctaxi-fare-quote`.
- **Métricas custom**: namespace `NYCTaxiDownload` (`JobSuccess`, `JobFailure`, `JobSkipped`, `DownloadDuration`, `UploadDuration`, `FileSize`, `FileSizeMB`, `DownloadThroughput`) y `NYCTaxiProcessing` (`JobTriggered`).
- **Alarmas**: `nyctaxi-ingestion-lambda-errors`, `nyctaxi-processing-lambda-errors` — se disparan ante errores no controlados, pero **sin destino configurado todavía** (ver abajo).

## Estado del proyecto

**Hecho y validado:**
- [x] Ingesta mensual automatizada + trigger event-driven del processing.
- [x] Processing con feature engineering (~20 features) a Delta Lake / Unity Catalog.
- [x] Backfill histórico cargado y auditado (schema + EDA).
- [x] Fase 1 — modelo XGBoost, supera al baseline naïve por 26.3% de MAE.
- [x] Fase 2 — registry, retraining, ground truth loop y promoción champion/challenger, corriendo como job real de Databricks.
- [x] Fase 3 (serving) — modelo desplegado en SageMaker (Serverless Inference), validado con datos reales tras corregir el cálculo de `zone_pair_stats`.
- [x] Backend remoto de Terraform (S3 + lock nativo).

**Pendiente:**
- [ ] `alarm_actions` de CloudWatch sin destino — falta SNS + email.
- [ ] `lambda/quote_api.py` + `terraform/quote_api.tf` — la API pública de cotización (Lambda + API Gateway) todavía no está desplegada.
- [ ] Tests automatizados (0% cobertura hoy) y CI (`ci.yml`) sin aplicar.
- [ ] Lakehouse Monitoring sobre `yellow_taxi_features` (drift de features).
- [ ] `zone_pair_stats` / `export_to_sagemaker` siguen siendo pasos manuales — automatizarlos como tasks del job requiere primero resolver el versionado del artifact en S3 (hoy pisa la misma ruta si el champion no cambió, y el endpoint no recarga solo).
- [ ] Fase 4 (forecasting de demanda, asistente de lenguaje natural) — sin empezar, a propósito: el roadmap del proyecto prioriza cerrar los puntos anteriores antes de sumar más superficie.
