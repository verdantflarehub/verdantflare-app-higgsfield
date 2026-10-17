"""Durable discovery metadata; credentials and task journals remain separate."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA synchronous=FULL')
            with connection:
                yield connection
        finally:
            connection.close()
