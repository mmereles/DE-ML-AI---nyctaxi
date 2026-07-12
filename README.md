# NYC Yellow Taxi — Data Pipeline

Ingesta automatizada de los datasets mensuales de **NYC Yellow Taxi** (TLC Trip Record Data) hacia S3, con un disparador hacia un job de **Databricks** para el procesamiento posterior. Infraestructura gestionada con Terraform sobre AWS.

## Arquitectura

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
                         │  dispara un job en      │
                         │  Databricks vía API     │
                         └───────────┬─────────────┘
                                     │
                                     ▼
                          Databricks Job (pendiente)
```

Ambas Lambdas publican métricas custom en CloudWatch (`NYCTaxiDownload`, `NYCTaxiProcessing`) y tienen una alarma asociada que se dispara ante errores de ejecución.

## Componentes

### `lambda/ingestion.py`
Se ejecuta mensualmente (cron EventBridge, día 5 a las 08:00 UTC). Calcula el mes anterior, verifica si el archivo ya existe en S3 (`head_object`) y, si no, lo descarga desde el CloudFront de la TLC (`d37ci6vzurychx.cloudfront.net`) y lo sube a `s3://<bucket>/nyctaxi/raw/year=YYYY/month=MM/`. Reintenta hasta 3 veces la descarga y reporta métricas de duración, tamaño y throughput a CloudWatch.

### `lambda/processing_trigger.py`
Se dispara por EventBridge cuando llega un objeto nuevo bajo `nyctaxi/raw/` en el bucket. Extrae `year`/`month` del key de S3, obtiene el token de Databricks desde Secrets Manager y llama a la API `POST /api/2.1/jobs/run-now` para lanzar el job de procesamiento, pasando `source_bucket`, `source_key`, `source_year` y `source_month` como `notebook_params`.

### `lambda/requirements.txt`
Dependencias Python de ambas funciones (`requests`; `boto3` ya viene incluido en el runtime de Lambda). Antes de empaquetar con Terraform hay que instalarlas apuntando al runtime real de Lambda:

```bash
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --abi cp313 \
  --only-binary=:all: \
  --target lambda/ \
  -r lambda/requirements.txt
```

> Estas dependencias no se versionan en git (ver `.gitignore`) — se generan en build/deploy, no se commitean binarios.

### `terraform/`
Infraestructura como código (AWS provider ~> 5.9, región `us-east-1`):

- `aws_s3_bucket.nyc_taxi_bucket` — bucket de datos crudos, con notificaciones EventBridge habilitadas.
- `aws_lambda_function.nyctaxi_ingestion` / `nyctaxi_processing_trigger` — ambas Lambdas, runtime `python3.13`.
- `aws_iam_role` + `aws_iam_role_policy` — permisos mínimos por función (S3, CloudWatch Logs/Metrics, Secrets Manager).
- `aws_cloudwatch_event_rule` — regla mensual (`monthly_trigger`) y regla de creación de objetos S3 (`s3_taxi_data_rule`).
- `aws_cloudwatch_metric_alarm` — alarmas de errores para ambas Lambdas (sin `alarm_actions` configuradas todavía — falta enlazar un SNS topic).
- `aws_secretsmanager_secret.databricks_token` — el token real de Databricks se rota fuera de Terraform (`ignore_changes`).
- `data.archive_file` — empaqueta `lambda/` en el zip de cada función, excluyendo el handler de la otra y `venv/`.

Variables (`variables.tf`): `databricks_host`, `databricks_job_id` — configurables por `.tfvars` para no hardcodear valores por ambiente.

## Requisitos

- Cuenta AWS con permisos para crear los recursos anteriores.
- Terraform >= 1.5.0.
- Un workspace de Databricks con un job ya creado (el notebook de procesamiento aún no forma parte de este repo — está en desarrollo).
- Un Personal Access Token de Databricks cargado manualmente en el secreto `nyctaxi/databricks-token` tras el primer `apply` (el valor en el código es solo un placeholder).

## Despliegue

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

Después del primer `apply`, actualizar el secreto con el token real:

```bash
aws secretsmanager put-secret-value \
  --secret-id nyctaxi/databricks-token \
  --secret-string '{"token":"<TOKEN_REAL>"}'
```

## Monitoreo

- **CloudWatch Logs**: `/aws/lambda/nyctaxi-data-ingestion`, `/aws/lambda/nyctaxi-processing-trigger`.
- **Métricas custom**: namespace `NYCTaxiDownload` (`JobSuccess`, `JobFailure`, `JobSkipped`, `DownloadDuration`, `UploadDuration`, `FileSize`, `FileSizeMB`, `DownloadThroughput`) y `NYCTaxiProcessing` (`JobTriggered`).
- **Alarmas**: `nyctaxi-ingestion-lambda-errors`, `nyctaxi-processing-lambda-errors` — se disparan ante cualquier error no controlado en la Lambda correspondiente.

## Estado del proyecto / próximos pasos

- [x] Ingesta mensual automatizada a S3.
- [x] Disparo automático de un job de Databricks al llegar datos nuevos.
- [ ] Notebook de Databricks: limpieza de datos crudos (bronze → silver) y feature engineering (gold), con tracking en MLflow — en desarrollo.
- [ ] Definición del target de Machine Learning (tarifa, duración de viaje, propina, etc.) y entrenamiento del modelo.
- [ ] `alarm_actions` de CloudWatch sin destino (falta SNS/notificación).
- [ ] Backend remoto de Terraform (actualmente el `tfstate` es local).
