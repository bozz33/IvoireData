from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    bucket_url: str = "file://data_lake"
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region_name: str | None = None
    dataset_name: str = "ivoiredata"
    pipeline_name: str = "ivoiredata_engine"
    state_dir: Path = Path(".ivoiredata/state")
    registry_path: Path = Path("registry/sources.csv")
    runtime_config_path: Path = Path("configs/runtime_sources.json")
    user_agent: str = "IvoireData/0.4 (+https://github.com/bozz33/IvoireData)"

    @classmethod
    def from_env(cls) -> "Settings":
        bucket = os.getenv("IVOIREDATA_BUCKET_URL") or "file://data_lake"
        return cls(
            bucket_url=bucket,
            endpoint_url=os.getenv("IVOIREDATA_S3_ENDPOINT") or None,
            access_key_id=os.getenv("IVOIREDATA_S3_ACCESS_KEY") or None,
            secret_access_key=os.getenv("IVOIREDATA_S3_SECRET_KEY") or None,
            region_name=os.getenv("IVOIREDATA_S3_REGION") or None,
            dataset_name=os.getenv("IVOIREDATA_DATASET_NAME") or "ivoiredata",
            pipeline_name=os.getenv("IVOIREDATA_PIPELINE_NAME") or "ivoiredata_engine",
            state_dir=Path(os.getenv("IVOIREDATA_STATE_DIR") or ".ivoiredata/state"),
            registry_path=Path(os.getenv("IVOIREDATA_REGISTRY") or "registry/sources.csv"),
            runtime_config_path=Path(os.getenv("IVOIREDATA_RUNTIME_CONFIG") or "configs/runtime_sources.json"),
        )

    def configure_dlt_env(self) -> None:
        os.environ["DESTINATION__FILESYSTEM__BUCKET_URL"] = self.bucket_url
        if self.endpoint_url:
            os.environ["DESTINATION__FILESYSTEM__CREDENTIALS__ENDPOINT_URL"] = self.endpoint_url
        if self.access_key_id:
            os.environ["DESTINATION__FILESYSTEM__CREDENTIALS__AWS_ACCESS_KEY_ID"] = self.access_key_id
        if self.secret_access_key:
            os.environ["DESTINATION__FILESYSTEM__CREDENTIALS__AWS_SECRET_ACCESS_KEY"] = self.secret_access_key
        if self.region_name:
            os.environ["DESTINATION__FILESYSTEM__CREDENTIALS__REGION_NAME"] = self.region_name
