from pathlib import Path


class PixelJournal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, step: str, *, entered: int, exited: int, reason: str = ""):
        pass
