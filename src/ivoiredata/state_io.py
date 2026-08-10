from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default(value: Any) -> Any:
    return value() if callable(value) else copy.deepcopy(value)


def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON state without making a corrupted state file fatal.

    A malformed file is moved aside with a timestamp so it can be inspected later,
    then a clean default value is returned. State is operational metadata; a torn
    write must never prevent the data lake from starting.
    """
    if not path.exists():
        return _default({} if default is None else default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            os.replace(path, corrupt)
        except OSError:
            pass
        return _default({} if default is None else default)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Durably replace a JSON state file using a same-directory atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)
