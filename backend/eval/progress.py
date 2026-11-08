"""Durable, append-only progress for a single agent evaluation (no resume)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


class ProgressJournal:
    def __init__(self, path: Path, preflight: dict):
        self.path = path
        self.preflight = preflight
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("x", encoding="utf-8")
        try:
            self.record("run_started", preflight=self.preflight)
        except BaseException:
            self.stream.close()
            raise
        return self

    def record(self, event: str, **fields):
        row = {"schema_version": 1, "event": event,
               "at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}
        self.stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def start_case(self, case_id: str, thread_id: str):
        self.record("case_started", case_id=case_id, thread_id=thread_id)

    def complete_case(self, result: dict):
        self.record("case_completed", result=result)

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None:
                # Exception text can contain credentials, URLs or provider response bodies.
                # Keep the original exception visible to the caller; persist only its class.
                try:
                    self.record("run_failed", error_type=exc_type.__name__)
                except Exception:
                    pass  # A failed disk must not hide the original evaluation error.
        finally:
            self.stream.close()
        return False
