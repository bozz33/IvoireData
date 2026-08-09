from __future__ import annotations
import hashlib,json,re
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Iterable
from .cleaning import clean_text
from .dedup import ExactDeduplicator
from .quality import score_text
_IDENTIFIER=re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
@dataclass
class CorpusStats:
    version:str;documents_seen:int=0;documents_written:int=0;duplicates:int=0;rejected_quality:int=0;shards:int=0
def row_to_training_text(row:dict[str,Any])->str:
    if isinstance(row.get("text"),str) and row["text"].strip():return clean_text(row["text"])
    pairs=[]
    for key,value in row.items():
        if key.startswith("__") or key.startswith("_dlt") or value in (None,""):continue
        if isinstance(value,(dict,list)):value=json.dumps(value,ensure_ascii=False,default=str)
        pairs.append(f"{key}: {value}")
    return clean_text("\n".join(pairs))
def build_from_rows(rows:Iterable[dict[str,Any]],output_dir:Path,version:str,*,shard_size:int=100000,min_quality:float=0.35)->CorpusStats:
    output_dir=output_dir/version;output_dir.mkdir(parents=True,exist_ok=True);dedup=ExactDeduplicator();stats=CorpusStats(version=version);shard=None;shard_index=-1;in_shard=0
    try:
        for row in rows:
            stats.documents_seen+=1;text=row_to_training_text(row);quality=score_text(text)
            if quality<min_quality:stats.rejected_quality+=1;continue
            accepted,fp=dedup.accept(text)
            if not accepted:stats.duplicates+=1;continue
            if shard is None or in_shard>=shard_size:
                if shard is not None:shard.close()
                shard_index+=1;in_shard=0;shard=(output_dir/f"train-{shard_index:05d}.jsonl").open("w",encoding="utf-8")
            record={"text":text,"sha256":fp,"quality":quality,"meta":{k:v for k,v in row.items() if k.startswith("__") or k in {"source_id","source_url","chunk_id"}}};shard.write(json.dumps(record,ensure_ascii=False,default=str)+"\n");in_shard+=1;stats.documents_written+=1
    finally:
        if shard is not None:shard.close()
    stats.shards=shard_index+1;manifest={"version":version,"created_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"immutable":True,"format":"jsonl","stats":asdict(stats)};payload=json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+"\n";(output_dir/"manifest.json").write_text(payload,encoding="utf-8");(output_dir/"manifest.sha256").write_text(hashlib.sha256(payload.encode()).hexdigest()+"  manifest.json\n",encoding="utf-8");return stats
def build_from_tables(tables:list[str],output_dir:Path,version:str,*,settings=None,shard_size:int=100000,min_quality:float=0.35)->CorpusStats:
    from .pipeline import get_pipeline
    from .settings import Settings
    settings=settings or Settings.from_env()
    for table in tables:
        if not _IDENTIFIER.match(table):raise ValueError(f"unsafe table name: {table}")
    pipeline=get_pipeline(settings)
    def rows():
        with pipeline.sql_client() as client:
            for table in tables:
                with client.execute_query(f'SELECT * FROM "{table}"') as cursor:
                    names=[col[0] for col in (cursor.description or [])]
                    while True:
                        batch=cursor.fetchmany(5000)
                        if not batch:break
                        for row in batch:
                            item=dict(zip(names,row));item.setdefault("__ivoiredata_table",table);yield item
    return build_from_rows(rows(),output_dir,version,shard_size=shard_size,min_quality=min_quality)
