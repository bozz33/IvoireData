from __future__ import annotations

import os
import time

from .engine import IvoireDataEngine


def run_once():
    engine = IvoireDataEngine()
    if not engine.runtime.automatic_enabled:
        return []
    return engine.sync_due(auto_only=True, public_only=True)


def run_forever(interval: int | None = None) -> None:
    explicit_interval = int(interval) if interval is not None else None
    while True:
        engine = IvoireDataEngine()
        if engine.runtime.automatic_enabled:
            for result in engine.sync_due(auto_only=True, public_only=True):
                print(result)
        sleep_seconds = explicit_interval
        if sleep_seconds is None:
            env_value = os.getenv("IVOIREDATA_SCHEDULER_INTERVAL")
            sleep_seconds = int(env_value) if env_value else engine.runtime.scheduler_interval_seconds
        time.sleep(max(300, int(sleep_seconds)))


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
