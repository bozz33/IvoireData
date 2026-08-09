from __future__ import annotations

import os
import time

from .engine import IvoireDataEngine


def run_once():
    return IvoireDataEngine().sync_due(auto_only=True, public_only=True)


def run_forever(interval: int | None = None) -> None:
    interval = interval or int(os.getenv("IVOIREDATA_SCHEDULER_INTERVAL", "3600"))
    interval = max(300, interval)
    while True:
        for result in run_once():
            print(result)
        time.sleep(interval)


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
