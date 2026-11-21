"""Заглушка отчёта качества"""
from pathlib import Path


class QualityReport:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)


    def set(self, key: str, value) -> None:
        pass

    def flush(self) -> None:
        pass

