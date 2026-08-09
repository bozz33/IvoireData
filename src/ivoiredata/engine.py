from __future__ import annotations
from datetime import datetime,timezone
from .connectors.data_gouv_ci import data_gouv_ci_resource,dataset_id_from_public_url
from .connectors.http_file import http_file_resource
from .connectors.public_web import public_document_resource
from .freshness import FreshnessStore
from .models import SourceSpec,SyncResult
from .pipeline import get_pipeline
from .registry import SourceRegistry
from .settings import Settings
def _now()->str:return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
class IvoireDataEngine:
    def __init__(self,settings:Settings|None=None):
        self.settings=settings or Settings.from_env();self.registry=SourceRegistry.load(self.settings.registry_path,self.settings.runtime_config_path);self.freshness=FreshnessStore(self.settings.state_dir/"freshness.json")
    def _resource_for(self,spec:SourceSpec,*,force:bool=False):
        if spec.connector=="data_gouv_ci":
            dsid=None if spec.source_id=="civ_datagouv_catalog" else dataset_id_from_public_url(spec.source_url)
            return data_gouv_ci_resource(dataset_ids=[dsid] if dsid else None,force=force,user_agent=self.settings.user_agent,limit=spec.options.get("limit"))
        if spec.connector=="http_file":return http_file_resource(source_id=spec.source_id,url=spec.source_url,force=force,user_agent=self.settings.user_agent)
        if spec.connector=="public_web":return public_document_resource(source_id=spec.source_id,url=spec.source_url,force=force,user_agent=self.settings.user_agent)
        raise ValueError(f"unsupported connector {spec.connector!r} for {spec.source_id}")
    def sync(self,source_id:str,*,force:bool=False)->SyncResult:
        spec=self.registry.get(source_id)
        if not spec.public:raise PermissionError(f"{source_id} is not an OPEN public source")
        started=_now()
        try:
            info=get_pipeline(self.settings).run(self._resource_for(spec,force=force));details=str(info);self.freshness.mark(source_id,success=True,details=details);return SyncResult(source_id,"success",started,_now(),spec.connector,details)
        except Exception as exc:
            self.freshness.mark(source_id,success=False,details=str(exc));return SyncResult(source_id,"error",started,_now(),spec.connector,str(exc))
    def sync_due(self,*,auto_only:bool=True,public_only:bool=True,force:bool=False)->list[SyncResult]:
        results=[]
        for spec in self.registry.list(public_only=public_only,auto_only=auto_only):
            if force or self.freshness.due(spec):results.append(self.sync(spec.source_id,force=force))
        return results
