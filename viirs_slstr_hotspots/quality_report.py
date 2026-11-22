"""Сбор и сохранение отчёта качества одного запуска."""

import json
import logging
import os
import tempfile
from pathlib import Path

from .store.schemas import DetectionValidationResult, QualityReportData, ValidationSummary

logger = logging.getLogger(__name__)


class QualityReport:
    """Накапливает значения отчёта и сохраняет их в JSON."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.exception(
                "Не удалось создать каталог отчёта качества",
                extra={"path": str(self.path.parent)},
            )
            raise OSError(
                f"Не удалось создать каталог отчёта качества {self.path.parent}."
            ) from exc
        self._data: dict[str, object] = {}
        self._validation = ValidationSummary(valid_count=0, invalid_count=0)

    def set(self, key: str, value: object) -> None:
        """Добавляет значение отчёта или заменяет значение с тем же ключом."""
        if not isinstance(key, str):
            raise TypeError("Ключ отчёта качества должен быть строкой.")
        if key == "validation":
            raise ValueError("Ключ 'validation' зарезервирован для результатов проверки.")
        self._data[key] = value

    def record_validation_result(self, result: DetectionValidationResult) -> None:
        """Добавляет результат проверки записи в сводку отчёта."""
        if result.valid:
            self._validation.valid_count += 1
        else:
            self._validation.invalid_count += 1
        self._validation.errors.extend(result.errors)
        self._validation.warnings.extend(result.warnings)

    def flush(self) -> None:
        """Сохраняет накопленный отчёт в UTF-8 JSON."""
        try:
            report = QualityReportData.model_validate(
                {**self._data, "validation": self._validation}
            )
            serialized = json.dumps(
                report.model_dump(mode="python"),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            serialized_bytes = serialized.encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            logger.exception("Не удалось сериализовать отчёт качества")
            raise ValueError(
                "Отчёт качества содержит значение, которое нельзя представить в JSON."
            ) from exc

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(serialized_bytes)

            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            logger.exception(
                "Не удалось записать отчёт качества",
                extra={"path": str(self.path)},
            )
            raise OSError(
                f"Не удалось записать отчёт качества в файл {self.path}."
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.exception(
                        "Не удалось удалить временный файл отчёта качества",
                        extra={"path": str(temporary_path)},
                    )

