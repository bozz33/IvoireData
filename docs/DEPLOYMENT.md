# Déploiement

## Local
```bash
pip install -e '.[dev,training]'
ivoiredata sources --public
ivoiredata sync civ_datagouv_catalog
uvicorn ivoiredata.api:app --reload
```
Par défaut les charges dlt sont écrites dans `file://data_lake`, hors Git.

## Docker + MinIO
```bash
docker compose up --build
```
API : `http://localhost:8000`, MinIO S3 : `http://localhost:9000`, console : `http://localhost:9001`.
Les identifiants Docker sont uniquement pour le développement local.

## S3/R2/MinIO externe
Renseigner `IVOIREDATA_BUCKET_URL`, `IVOIREDATA_S3_ENDPOINT`, `IVOIREDATA_S3_ACCESS_KEY`, `IVOIREDATA_S3_SECRET_KEY`.
