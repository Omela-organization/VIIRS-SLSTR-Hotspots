from pathlib import Path
import yaml
from pydantic import BaseModel, Field, ValidationError


# 1. Модели для каждого раздела YAML
class AlgorithmConfig(BaseModel):
    pass  # Сюда добавим поля


class HistoryConfig(BaseModel):
    pass


class StorageConfig(BaseModel):
    pass


class ComputeConfig(BaseModel):
    pass


class OutputConfig(BaseModel):
    pass


class AcceptanceConfig(BaseModel):
    pass


class DeploymentConfig(BaseModel):
    pass


# 2. Главная модель, которая собирает всё вместе
class Config(BaseModel):
    algorithm: AlgorithmConfig
    history: HistoryConfig
    storage: StorageConfig
    compute: ComputeConfig
    output: OutputConfig
    acceptance: AcceptanceConfig
    deployment: DeploymentConfig


# 3. Функция загрузки
def load_config(path: Path = None) -> Config:
    # Если путь не передан, ищем default.yaml рядом с этим файлом
    if path is None:
        path = Path(__file__).parent.parent / "config" / "default.yaml"

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Валидация
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Ошибка в конфигурации {path}:\n{e}") from e