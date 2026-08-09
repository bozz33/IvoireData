from __future__ import annotations
import os,time
from .engine import IvoireDataEngine
def main()->None:
    interval=max(300,int(os.getenv("IVOIREDATA_SCHEDULER_INTERVAL","3600")))
    while True:
        for result in IvoireDataEngine().sync_due(auto_only=True,public_only=True):print(result)
        time.sleep(interval)
if __name__=="__main__":main()
