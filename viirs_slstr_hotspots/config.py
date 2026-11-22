"""Загрузка и валидация конфигурации (ФТ-20)."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class AlgorithmConfig(BaseModel):
    eps0: float = Field(gt=0, description="Порог значимости")
    sigma_rel: float = Field(gt=0, lt=1)
    max_window_viirs_i: int = Field(gt=0)
    max_window_slstr: int = Field(gt=0)
    max_window_viirs_m: int = Field(gt=0)
    tail_confidence: float = Field(ge=0, le=1)


class HistoryConfig(BaseModel):
    n_bins: int = Field(gt=0)
    n_months: int = Field(gt=0)
    n_max_per_cell: int = Field(gt=0)


class StorageConfig(BaseModel):
    l1b_retention_days: int
    tensor_x_retention_days: int
    kriging_cache_gb: float


class ComputeConfig(BaseModel):
    n_processes: int | Literal["auto"]
    regression_subsample: int
    zarr_block: list[int]


class OutputConfig(BaseModel):
    default_step: str
    working_grid_m: int
    reference_grid_m: int


class AcceptanceConfig(BaseModel):
    p2_qq_tolerance: float
    p5_new_confirmed_frac: float


class DeploymentConfig(BaseModel):
    data_root: str | None = None
    reference_grid_file: str | None = None


class Config(BaseModel):
    algorithm: AlgorithmConfig
    history: HistoryConfig
    storage: StorageConfig
    compute: ComputeConfig
    output: OutputConfig
    acceptance: AcceptanceConfig
    deployment: DeploymentConfig


class ConfigError(ValueError):
    """Ошибка валидации конфигурации."""


def load_config(path: Path | None = None) -> Config:
    """Загружает YAML и валидирует через Pydantic."""
    if path is None:
        path = Path(__file__).parent.parent / "config" / "default.yaml"

    with path.open("r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    try:
        return Config.model_validate(raw_data)
    except ValidationError as e:
        raise ConfigError(f"Ошибка в конфигурации {path}:\n{e}") from e