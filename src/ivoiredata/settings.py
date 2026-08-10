from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data_lake")
    dataset_name: str = "ivoiredata"
    pipeline_name: str = "ivoiredata_engine"
    state_dir: Path = Path(".ivoiredata/state")
    registry_path: Path = Path("registry/sources.csv")
    ci_gold_registry_path: Path = Path("registry/ci_gold_completeness.csv")
    runtime_config_path: Path = Path("configs/runtime_sources.json")
    ci_gold_runtime_path: Path = Path("configs/ci_gold_sources.json")
    ci_coverage_path: Path = Path("configs/ci_coverage.json")
    user_agent: str = "IvoireData/0.8.1 (+https://github.com/bozz33/IvoireData)"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("IVOIREDATA_DATA_DIR") or "data_lake"),
            dataset_name=os.getenv("IVOIREDATA_DATASET_NAME") or "ivoiredata",
            pipeline_name=os.getenv("IVOIREDATA_PIPELINE_NAME") or "ivoiredata_engine",
            state_dir=Path(os.getenv("IVOIREDATA_STATE_DIR") or ".ivoiredata/state"),
            registry_path=Path(os.getenv("IVOIREDATA_REGISTRY") or "registry/sources.csv"),
            ci_gold_registry_path=Path(os.getenv("IVOIREDATA_CI_GOLD_REGISTRY") or "registry/ci_gold_completeness.csv"),
            runtime_config_path=Path(os.getenv("IVOIREDATA_RUNTIME_CONFIG") or "configs/runtime_sources.json"),
            ci_gold_runtime_path=Path(os.getenv("IVOIREDATA_CI_GOLD_RUNTIME") or "configs/ci_gold_sources.json"),
            ci_coverage_path=Path(os.getenv("IVOIREDATA_CI_COVERAGE") or "configs/ci_coverage.json"),
        )

    @property
    def runtime_overrides_path(self) -> Path:
        configured = os.getenv("IVOIREDATA_RUNTIME_OVERRIDES")
        return Path(configured) if configured else self.state_dir / "runtime_overrides.json"

    @property
    def qualification_path(self) -> Path:
        configured = os.getenv("IVOIREDATA_CI_QUALIFICATION")
        return Path(configured) if configured else self.state_dir / "ci_gold_qualification.json"

    @property
    def registry_overlay_paths(self) -> list[Path]:
        return [self.ci_gold_registry_path]

    def configure_dlt_env(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DESTINATION__FILESYSTEM__BUCKET_URL"] = self.data_dir.resolve().as_uri()
