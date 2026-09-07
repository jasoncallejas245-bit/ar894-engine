"""
Shared safe JSON read/write for all persisted state files.

Why this exists: worker.py (the trading loop) and dashboard.py (the Flask
app) run in the same process but read/write the same JSON files
independently. A plain `open(path, "w")` write is NOT atomic -- if the
dashboard reads a state file at the exact moment the worker is mid-write,
it can see a truncated/invalid JSON document and crash that dashboard
request. atomic_write_json avoids this by writing to a temp file first and
renaming it into place -- a rename is atomic on POSIX filesystems (which
covers Railway's containers), so any reader always sees either the old
complete file or the new complete file, never a partial one.

safe_read_json is the matching defensive read: if a file is ever
genuinely corrupt (e.g. process killed mid-write despite the above, or a
hand-edited file), it falls back to the given default instead of raising
and taking down whatever called it.
"""
import json
import os
import tempfile


def atomic_write_json(path, data, indent=2):
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp_path, path)  # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def safe_read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[state_io] {path} unreadable ({e}), using default -- if this persists, the file may need manual repair")
        return default
