from __future__ import annotations
from .settings import Settings
def get_pipeline(settings:Settings):
    import dlt
    settings.configure_dlt_env();return dlt.pipeline(pipeline_name=settings.pipeline_name,destination="filesystem",dataset_name=settings.dataset_name)
