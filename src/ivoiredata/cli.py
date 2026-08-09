from __future__ import annotations
import argparse,json
from pathlib import Path
from .corpus import build_from_tables
from .engine import IvoireDataEngine
from .query import query_sql
from .tokenizer import train_bpe
def parser():
    p=argparse.ArgumentParser(prog="ivoiredata",description="IvoireData federated data and corpus engine");sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("sources");s.add_argument("--public",action="store_true")
    s=sub.add_parser("sync");s.add_argument("source_id",nargs="?");s.add_argument("--due",action="store_true");s.add_argument("--all-public",action="store_true");s.add_argument("--force",action="store_true")
    s=sub.add_parser("query");s.add_argument("sql");s.add_argument("--max-rows",type=int,default=1000)
    s=sub.add_parser("corpus-build");s.add_argument("version");s.add_argument("tables",nargs="+");s.add_argument("--output",default="corpora");s.add_argument("--shard-size",type=int,default=100000);s.add_argument("--min-quality",type=float,default=0.35)
    s=sub.add_parser("tokenizer-train");s.add_argument("corpus_dir");s.add_argument("--output",default="tokenizer/tokenizer.json");s.add_argument("--vocab-size",type=int,default=32000);return p
def main(argv=None)->int:
    args=parser().parse_args(argv)
    if args.command=="sources":
        for s in IvoireDataEngine().registry.list(public_only=args.public):print(json.dumps(s.__dict__,ensure_ascii=False))
        return 0
    if args.command=="sync":
        e=IvoireDataEngine()
        if args.due:results=e.sync_due(auto_only=not args.all_public,public_only=True,force=args.force)
        elif args.source_id:results=[e.sync(args.source_id,force=args.force)]
        else:raise SystemExit("sync requires source_id or --due")
        failed=0
        for r in results:print(json.dumps(r.__dict__,ensure_ascii=False));failed+=r.status!="success"
        return 1 if failed and not args.all_public else 0
    if args.command=="query":print(json.dumps(query_sql(args.sql,max_rows=args.max_rows),ensure_ascii=False,default=str,indent=2));return 0
    if args.command=="corpus-build":
        stats=build_from_tables(args.tables,Path(args.output),args.version,shard_size=args.shard_size,min_quality=args.min_quality);print(json.dumps(stats.__dict__,ensure_ascii=False,indent=2));return 0
    if args.command=="tokenizer-train":print(train_bpe(Path(args.corpus_dir),Path(args.output),args.vocab_size));return 0
    return 2
if __name__=="__main__":raise SystemExit(main())
