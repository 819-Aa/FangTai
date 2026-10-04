"""Isolated test server: one SQL fault, controlled only by a local test file."""

import json
import os
from pathlib import Path
from threading import Lock

from food_agent_v2.api_app import app as app
from food_agent_v2.application import commit_service

CONTROL = Path(os.environ["HTTP_COMMIT_FAULT_CONTROL"])
_connect = commit_service._connect
_lock = Lock()


class Cursor:
    def __init__(self, cursor):
        self.inner = cursor

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def execute(self, sql, args=None):
        result = self.inner.execute(sql, args)
        # Fail AFTER the real outbox insert, with the session, result/audit and
        # menu version already written inside the same uncommitted transaction.
        if "INSERT IGNORE INTO outbox" in sql and args:
            with _lock:
                control = (
                    json.loads(CONTROL.read_text(encoding="utf-8")) if CONTROL.exists() else {}
                )
                if args[1] == control.get("request_id") and not control.get("injected"):
                    control.update(injected=True, after_real_outbox_insert=True)
                    CONTROL.write_text(json.dumps(control), encoding="utf-8")
                    raise OSError("synthetic commit fault after outbox insert")
        return result


class Connection:
    def __init__(self, conn):
        self.inner = conn

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def cursor(self, *args, **kwargs):
        return Cursor(self.inner.cursor(*args, **kwargs))

    def rollback(self):
        result = self.inner.rollback()
        with _lock:
            control = json.loads(CONTROL.read_text(encoding="utf-8")) if CONTROL.exists() else {}
            if control.get("injected"):
                control["rollback_seen"] = True
                CONTROL.write_text(json.dumps(control), encoding="utf-8")
        return result


commit_service._connect = lambda: Connection(_connect())
