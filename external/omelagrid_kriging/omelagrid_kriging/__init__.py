"""Support-aware kriging scaler for OmelaGrid NetCDF datasets."""

from .core import (
    CoordinateTransformer,
    ElevationInputs,
    GridSpec,
    ScalingConfig,
    SupportAwareKrigingScaler,
    VariogramParams,
    configure_logging,
    detect_grid,
    fit_variogram,
    grid_resolution_m,
    load_elevation_inputs,
    run_file,
)

__all__ = [
    "CoordinateTransformer",
    "ElevationInputs",
    "GridSpec",
    "ScalingConfig",
    "SupportAwareKrigingScaler",
    "VariogramParams",
    "configure_logging",
    "detect_grid",
    "fit_variogram",
    "grid_resolution_m",
    "load_elevation_inputs",
    "run_file",
]
