from .pixel_journal import PixelJournal
from .quality_report import QualityReport
from pathlib import Path


# Объект для runner.
# Хранит в себе всю необходимую информацию о шаге:
# название, функцию (нам нужные функции run в скриптах
# из папки pipline), им
class Step:
    def __init__(self, name, func, inputs, outputs):
        self.name = name # имя шага
        self.func = func # вызываемая функция (нам нужны функции run в скриптах из pipline/
        self.inputs = inputs # имена файлов .json, которые необходимы на этом шаге
        self.outputs = outputs # имена файлов .json, которые должны создаться на шаге


# оркестратор
# На вход идёт конфиг (по умолчанию config/default.yaml)
# и путь к папке для записи файлов run_dir (к примеру, runs/demo)
class Runner:
    def __init__(self, cfg, run_dir: Path):
        self.steps = []

        self.cfg = cfg
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.journal = PixelJournal(run_dir / "pixel_journal.sqlite")
        self.quality = QualityReport(run_dir / "quality_report.json")


    # добавить один шаг
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

    # пройтись по всем шагам.
    def run(self, force=False):
        """
        force - если True, пропускает шаг, все файлы которого уже записаны,
        иначе перезаписывает.
        """
        for step in self.steps:
            if not force and self._is_built(step.outputs):
                continue

            self._execute(step)

        self.quality.flush()
        self.journal.close()


    # выполняет и перезаписывает один шаг
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
