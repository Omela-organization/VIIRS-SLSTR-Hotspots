from .pixel_journal import PixelJournal
from .quality_report import QualityReport
from pathlib import Path


class Step:
    def __init__(self, name, func, inputs, outputs):
        self.name = name
        self.func = func
        self.inputs = inputs
        self.outputs = outputs


class Runner:
    def __init__(self, cfg, run_dir: Path):
        self.steps = []

        self.cfg = cfg
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.journal = PixelJournal(run_dir / "pixel_journal.sqlite")
        self.quality = QualityReport(run_dir / "quality_report.json")


    def register(self, name, func, inputs, outputs):
        step = Step(name, func, inputs, outputs)
        self.steps.append(step)

    def _path(self, name: str) -> Path:
        return self.run_dir / f"{name}.json"

    def _is_built(self, outputs: tuple) -> bool:
        return all(self._path(name).exists() for name in outputs)

    def _execute(self, step):
        inputs = {name: self._path(name) for name in step.inputs}
        outputs = {name: self._path(name) for name in step.outputs}

        result = step.func(
            cfg=self.cfg,
            inputs=inputs,
            outputs=outputs,
            journal=self.journal,
            quality=self.quality
        )

        if not result.get("ok"):
            raise RuntimeError(f"Шаг {step.name} вернул ok=False")


    def run(self, force=False):
        for step in self.steps:
            if not force and self._is_built(step.outputs):
                continue

            self._execute(step)

        self.quality.flush()
        self.journal.close()

    def run_step(self, step_name):
        step = next((s for s in self.steps if s.name == step_name), None)

        if step is None:
            raise ValueError(f"Шаг {step_name} отсутствует")

        for out in step.outputs:
            path = self._path(out)
            if path.exists():
                path.unlink()

        self._execute(step)
        self.quality.flush()
        self.journal.close()
