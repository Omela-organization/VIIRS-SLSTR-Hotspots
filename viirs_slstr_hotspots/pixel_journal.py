"""Журнал учёта пикселей (НФТ-04)."""
import sqlite3
from pathlib import Path


class PixelJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS pixels (
                step TEXT,
                reason TEXT,
                entered INTEGER,
                exited INTEGER,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.conn.commit()

    def record(self, step: str, *, entered: int, exited: int, reason: str = "") -> None:
        self.conn.execute(
            "INSERT INTO pixels(step, reason, entered, exited) VALUES (?, ?, ?, ?)",
            (step, reason, entered, exited),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
