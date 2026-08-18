# NYC Yellow Taxi — Data Pipeline + ML + MLOps + Serving + AI Agent

End-to-end project on **NYC Yellow Taxi** (TLC Trip Record Data), start to finish: automated monthly ingestion into S3, processing and feature engineering in Databricks (Delta Lake + Unity Catalog), fare-prediction model training and governance (MLOps with champion/challenger), production serving via AWS SageMaker, demand forecasting, and a conversational agent (BYOK) with a public web frontend. Infrastructure as code with Terraform (`aws` + `databricks` providers in the same state).

**Live demo:** [mmereles.github.io/DE-ML-AI---nyctaxi](https://mmereles.github.io/DE-ML-AI---nyctaxi/) — instant, free fare quotes, plus a chat where each visitor brings their own OpenAI key.

## Architecture

All 5 phases of the roadmap, from ingestion to the AI layer — all implemented and deployed. Real AWS/Databricks icons, grouped by service (editable source: [`nyctaxi_architecture.drawio`](nyctaxi_architecture.drawio), open it in [app.diagrams.net](https://app.diagrams.net)):

![Architecture diagram with AWS and Databricks icons](nyctaxi_architecture.svg)

**Phase 0** (ingestion + prerequisites) → **Phase 1-2** (processing, ML, MLOps in Databricks) → **Phase 3** (serving on SageMaker + public API) → **Phase 4.1** (demand forecasting) → **Phase 4.2** (BYOK conversational agent + web frontend).

All Lambdas publish custom metrics to CloudWatch (`NYCTaxiDownload`, `NYCTaxiProcessing`) with alarms tied to execution errors.

## Components

### `lambda/`

- **`ingestion.py`** — monthly cron (EventBridge, day 5, 08:00 UTC). Computes the previous month, skips re-downloading if the file already exists in S3 (`head_object`), downloads from TLC's CloudFront (`d37ci6vzurychx.cloudfront.net`), and uploads to `s3://<bucket>/nyctaxi/raw/year=YYYY/month=MM/`. Retries up to 3 times, reports duration/size/throughput to CloudWatch.
- **`processing_trigger.py`** — triggered by EventBridge on a new object under `nyctaxi/raw/`. Extracts `year`/`month` from the key, fetches the Databricks token from Secrets Manager, and calls `POST /api/2.1/jobs/run-now` to kick off the Databricks job.
- **`quote_api.py`** (Phase 3) — Lambda behind API Gateway: validates the public request (`pickup_zone`, `dropoff_zone`, `pickup_datetime`, `passenger_count`), translates it into the format `sagemaker/inference.py` expects, and calls the SageMaker endpoint via `sagemaker-runtime.invoke_endpoint` (IAM, no token/secret needed). Free and keyless for visitors — used both by the `agent/web/` quote form and the agent's `get_fare_quote` tool.
- **`requirements.txt`** — dependencies for `ingestion.py`/`processing_trigger.py`/`quote_api.py` (`requests`; `boto3` ships with the runtime). Installed dependencies aren't versioned — they're generated at build/deploy time targeting the actual Lambda runtime (see Deployment section).

### `agent/` (Phase 4.2 — BYOK conversational agent)

- **`backend/ask_agent.py`** — its own Lambda, behind a **Function URL** (not API Gateway: API Gateway's integration timeout has a hard ~29s ceiling, not enough for a multi-turn tool-calling loop; a Function URL is limited only by the Lambda's own timeout, up to 900s). Receives `{question, openai_api_key}`, runs a tool-calling loop with OpenAI (`gpt-4o`) over two tools: `run_sql` (SELECT-only against Unity Catalog, catalog/schema fixed on the connection) and `get_fare_quote` (calls `quote_api`, never makes up a fare). The OpenAI key belongs to the visitor — it travels in the request, is never stored or logged; Databricks credentials stay only in server-side environment variables.
- **`web/`** — React + Vite frontend: a direct quote form (no key, no cost) and a collapsible chat against the agent. Deployed to GitHub Pages via GitHub Actions (`deploy-web.yml`) on every push touching this folder; the two backend URLs are injected at build time as repo variables (`ASK_AGENT_URL`, `QUOTE_API_URL`), never hardcoded.

### `notebooks/` (mostly Databricks `.py` format, synced via `databricks_repo`/Git Folder — `schema_audit` and `eda` are still `.ipynb`)

| Notebook | Phase | What it does |
|---|---|---|
| `nyctaxi - processing` | Base pipeline | Bronze → silver → gold: data-quality cleaning rules, ~20 engineered features (Manhattan/airport flags via `taxi_zone_lookup`, which this notebook also persists as a real Unity Catalog table), idempotent Delta writes (`replaceWhere` per partition) into `yellow_taxi_features`. |
| `nyctaxi_historical_processing_loop` | 0.1 | Runs after `backfill.py`: calls `nyctaxi - processing` once per month under `nyctaxi/historical/` (a separate prefix on purpose, doesn't trigger the automatic trigger), so the historical backfill lands in `yellow_taxi_features` before training. |
| `nyctaxi_schema_audit` | 0.5 | Reads only the *schema* (not the data) of each backfilled month and builds a column × month matrix, to catch columns TLC added midway through (`congestion_surcharge`, `airport_fee`) before assuming a homogeneous dataset. |
| `nyctaxi_eda` | 0.6 | Manual EDA over `yellow_taxi_features`: rows per month, target distribution, outliers `clean_data` doesn't filter, nulls in pre-trip features — the last check before training. |
| `nyctaxi_fare_prediction_training` | 1 | Target = `total_amount − tip_amount`; pre-trip features only (no leakage); temporal split; naive per-origin-destination baseline; reference Ridge; XGBoost; optional tuning; full MLflow tracking. Reference run: XGBoost beats the naive baseline by 26.3% MAE. |
| `nyctaxi_register_model` | 2.1 | Finds the most recent `xgboost_default` run in the MLflow experiment, and if it clears the improvement threshold over the naive baseline, registers it in the Unity Catalog Model Registry (`nyc_taxi_analytics.fare_prediction.fare_model`) with the `champion` alias. |
| `nyctaxi_ground_truth_eval` | 2.4 | Runs right after `process`, before retraining: compares last month's predictions from the current champion against the real fares that just arrived — "free ground truth" with no extra logging infrastructure. |
| `nyctaxi_promote_champion` | 2.3 | Compares the freshly registered challenger against the current champion on `test_mae`; if it wins, moves the `champion` alias to the new version. |
| `nyctaxi_zone_pair_stats` | 3.1 / 4.2 | Precomputes the historical median distance/tolls/fare for each `(PULocationID, DOLocationID)` pair — the replacement for `trip_distance` (which doesn't exist before the trip) when serving quotes. Pairs with <10 trips are flagged `reliable=false`; also builds an intermediate fallback by *borough* pair (much more representative than the global fallback for peripheral zone pairs with little history — the global fallback was producing negative fares in those cases). |
| `nyctaxi_export_to_sagemaker` | 3.4 | Packages the champion (XGBoost booster as JSON) + `zone_pair_stats` (exact pair, borough-level, and global) + zone flags + `sagemaker/inference.py` into a `model.tar.gz` with Script Mode layout, and uploads it to S3 via `dbutils.fs.cp` (the workspace is serverless, no classic instance profile). |
| `nyctaxi_demand_forecasting` | 4.1 | LightGBM over `yellow_taxi_features` aggregated by zone/hour; generates `demand_forecast` (next 7 days) by reusing the feature pattern rather than true recursive forecasting — documented as such on purpose, no registration in the Unity Catalog Model Registry (not served as an endpoint). |

### `sagemaker/`

- **`inference.py`** — Script Mode code for the official `sagemaker-xgboost:1.7-1` container. Implements the 4-function contract (`model_fn`, `input_fn`, `predict_fn`, `output_fn`): reconstructs training features from the minimal input (`PULocationID`, `DOLocationID`, `pickup_datetime`, `passenger_count`), estimating distance through a fallback chain (exact reliable pair → borough-level median → global median) and computing Manhattan/airport flags from the real TLC lookup. The final prediction is clamped to a minimum fare — no tree-based regressor guarantees non-negative output, and no real taxi fare is negative.
- **`requirements.txt`** — `pyarrow`, needed for `pd.read_parquet` to work inside the container (not included by default).

### `terraform/`

Infrastructure as code with two providers in the same state (`aws` ~> 5.9, `databricks` ~> 1.55), `us-east-1` region:

- **`main.tf`** — S3 bucket, ingestion/trigger Lambdas, IAM, EventBridge rules, CloudWatch alarms (no `alarm_actions` yet — SNS still missing), Databricks token secret.
- **`databricks.tf`** — Git credential + `databricks_repo` (syncs this repo to the workspace) + `databricks_job.processing`, the job with 5 chained tasks (`process → ground_truth_eval → train → register_model → promote_champion`), with a serverless `environment` (no cluster spec, this workspace is Free Edition).
- **`sagemaker.tf`** — execution IAM role (only reads the artifact + logs), `aws_sagemaker_model` (the name includes the real ETag of the S3 artifact, not a fixed value — SageMaker Models are immutable, so any redeploy needs a new name to be able to replace the resource without downtime), `aws_sagemaker_endpoint_configuration`/`aws_sagemaker_endpoint` (Serverless Inference, `create_before_destroy`).
- **`quote_api.tf`** (Phase 3) — API Gateway HTTP API (`POST /quote`) + Lambda integration + `cors_configuration` (without this, any call from the browser fails at the `OPTIONS` preflight, even though `curl` works fine).
- **`ask_agent.tf`** (Phase 4.2) — Lambda + Function URL (`authorization_type = "NONE"`, public on purpose — the visitor brings the OpenAI key) + a scoped IAM role (only logs + reading the Databricks secret) + S3-based deploy (the zip weighs ~60MB due to `databricks-sql-connector`'s dependencies, above Lambda's direct-upload limit).
- **`backend.tf`** — remote state in S3 (`nyctaxi-tfstate-98741313131`), native locking (`use_lockfile`, no DynamoDB).
- **`variables.tf`** / **`output.tf`** — `databricks_host`, `git_repo_url`, `model_version`, `sagemaker_xgboost_image`, `databricks_sql_warehouse_id`; outputs for ARNs/names/URLs of key resources.

### `backfill.py` (root)

Downloads N months back from TLC's CloudFront and uploads them to `nyctaxi/historical/` — a prefix kept separate from `nyctaxi/raw/` on purpose, to avoid triggering automatic processing. Processed manually, once, with `nyctaxi_historical_processing_loop`.

### `tests/`

35 tests with `pytest` (`monkeypatch`/`MagicMock`, no credentials or real calls): the 3 Lambdas in `lambda/` and `sagemaker/inference.py` (including the borough-level distance fallback and the minimum-fare clamp).

## Requirements

- AWS account with permissions for the Terraform resources.
- Terraform >= 1.5.0 (bump to >= 1.10 if using `backend.tf`'s native lock on an earlier version).
- Databricks workspace (Free Edition, serverless) with Unity Catalog enabled.
- Databricks Personal Access Token in Secrets Manager (`nyctaxi/databricks-token`), and the Terraform provider token available as `DATABRICKS_TOKEN` when applying (never hardcoded in any `.tf` file).
- Unity Catalog External Location covering `s3://<bucket>/` (or at least `/nyctaxi/` and `/sagemaker/`) so notebooks can read/write to S3.
- For the frontend (`agent/web/`): Node 20+, and on GitHub — Settings → Pages → Source = "GitHub Actions", plus the repo variables `ASK_AGENT_URL` and `QUOTE_API_URL` (outputs of `terraform apply`, not secret — visitors see them the same way in the browser).
- For the agent chat: each visitor needs their own OpenAI API key (BYOK) — the project owner pays nothing and exposes no account, no matter how many people try the demo.

## Deployment

```bash
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --abi cp313 \
  --only-binary=:all: \
  --target lambda/ \
  -r lambda/requirements.txt

pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --abi cp313 \
  --only-binary=:all: \
  --target agent/backend/ \
  -r agent/backend/requirements.txt

cd terraform
terraform init
terraform plan
terraform apply
```

> Dependencies installed into `lambda/` and `agent/backend/` aren't versioned in git (see `.gitignore`) — they're generated on every build/deploy.

The frontend deploys itself: any push to `main` touching `agent/web/**` triggers `.github/workflows/deploy-web.yml`, which builds with Vite and publishes to GitHub Pages. `.github/workflows/terraform.yml` runs `plan` on every PR touching `terraform/`/`lambda/` and `apply` on every push to `main` (OIDC role, no access keys stored in GitHub).

## Monitoring

- **CloudWatch Logs**: `/aws/lambda/nyctaxi-data-ingestion`, `/aws/lambda/nyctaxi-processing-trigger`, `/aws/lambda/nyctaxi-quote-api`, `/aws/lambda/nyctaxi-ask-agent`, `/aws/sagemaker/Endpoints/nyctaxi-fare-quote`.
- **Custom metrics**: namespace `NYCTaxiDownload` (`JobSuccess`, `JobFailure`, `JobSkipped`, `DownloadDuration`, `UploadDuration`, `FileSize`, `FileSizeMB`, `DownloadThroughput`) and `NYCTaxiProcessing` (`JobTriggered`).
- **Alarms**: `nyctaxi-ingestion-lambda-errors`, `nyctaxi-processing-lambda-errors` — fire on unhandled errors, but **with no destination configured yet** (see below).

## Project status

**Done and validated — all 5 phases complete and deployed:**
- [x] Automated monthly ingestion + event-driven processing trigger.
- [x] Processing with feature engineering (~20 features) into Delta Lake / Unity Catalog.
- [x] Historical backfill loaded and audited (schema + EDA).
- [x] Phase 1 — XGBoost model, beats the naive baseline by 26.3% MAE.
- [x] Phase 2 — registry, retraining, ground truth loop, and champion/challenger promotion, running as a real Databricks job.
- [x] Phase 3 — public quote API (`quote_api` + API Gateway) on top of the model served on SageMaker (Serverless Inference).
- [x] Phase 4.1 — demand forecasting with LightGBM (`demand_forecast`).
- [x] Phase 4.2 — BYOK conversational agent (Lambda Function URL, OpenAI tool-calling over `run_sql`/`get_fare_quote`) + React frontend deployed on GitHub Pages.
- [x] 35-test suite (`pytest`) covering the Lambdas and `inference.py`.
- [x] CI/CD: `terraform.yml` (plan/apply via OIDC) + `deploy-web.yml` (automatic frontend deploy).
- [x] Remote Terraform backend (S3 + native lock).

**Pending / known technical debt:**
- [ ] CloudWatch `alarm_actions` with no destination — SNS + email still missing.
- [ ] Lakehouse Monitoring over `yellow_taxi_features` (feature drift).
- [ ] `zone_pair_stats` / `export_to_sagemaker` are still manual steps in Databricks (not part of the automated job).
- [ ] An `aws_s3_bucket_notification` not declared in Terraform — Unity Catalog creates it automatically on the bucket's External Location; not touched in a root `apply` to avoid accidentally deleting it.
