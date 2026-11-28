"""Geospatial kriging scaler for the OmelaGrid datasets.

The module implements two support-aware scaling paths:

* downscaling: a larger raster -> a smaller reference raster by numerical
  integration over the target block support; empty target blocks stay NaN by
  default;
* upscaling: a smaller raster -> a larger reference raster by local ordinary kriging,
  treating coarse values as estimates at cell centers to produce a continuous
  field without inherited block seams. An explicit area-support mode remains
  available for datasets that truly contain areal averages.

The default center model follows the interpretation used in the project review.
The optional area model builds quadrature support directly from the rectilinear
NetCDF grid because the inputs do not contain polygon point-support structures.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

os.environ.setdefault("MPLCONFIGDIR", str(Path.cwd() / ".mplconfig"))

import numpy as np
import xarray as xr
from dask import compute, delayed
from numba import njit
from pyproj import Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from scipy.spatial import cKDTree

try:
    from pyinterpolate import build_experimental_variogram, build_theoretical_variogram
except Exception:  # pragma: no cover - pyinterpolate is optional at import time
    build_experimental_variogram = None
    build_theoretical_variogram = None


LOGGER = logging.getLogger("omelagrid.kriging")
EARTH_RADIUS_M = 6_371_000.0
ELEVATION_OBSERVATION_MODELS = {"elevation-adjusted", "terrain-regression-blend"}


@dataclass(frozen=True)
class GridSpec:
    """Normalized rectilinear grid description."""

    x_name: str
    y_name: str
    x: np.ndarray
    y: np.ndarray
    dx: float
    dy: float

    @property
    def shape(self) -> tuple[int, int]:
        return (self.y.size, self.x.size)


@dataclass(frozen=True)
class CellBounds:
    """Cell bounds prepared for fast overlap queries on a monotonic axis."""

    lows: np.ndarray
    highs: np.ndarray
    order: np.ndarray
    sorted_lows: np.ndarray
    sorted_highs: np.ndarray


@dataclass(frozen=True)
class VariogramParams:
    """Spherical semivariogram parameters in metric coordinates."""

    nugget: float
    sill: float
    rang: float
    model: str = "spherical"
    source: str = "fallback"


@dataclass
class ScalingConfig:
    """Runtime options for support-aware scaling."""

    mode: str = "auto"
    transform_method: str = "utm"
    k_neighbors: int = 16
    max_search_radius: Optional[float] = None
    variogram_sample_size: int = 4000
    variogram_lags: int = 12
    support_points_per_axis: int = 3
    coherence_correction: bool = True
    use_target_mask: bool = True
    max_targets: Optional[int] = None
    random_seed: int = 42
    dask_scheduler: str = "threads"
    parallel_time: bool = True
    time_batch_size: int = 8
    fill_missing_downscale: bool = False
    nodata_below: Optional[float] = None
    nodata_above: Optional[float] = None
    restrict_upscale_to_source_footprint: bool = True
    upscale_observation_model: str = "center"
    target_elevation_path: Optional[str] = None
    target_elevation_variable: Optional[str] = None
    source_elevation_path: Optional[str] = None
    source_elevation_variable: Optional[str] = None
    derive_source_elevation_from_target: bool = False
    lapse_rate_c_per_km: float = -6.5
    max_elevation_correction: Optional[float] = None
    terrain_blend_weight: float = 0.5
    terrain_ridge_alpha: float = 1.0
    output_min: Optional[float] = None
    output_max: Optional[float] = None


@dataclass(frozen=True)
class ElevationInputs:
    """Elevation fields normalized to source and reference grids in metres."""

    source: np.ndarray
    target: np.ndarray
    source_path: str
    source_variable: str
    source_derivation: str
    target_path: str
    target_variable: str


class _SpatialCache:
    """Mutable geometry cache shared by variables on the same input grid."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.center_geometry_key: Optional[tuple[object, ...]] = None
        self.center_geometry: Optional[tuple[np.ndarray, cKDTree, CoordinateTransformer]] = None
        self.target_geometry_cache: Optional[tuple[tuple[int, bytes, bytes], np.ndarray]] = None
        self.neighbor_query_cache: Optional[
            tuple[tuple[int, bytes, float, int], np.ndarray, np.ndarray]
        ] = None


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def log_runtime_backend(config: ScalingConfig) -> None:
    """Log the local compute backend used by the pipeline."""

    LOGGER.info(
        "Compute backend: dask scheduler=%s, parallel_time=%s, distributed_cluster=%s",
        config.dask_scheduler,
        config.parallel_time,
        "not configured",
    )


def apply_nodata_policy(values: np.ndarray, config: ScalingConfig) -> np.ndarray:
    """Convert configured numeric nodata sentinels to NaN."""

    needs_mutation = config.nodata_below is not None or config.nodata_above is not None
    arr = values.astype(np.float32, copy=needs_mutation)
    if config.nodata_below is not None:
        arr[arr <= float(config.nodata_below)] = np.nan
    if config.nodata_above is not None:
        arr[arr >= float(config.nodata_above)] = np.nan
    return arr


def finite_variance(values: np.ndarray, default: float = 0.0) -> float:
    """Variance over finite values without warnings on all-NaN arrays."""

    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return default
    return float(np.var(finite))


def _assign_centers_to_cells(centers: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Map source-cell centers to target cells without clipping outside values.

    The returned indices always refer to the original target-axis order, even
    when that axis is descending.
    """

    centers = np.asarray(centers, dtype=np.float64)
    edges = np.asarray(edges, dtype=np.float64)
    increasing = bool(edges[0] < edges[-1])
    sorted_edges = edges if increasing else edges[::-1]
    cell_count = sorted_edges.size - 1

    assigned = np.searchsorted(sorted_edges, centers, side="right") - 1
    on_upper_edge = np.isclose(centers, sorted_edges[-1], rtol=0.0, atol=1e-12)
    assigned[on_upper_edge] = cell_count - 1
    outside = (centers < sorted_edges[0]) | (centers > sorted_edges[-1])
    assigned[outside] = -1
    assigned[(assigned < 0) | (assigned >= cell_count)] = -1

    if not increasing:
        valid = assigned >= 0
        assigned[valid] = cell_count - 1 - assigned[valid]
    return assigned.astype(np.int32, copy=False)


def validate_config(config: ScalingConfig) -> None:
    """Validate values used by both the Python API and CLI."""

    if config.k_neighbors < 3:
        raise ValueError("k_neighbors must be at least 3 for ordinary kriging.")
    if config.max_search_radius is not None and config.max_search_radius <= 0:
        raise ValueError("max_search_radius must be positive when provided.")
    if config.max_targets is not None and config.max_targets < 0:
        raise ValueError("max_targets must be non-negative when provided.")
    if config.support_points_per_axis < 1:
        raise ValueError("support_points_per_axis must be at least 1.")
    if config.variogram_sample_size < 1:
        raise ValueError("variogram_sample_size must be at least 1.")
    if config.variogram_lags < 2:
        raise ValueError("variogram_lags must be at least 2.")
    if config.time_batch_size < 1:
        raise ValueError("time_batch_size must be at least 1.")
    if config.upscale_observation_model not in {
        "center",
        "area",
        *ELEVATION_OBSERVATION_MODELS,
    }:
        raise ValueError(
            "upscale_observation_model must be one of: center, area, "
            "elevation-adjusted, terrain-regression-blend"
        )
    if not np.isfinite(config.lapse_rate_c_per_km):
        raise ValueError("lapse_rate_c_per_km must be finite.")
    if config.max_elevation_correction is not None and config.max_elevation_correction <= 0:
        raise ValueError("max_elevation_correction must be positive when provided.")
    if not 0.0 <= config.terrain_blend_weight <= 1.0:
        raise ValueError("terrain_blend_weight must be between 0 and 1.")
    if not np.isfinite(config.terrain_ridge_alpha) or config.terrain_ridge_alpha < 0:
        raise ValueError("terrain_ridge_alpha must be finite and non-negative.")
    if config.output_min is not None and not np.isfinite(config.output_min):
        raise ValueError("output_min must be finite when provided.")
    if config.output_max is not None and not np.isfinite(config.output_max):
        raise ValueError("output_max must be finite when provided.")
    if (
        config.output_min is not None
        and config.output_max is not None
        and config.output_min > config.output_max
    ):
        raise ValueError("output_min must not exceed output_max.")
    if config.derive_source_elevation_from_target and config.source_elevation_path:
        raise ValueError(
            "derive_source_elevation_from_target and source_elevation_path are mutually exclusive."
        )
    if bool(config.target_elevation_path) != bool(config.target_elevation_variable):
        raise ValueError(
            "target_elevation_path and target_elevation_variable must be provided together."
        )
    if bool(config.source_elevation_path) != bool(config.source_elevation_variable):
        raise ValueError(
            "source_elevation_path and source_elevation_variable must be provided together."
        )
    if config.derive_source_elevation_from_target and config.source_elevation_variable:
        raise ValueError(
            "derive_source_elevation_from_target and source_elevation_variable are mutually exclusive."
        )


def detect_grid(ds: xr.Dataset) -> GridSpec:
    """Detect common lon/lat or x/y coordinate names and spacing."""

    x_name = next((name for name in ("longitude", "lon", "x") if name in ds.coords), None)
    y_name = next((name for name in ("latitude", "lat", "y") if name in ds.coords), None)
    if x_name is None or y_name is None:
        raise ValueError("Dataset must contain longitude/latitude, lon/lat, or x/y coordinates.")

    x = np.asarray(ds[x_name].values, dtype=np.float64)
    y = np.asarray(ds[y_name].values, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1:
        raise ValueError("Only rectilinear 1D coordinate grids are supported.")

    dx = float(np.nanmedian(np.abs(np.diff(x)))) if x.size > 1 else np.nan
    dy = float(np.nanmedian(np.abs(np.diff(y)))) if y.size > 1 else np.nan
    return GridSpec(x_name=x_name, y_name=y_name, x=x, y=y, dx=dx, dy=dy)


def grid_resolution_m(grid: GridSpec) -> float:
    mid_lat = float(np.nanmean(grid.y))
    meters_per_deg_lat = 111_320.0
    meters_per_deg_lon = meters_per_deg_lat * max(math.cos(math.radians(mid_lat)), 1e-6)
    return float(math.sqrt((grid.dx * meters_per_deg_lon) * (grid.dy * meters_per_deg_lat)))


class CoordinateTransformer:
    """Metric coordinate transformation for kriging distances."""

    def __init__(self, method: str = "utm", crs_auth: str = "EPSG:4326"):
        self.method = method.lower()
        self.crs_auth = crs_auth
        self.optimal_epsg: Optional[str] = None
        self.transformer: Optional[Transformer] = None

    def fit(self, lon: np.ndarray, lat: np.ndarray) -> "CoordinateTransformer":
        if self.method == "utm":
            aoi = AreaOfInterest(
                west_lon_degree=float(np.nanmin(lon)),
                south_lat_degree=float(np.nanmin(lat)),
                east_lon_degree=float(np.nanmax(lon)),
                north_lat_degree=float(np.nanmax(lat)),
            )
            candidates = query_utm_crs_info(datum_name="WGS 84", area_of_interest=aoi)
            if not candidates:
                raise ValueError("Cannot determine a local UTM CRS for the dataset extent.")
            self.optimal_epsg = f"EPSG:{candidates[0].code}"
            self.transformer = Transformer.from_crs(self.crs_auth, self.optimal_epsg, always_xy=True)
            LOGGER.info("Selected local metric CRS: %s", self.optimal_epsg)
        return self

    def transform(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        lon = np.asarray(lon, dtype=np.float64)
        lat = np.asarray(lat, dtype=np.float64)
        if self.method == "ecef":
            lon_rad = np.radians(lon)
            lat_rad = np.radians(lat)
            x = EARTH_RADIUS_M * np.cos(lat_rad) * np.cos(lon_rad)
            y = EARTH_RADIUS_M * np.cos(lat_rad) * np.sin(lon_rad)
            z = EARTH_RADIUS_M * np.sin(lat_rad)
            return np.column_stack((x, y, z)).astype(np.float32)
        if self.transformer is None:
            self.fit(lon, lat)
        assert self.transformer is not None
        x, y = self.transformer.transform(lon, lat)
        return np.column_stack((x, y)).astype(np.float32)


def spherical_semivariance(h: np.ndarray, params: VariogramParams) -> np.ndarray:
    h = np.asarray(h, dtype=np.float32)
    rang = max(float(params.rang), 1e-6)
    nugget = max(float(params.nugget), 0.0)
    sill = max(float(params.sill), nugget + 1e-6)
    partial = sill - nugget
    ratio = np.minimum(h / rang, 1.0)
    gamma = nugget + partial * (1.5 * ratio - 0.5 * ratio**3)
    gamma = np.where(h >= rang, sill, gamma)
    return np.where(h == 0, 0.0, gamma).astype(np.float32)


def fit_variogram(
    coords: np.ndarray,
    values: np.ndarray,
    sample_size: int,
    max_range: Optional[float],
    lag_count: int,
    random_seed: int,
) -> VariogramParams:
    """Fit a spherical variogram, with a deterministic fallback."""

    finite = np.isfinite(values)
    coords = coords[finite]
    values = values[finite].astype(np.float32)
    if values.size < 8:
        variance = float(np.nanvar(values)) if values.size else 1.0
        return VariogramParams(0.0, max(variance, 1e-6), max(max_range or 1.0, 1.0), source="small-sample")

    rng = np.random.default_rng(random_seed)
    n = min(sample_size, values.size)
    idx = rng.choice(values.size, size=n, replace=False)
    sample_coords = coords[idx]
    sample_values = values[idx]

    if max_range is None:
        span = np.nanmax(sample_coords, axis=0) - np.nanmin(sample_coords, axis=0)
        max_range = float(np.linalg.norm(span) / 3.0)
    max_range = max(float(max_range), 1.0)
    step = max(max_range / max(lag_count, 2), 1.0)

    if (
        sample_coords.shape[1] == 2
        and build_experimental_variogram is not None
        and build_theoretical_variogram is not None
    ):
        try:
            point_data = np.column_stack((sample_coords[:, 0], sample_coords[:, 1], sample_values))
            exp = build_experimental_variogram(ds=point_data, step_size=step, max_range=max_range)
            model = build_theoretical_variogram(
                exp,
                models_group="spherical",
                nugget=0,
                rang=max_range * 0.7,
                sill=max(float(np.nanvar(sample_values)), 1e-6),
            )
            sill = float(getattr(model, "sill", np.nanvar(sample_values)))
            rang = float(getattr(model, "rang", max_range * 0.7))
            nugget = float(getattr(model, "nugget", 0.0))
            return VariogramParams(nugget=nugget, sill=max(sill, 1e-6), rang=max(rang, 1.0), source="pyinterpolate")
        except Exception as exc:
            LOGGER.warning("pyinterpolate variogram fit failed; using fallback: %s", exc)
    elif sample_coords.shape[1] != 2:
        LOGGER.info(
            "Skipping 2D pyinterpolate variogram fit for %dD coordinates; using metric fallback",
            sample_coords.shape[1],
        )

    variance = max(float(np.nanvar(sample_values)), 1e-6)
    return VariogramParams(nugget=0.0, sill=variance, rang=max_range * 0.7, source="fallback")


@njit(fastmath=True, nogil=True)
def _solve_ordinary_kriging(
    target_coord: np.ndarray,
    neighbor_coords: np.ndarray,
    neighbor_values: np.ndarray,
    nugget: float,
    sill: float,
    rang: float,
) -> float:
    n = neighbor_values.shape[0]
    if n < 3:
        return np.nan
    k = np.ones((n + 1, n + 1), dtype=np.float32)
    k[n, n] = 0.0
    partial = sill - nugget
    for i in range(n):
        k[i, i] = 0.0
        for j in range(i + 1, n):
            d = 0.0
            for m in range(neighbor_coords.shape[1]):
                diff = neighbor_coords[i, m] - neighbor_coords[j, m]
                d += diff * diff
            h = math.sqrt(d)
            if h >= rang:
                gamma = sill
            elif h == 0.0:
                gamma = 0.0
            else:
                r = h / rang
                gamma = nugget + partial * (1.5 * r - 0.5 * r * r * r)
            k[i, j] = gamma
            k[j, i] = gamma

    rhs = np.ones(n + 1, dtype=np.float32)
    for i in range(n):
        d = 0.0
        for m in range(neighbor_coords.shape[1]):
            diff = neighbor_coords[i, m] - target_coord[m]
            d += diff * diff
        h = math.sqrt(d)
        if h >= rang:
            rhs[i] = sill
        elif h == 0.0:
            rhs[i] = 0.0
        else:
            r = h / rang
            rhs[i] = nugget + partial * (1.5 * r - 0.5 * r * r * r)

    try:
        weights = np.linalg.solve(k, rhs)
    except Exception:
        return np.nan
    pred = 0.0
    for i in range(n):
        pred += weights[i] * neighbor_values[i]
    return pred


@njit(fastmath=True, nogil=True)
def _solve_ordinary_kriging_batch(
    target_coords: np.ndarray,
    neighbor_indices: np.ndarray,
    valid_counts: np.ndarray,
    source_coords: np.ndarray,
    source_values: np.ndarray,
    nugget: float,
    sill: float,
    rang: float,
) -> np.ndarray:
    """Solve many local kriging systems without crossing Python per target."""

    out = np.full(target_coords.shape[0], np.nan, dtype=np.float32)
    dimensions = source_coords.shape[1]
    for i in range(target_coords.shape[0]):
        count = int(valid_counts[i])
        if count < 3:
            continue
        local_coords = np.empty((count, dimensions), dtype=np.float32)
        local_values = np.empty(count, dtype=np.float32)
        for j in range(count):
            source_index = int(neighbor_indices[i, j])
            local_values[j] = source_values[source_index]
            for dimension in range(dimensions):
                local_coords[j, dimension] = source_coords[source_index, dimension]
        out[i] = _solve_ordinary_kriging(
            target_coords[i],
            local_coords,
            local_values,
            nugget,
            sill,
            rang,
        )
    return out


def _solve_ordinary_kriging_grouped(
    target_coords: np.ndarray,
    neighbor_indices: np.ndarray,
    valid_counts: np.ndarray,
    source_coords: np.ndarray,
    source_values: np.ndarray,
    params: VariogramParams,
) -> np.ndarray:
    """Reuse one kriging matrix for targets with the same neighbor set."""

    target_count = target_coords.shape[0]
    if target_count == 0:
        return np.empty(0, dtype=np.float32)

    keys = np.column_stack((valid_counts.astype(np.int64, copy=False), neighbor_indices))
    _, inverse, group_sizes = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    repeated_targets = int(np.sum(group_sizes[group_sizes > 1]))
    if repeated_targets < max(32, target_count // 10):
        return _solve_ordinary_kriging_batch(
            target_coords,
            neighbor_indices,
            valid_counts,
            source_coords,
            source_values,
            float(params.nugget),
            float(params.sill),
            float(params.rang),
        )

    order = np.argsort(inverse, kind="stable")
    ordered_groups = inverse[order]
    boundaries = np.flatnonzero(np.diff(ordered_groups)) + 1
    groups = np.split(order, boundaries)
    out = np.full(target_count, np.nan, dtype=np.float32)

    for rows in groups:
        count = int(valid_counts[rows[0]])
        if count < 3:
            continue
        local_indices = neighbor_indices[rows[0], :count]
        local_coords = source_coords[local_indices].astype(np.float32, copy=False)
        local_values = source_values[local_indices].astype(np.float32, copy=False)

        neighbor_delta = local_coords[:, None, :] - local_coords[None, :, :]
        neighbor_distances = np.sqrt(np.sum(neighbor_delta * neighbor_delta, axis=2))
        matrix = np.ones((count + 1, count + 1), dtype=np.float32)
        matrix[:count, :count] = spherical_semivariance(neighbor_distances, params)
        matrix[count, count] = 0.0

        group_targets = target_coords[rows].astype(np.float32, copy=False)
        target_delta = local_coords[:, None, :] - group_targets[None, :, :]
        target_distances = np.sqrt(np.sum(target_delta * target_delta, axis=2))
        rhs = np.ones((count + 1, rows.size), dtype=np.float32)
        rhs[:count] = spherical_semivariance(target_distances, params)
        try:
            weights = np.linalg.solve(matrix, rhs)
        except np.linalg.LinAlgError:
            continue
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            predictions = np.asarray(local_values @ weights[:count], dtype=np.float32)
        out[rows] = np.where(np.isfinite(predictions), predictions, np.nan)

    LOGGER.debug(
        "Grouped %d kriging targets into %d neighbor matrices",
        target_count,
        len(groups),
    )
    return out


def _solve_ordinary_kriging_grouped_fields(
    target_coords: np.ndarray,
    neighbor_indices: np.ndarray,
    valid_counts: np.ndarray,
    source_coords: np.ndarray,
    source_fields: np.ndarray,
    params: VariogramParams,
) -> np.ndarray:
    """Apply one ordinary-kriging weight solution to multiple source fields."""

    fields = np.asarray(source_fields, dtype=np.float32)
    if fields.ndim != 2 or fields.shape[1] != source_coords.shape[0]:
        raise ValueError("source_fields must have shape (fields, source_points).")
    target_count = target_coords.shape[0]
    out = np.full((fields.shape[0], target_count), np.nan, dtype=np.float32)
    if target_count == 0:
        return out

    keys = np.column_stack((valid_counts.astype(np.int64, copy=False), neighbor_indices))
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    boundaries = np.flatnonzero(np.diff(inverse[order])) + 1
    groups = np.split(order, boundaries)

    for rows in groups:
        count = int(valid_counts[rows[0]])
        if count < 3:
            continue
        local_indices = neighbor_indices[rows[0], :count]
        local_coords = source_coords[local_indices].astype(np.float32, copy=False)

        neighbor_delta = local_coords[:, None, :] - local_coords[None, :, :]
        neighbor_distances = np.sqrt(np.sum(neighbor_delta * neighbor_delta, axis=2))
        matrix = np.ones((count + 1, count + 1), dtype=np.float32)
        matrix[:count, :count] = spherical_semivariance(neighbor_distances, params)
        matrix[count, count] = 0.0

        group_targets = target_coords[rows].astype(np.float32, copy=False)
        target_delta = local_coords[:, None, :] - group_targets[None, :, :]
        target_distances = np.sqrt(np.sum(target_delta * target_delta, axis=2))
        rhs = np.ones((count + 1, rows.size), dtype=np.float32)
        rhs[:count] = spherical_semivariance(target_distances, params)
        try:
            weights = np.linalg.solve(matrix, rhs)[:count]
        except np.linalg.LinAlgError:
            continue
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            predictions = fields[:, local_indices] @ weights
        out[:, rows] = np.where(np.isfinite(predictions), predictions, np.nan)
    return out


def _coord_edges(coord: np.ndarray) -> np.ndarray:
    coord = np.asarray(coord, dtype=np.float64)
    if coord.size == 1:
        return np.array([coord[0] - 0.5, coord[0] + 0.5])
    mid = (coord[:-1] + coord[1:]) / 2.0
    first = coord[0] - (mid[0] - coord[0])
    last = coord[-1] + (coord[-1] - mid[-1])
    return np.concatenate([[first], mid, [last]])


def _prepare_cell_bounds(edges: np.ndarray) -> CellBounds:
    """Prepare monotonic cell bounds for repeated interval-overlap queries."""

    edges = np.asarray(edges, dtype=np.float64)
    lows = np.minimum(edges[:-1], edges[1:])
    highs = np.maximum(edges[:-1], edges[1:])
    if lows.size <= 1 or lows[0] <= lows[-1]:
        order = np.arange(lows.size, dtype=np.int64)
    else:
        order = np.arange(lows.size - 1, -1, -1, dtype=np.int64)
    return CellBounds(
        lows=lows,
        highs=highs,
        order=order,
        sorted_lows=lows[order],
        sorted_highs=highs[order],
    )


def _interval_overlaps(bounds: CellBounds, edge_a: float, edge_b: float) -> tuple[np.ndarray, np.ndarray]:
    """Return source-cell indices and positive overlap lengths for an interval."""

    lower, upper = sorted((float(edge_a), float(edge_b)))
    start = int(np.searchsorted(bounds.sorted_highs, lower, side="right"))
    stop = int(np.searchsorted(bounds.sorted_lows, upper, side="left"))
    indices = bounds.order[start:stop]
    if indices.size == 0:
        return indices, np.empty(0, dtype=np.float64)
    overlap = np.maximum(
        0.0,
        np.minimum(bounds.highs[indices], upper) - np.maximum(bounds.lows[indices], lower),
    )
    positive = overlap > 0
    return indices[positive], overlap[positive]


def _parse_original_area(ds: xr.Dataset) -> Optional[tuple[float, float, float, float]]:
    """Read the actual lon/lat extraction bounds stored by the data provider."""

    text = ds.attrs.get("original_area")
    if not isinstance(text, str):
        return None
    lat_match = re.search(r"lat\s*:\s*\(([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\)", text)
    lon_match = re.search(r"lon\s*:\s*\(([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\)", text)
    if lat_match is None or lon_match is None:
        return None
    lat0, lat1 = map(float, lat_match.groups())
    lon0, lon1 = map(float, lon_match.groups())
    return min(lon0, lon1), max(lon0, lon1), min(lat0, lat1), max(lat0, lat1)


def _coord_edges_with_bounds(coord: np.ndarray, lower: float, upper: float) -> np.ndarray:
    """Build cell edges while honoring provider-supplied outer boundaries."""

    edges = _coord_edges(coord)
    if coord[0] <= coord[-1]:
        edges[0], edges[-1] = lower, upper
    else:
        edges[0], edges[-1] = upper, lower
    return edges


def _static_spatial_variable(ds: xr.Dataset, variable: str) -> xr.DataArray:
    """Return a static 2D spatial variable, accepting singleton extra axes."""

    if variable not in ds:
        raise KeyError(f"Elevation variable {variable!r} is absent from dataset.")
    grid = detect_grid(ds)
    da = ds[variable]
    for dim in tuple(da.dims):
        if dim not in {grid.y_name, grid.x_name}:
            if da.sizes[dim] != 1:
                raise ValueError(
                    f"Elevation variable {variable!r} must be static; dimension {dim!r} "
                    f"has size {da.sizes[dim]}."
                )
            da = da.isel({dim: 0}, drop=True)
    if set(da.dims) != {grid.y_name, grid.x_name}:
        raise ValueError(
            f"Elevation variable {variable!r} must contain spatial dimensions "
            f"{grid.y_name!r} and {grid.x_name!r}."
        )
    return da.transpose(grid.y_name, grid.x_name)


def _elevation_metres(da: xr.DataArray) -> np.ndarray:
    """Normalize elevation or geopotential values to metres."""

    values = np.asarray(da.values, dtype=np.float32)
    units_raw = str(da.attrs.get("units", "")).strip()
    units = (
        units_raw.lower()
        .replace("²", "2")
        .replace("^", "")
        .replace("**", "")
        .replace(" ", "")
    )
    metre_units = {
        "m",
        "meter",
        "meters",
        "metre",
        "metres",
        "masl",
        "mamsl",
    }
    geopotential_units = {
        "m2/s2",
        "m2s-2",
        "m2s^-2",
        "m2sec-2",
        "m2/sec2",
    }
    if units in metre_units:
        return values
    if units in geopotential_units:
        return (values / np.float32(9.80665)).astype(np.float32)
    if not units_raw:
        name = (da.name or "").lower()
        if any(token in name for token in ("elevation", "orography", "height", "dem")):
            LOGGER.warning(
                "Elevation variable %s has no units; metres inferred from its name",
                da.name,
            )
            return values
    raise ValueError(
        f"Unsupported or missing elevation units {units_raw!r} for variable {da.name!r}; "
        "expected metres or geopotential in m2/s2."
    )


def _area_weighted_resample(
    values: np.ndarray,
    source_grid: GridSpec,
    target_grid: GridSpec,
) -> np.ndarray:
    """Resample a static rectilinear field using cell-overlap weighted means."""

    values = np.asarray(values, dtype=np.float32)
    if values.shape != source_grid.shape:
        raise ValueError(
            f"Elevation shape {values.shape} does not match source grid {source_grid.shape}."
        )
    if (
        values.shape == target_grid.shape
        and np.array_equal(source_grid.x, target_grid.x)
        and np.array_equal(source_grid.y, target_grid.y)
    ):
        return values.copy()

    source_x_bounds = _prepare_cell_bounds(_coord_edges(source_grid.x))
    source_y_bounds = _prepare_cell_bounds(_coord_edges(source_grid.y))
    target_x_edges = _coord_edges(target_grid.x)
    target_y_edges = _coord_edges(target_grid.y)
    x_overlaps = [
        _interval_overlaps(source_x_bounds, target_x_edges[ix], target_x_edges[ix + 1])
        for ix in range(target_grid.x.size)
    ]
    y_overlaps = [
        _interval_overlaps(source_y_bounds, target_y_edges[iy], target_y_edges[iy + 1])
        for iy in range(target_grid.y.size)
    ]
    out = np.full(target_grid.shape, np.nan, dtype=np.float32)
    for iy, (source_y, y_weight) in enumerate(y_overlaps):
        if source_y.size == 0:
            continue
        for ix, (source_x, x_weight) in enumerate(x_overlaps):
            if source_x.size == 0:
                continue
            block = values[np.ix_(source_y, source_x)]
            weights = np.outer(y_weight, x_weight)
            finite = np.isfinite(block)
            weight_sum = float(np.sum(weights[finite]))
            if weight_sum > 0:
                out[iy, ix] = float(np.sum(block[finite] * weights[finite]) / weight_sum)
    return out


def load_elevation_inputs(
    config: ScalingConfig,
    ds_in: xr.Dataset,
    ds_ref: xr.Dataset,
) -> ElevationInputs:
    """Load and align elevation inputs required by elevation-adjusted mode."""

    if not config.target_elevation_path or not config.target_elevation_variable:
        raise ValueError(
            "Terrain-aware temperature modes require target_elevation_path and "
            "target_elevation_variable."
        )
    if not config.derive_source_elevation_from_target and (
        not config.source_elevation_path or not config.source_elevation_variable
    ):
        raise ValueError(
            "Terrain-aware temperature modes require source elevation or "
            "derive_source_elevation_from_target=True."
        )

    input_grid = detect_grid(ds_in)
    reference_grid = detect_grid(ds_ref)
    target_path = Path(config.target_elevation_path)
    with xr.open_dataset(target_path) as target_ds:
        target_grid = detect_grid(target_ds)
        target_da = _static_spatial_variable(target_ds, config.target_elevation_variable)
        target_metres = _elevation_metres(target_da)
        target_values = _area_weighted_resample(
            target_metres,
            target_grid,
            reference_grid,
        )

    if config.derive_source_elevation_from_target:
        source_values = _area_weighted_resample(target_values, reference_grid, input_grid)
        source_path = str(target_path)
        source_variable = config.target_elevation_variable
        source_derivation = "area-weighted target DEM aggregation to source grid"
    else:
        assert config.source_elevation_path is not None
        assert config.source_elevation_variable is not None
        source_path_obj = Path(config.source_elevation_path)
        with xr.open_dataset(source_path_obj) as source_ds:
            elevation_grid = detect_grid(source_ds)
            source_da = _static_spatial_variable(source_ds, config.source_elevation_variable)
            source_metres = _elevation_metres(source_da)
            source_values = _area_weighted_resample(
                source_metres,
                elevation_grid,
                input_grid,
            )
        source_path = str(source_path_obj)
        source_variable = config.source_elevation_variable
        source_derivation = "external source elevation aligned by cell-overlap mean"

    return ElevationInputs(
        source=source_values.astype(np.float32),
        target=target_values.astype(np.float32),
        source_path=source_path,
        source_variable=source_variable,
        source_derivation=source_derivation,
        target_path=str(target_path),
        target_variable=config.target_elevation_variable,
    )


def _is_temperature_variable(name: str, da: xr.DataArray) -> bool:
    """Recognize temperature variables without relying on one naming convention."""

    normalized_name = name.lower()
    standard_name = str(da.attrs.get("standard_name", "")).lower()
    long_name = str(da.attrs.get("long_name", "")).lower()
    units = str(da.attrs.get("units", "")).lower().replace(" ", "")
    return (
        any(token in normalized_name for token in ("temperature", "temp", "t2m"))
        or "temperature" in standard_name
        or "temperature" in long_name
        or units in {"k", "kelvin", "degree_celsius", "degrees_celsius", "degc", "°c"}
    )


def _validate_temperature_units(da: xr.DataArray) -> None:
    """Ensure the lapse-rate correction is applied only to K or Celsius data."""

    raw_units = str(da.attrs.get("units", "")).strip()
    if not raw_units:
        LOGGER.warning("Temperature variable %s has no units metadata", da.name)
        return
    units = raw_units.lower().replace(" ", "")
    allowed = {
        "k",
        "kelvin",
        "degree_celsius",
        "degrees_celsius",
        "degreecelsius",
        "degreescelsius",
        "degc",
        "°c",
        "celsius",
    }
    if units not in allowed:
        raise ValueError(
            f"elevation-adjusted requires temperature in Kelvin or Celsius; got {raw_units!r}."
        )


class SupportAwareKrigingScaler:
    """Orchestrates support-aware upscaling and downscaling."""

    def __init__(
        self,
        ds_in: xr.Dataset,
        ds_ref: xr.Dataset,
        variable: str,
        config: ScalingConfig,
        geometry_cache: Optional[_SpatialCache] = None,
        elevation_inputs: Optional[ElevationInputs] = None,
    ):
        self.ds_in = ds_in
        self.ds_ref = ds_ref
        self.variable = variable
        self.config = config
        self.elevation_inputs = elevation_inputs
        validate_config(config)
        self.in_grid = detect_grid(ds_in)
        self.ref_grid = detect_grid(ds_ref)
        self.source_x_edges = _coord_edges(self.in_grid.x)
        self.source_y_edges = _coord_edges(self.in_grid.y)
        self.ref_x_edges = _coord_edges(self.ref_grid.x)
        self.ref_y_edges = _coord_edges(self.ref_grid.y)
        original_area = _parse_original_area(ds_in)
        if (
            original_area is not None
            and self.in_grid.x_name in {"longitude", "lon"}
            and self.in_grid.y_name in {"latitude", "lat"}
        ):
            xmin, xmax, ymin, ymax = original_area
            self.source_x_edges = _coord_edges_with_bounds(self.in_grid.x, xmin, xmax)
            self.source_y_edges = _coord_edges_with_bounds(self.in_grid.y, ymin, ymax)
            LOGGER.info(
                "Using provider source bounds: lon %.8f..%.8f, lat %.8f..%.8f",
                xmin,
                xmax,
                ymin,
                ymax,
            )
        self.source_x_bounds = _prepare_cell_bounds(self.source_x_edges)
        self.source_y_bounds = _prepare_cell_bounds(self.source_y_edges)
        self.ref_x_bounds = _prepare_cell_bounds(self.ref_x_edges)
        self.ref_y_bounds = _prepare_cell_bounds(self.ref_y_edges)
        source_x_to_ref = _assign_centers_to_cells(self.in_grid.x, self.ref_x_edges)
        source_y_to_ref = _assign_centers_to_cells(self.in_grid.y, self.ref_y_edges)
        self.source_x_by_ref = [
            np.flatnonzero(source_x_to_ref == ix) for ix in range(self.ref_grid.x.size)
        ]
        self.source_y_by_ref = [
            np.flatnonzero(source_y_to_ref == iy) for iy in range(self.ref_grid.y.size)
        ]
        self.source_x_overlap_by_ref = [
            _interval_overlaps(self.source_x_bounds, self.ref_x_edges[ix], self.ref_x_edges[ix + 1])
            for ix in range(self.ref_grid.x.size)
        ]
        self.source_y_overlap_by_ref = [
            _interval_overlaps(self.source_y_bounds, self.ref_y_edges[iy], self.ref_y_edges[iy + 1])
            for iy in range(self.ref_grid.y.size)
        ]

        # Time slices usually share one spatial validity mask. Reusing the
        # projected coordinates, KD-tree, target coordinates and neighbor
        # lookup avoids repeating the most expensive invariant geometry work.
        self._spatial_cache = geometry_cache or _SpatialCache()

        if variable not in ds_in:
            raise KeyError(f"Variable {variable!r} is absent from input dataset.")
        if self.config.upscale_observation_model in ELEVATION_OBSERVATION_MODELS:
            if self.elevation_inputs is None and self.config.target_elevation_path:
                self.elevation_inputs = load_elevation_inputs(config, ds_in, ds_ref)
            if not _is_temperature_variable(variable, ds_in[variable]):
                raise ValueError(
                    f"{self.config.upscale_observation_model} may only be used with an explicitly selected "
                    "temperature variable."
                )
            _validate_temperature_units(ds_in[variable])
            if self.elevation_inputs is None:
                raise ValueError(
                    f"{self.config.upscale_observation_model} requires prepared elevation inputs."
                )
            if self.elevation_inputs.source.shape != self.in_grid.shape:
                raise ValueError("Source elevation does not match the temperature source grid.")
            if self.elevation_inputs.target.shape != self.ref_grid.shape:
                raise ValueError("Target elevation does not match the reference grid.")

    def __getstate__(self) -> dict[str, object]:
        """Keep the scaler serializable for the optional process scheduler."""

        state = self.__dict__.copy()
        state["_spatial_cache"] = None
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        self._spatial_cache = _SpatialCache()

    def _ordered_variable(self) -> xr.DataArray:
        """Return the variable in canonical (time, y, x) or (y, x) order."""

        da = self.ds_in[self.variable]
        spatial_dims = {self.in_grid.y_name, self.in_grid.x_name}
        expected_dims = spatial_dims | ({"time"} if "time" in da.dims else set())
        extra_dims = set(da.dims) - expected_dims
        missing_dims = spatial_dims - set(da.dims)
        if missing_dims:
            raise ValueError(
                f"Variable {self.variable!r} is missing spatial dimensions: {sorted(missing_dims)}"
            )
        if extra_dims:
            raise ValueError(
                f"Variable {self.variable!r} has unsupported dimensions {sorted(extra_dims)}; "
                "select them before scaling"
            )
        order = (
            ("time", self.in_grid.y_name, self.in_grid.x_name)
            if "time" in da.dims
            else (self.in_grid.y_name, self.in_grid.x_name)
        )
        return da.transpose(*order)

    def run(self) -> xr.Dataset:
        log_runtime_backend(self.config)
        mode = self.config.mode.lower()
        if mode == "auto":
            in_res = grid_resolution_m(self.in_grid)
            ref_res = grid_resolution_m(self.ref_grid)
            mode = "downscale" if in_res < ref_res else "upscale"
            LOGGER.info("Auto mode selected %s (input %.1fm, reference %.1fm)", mode, in_res, ref_res)
        if mode == "downscale":
            if self.config.upscale_observation_model in ELEVATION_OBSERVATION_MODELS:
                raise ValueError(
                    f"{self.config.upscale_observation_model} is only available in upscale mode."
                )
            return self.downscale()
        if mode == "upscale":
            return self.upscale()
        raise ValueError("mode must be one of: auto, upscale, downscale")

    def _reference_mask(self) -> xr.DataArray:
        """Return the reference mask normalized to canonical spatial order."""

        mask_da = self.ds_ref["grid8_mask"]
        expected_dims = {self.ref_grid.y_name, self.ref_grid.x_name}
        if set(mask_da.dims) != expected_dims or mask_da.ndim != 2:
            raise ValueError(
                "grid8_mask must be a 2D variable with dimensions "
                f"{self.ref_grid.y_name!r} and {self.ref_grid.x_name!r}; got {mask_da.dims!r}."
            )
        return mask_da.transpose(self.ref_grid.y_name, self.ref_grid.x_name)

    def target_mask(self) -> np.ndarray:
        if not self.config.use_target_mask or "grid8_mask" not in self.ds_ref:
            return np.ones(self.ref_grid.shape, dtype=bool)
        mask = np.asarray(self._reference_mask().values)
        return np.isfinite(mask) & (mask > 0)

    def upscale_target_mask(self, values_2d: np.ndarray) -> np.ndarray:
        """Return the reference mask, optionally clipped to source support."""

        mask = self.target_mask()
        if not self.config.restrict_upscale_to_source_footprint:
            return mask

        finite_source = np.isfinite(values_2d)
        if not finite_source.any():
            return np.zeros_like(mask, dtype=bool)

        x_edges = self.source_x_edges
        y_edges = self.source_y_edges
        footprint = np.zeros_like(mask, dtype=bool)

        if finite_source.all():
            xmin = float(np.nanmin(x_edges))
            xmax = float(np.nanmax(x_edges))
            ymin = float(np.nanmin(y_edges))
            ymax = float(np.nanmax(y_edges))
            x_mask = (self.ref_grid.x >= xmin) & (self.ref_grid.x <= xmax)
            y_mask = (self.ref_grid.y >= ymin) & (self.ref_grid.y <= ymax)
            footprint[np.ix_(y_mask, x_mask)] = True
        else:
            for iy in range(self.in_grid.y.size):
                y0, y1 = y_edges[iy], y_edges[iy + 1]
                ymin, ymax = min(y0, y1), max(y0, y1)
                y_mask = (self.ref_grid.y >= ymin) & (self.ref_grid.y <= ymax)
                if not y_mask.any():
                    continue
                for ix in range(self.in_grid.x.size):
                    if not finite_source[iy, ix]:
                        continue
                    x0, x1 = x_edges[ix], x_edges[ix + 1]
                    xmin, xmax = min(x0, x1), max(x0, x1)
                    x_mask = (self.ref_grid.x >= xmin) & (self.ref_grid.x <= xmax)
                    if x_mask.any():
                        footprint[np.ix_(y_mask, x_mask)] = True

        clipped = mask & footprint
        skipped = int(np.count_nonzero(mask) - np.count_nonzero(clipped))
        if skipped > 0:
            LOGGER.info("Upscale target mask clipped to source footprint; skipped %d reference cells", skipped)
        return clipped

    def _target_indices(self, mask: np.ndarray) -> np.ndarray:
        indices = np.argwhere(mask)
        if self.config.max_targets is not None and indices.shape[0] > self.config.max_targets:
            rng = np.random.default_rng(self.config.random_seed)
            take = rng.choice(indices.shape[0], size=self.config.max_targets, replace=False)
            indices = indices[np.sort(take)]
            LOGGER.info("Using deterministic subset of %d target cells for demo run", indices.shape[0])
        return indices

    def _source_points_for_2d(
        self,
        values_2d: np.ndarray,
        additional_valid: Optional[np.ndarray] = None,
    ) -> tuple[np.ndarray, np.ndarray, cKDTree, CoordinateTransformer]:
        valid = np.isfinite(values_2d)
        if additional_valid is not None:
            if additional_valid.shape != valid.shape:
                raise ValueError("Additional source validity mask has an incompatible shape.")
            valid &= additional_valid
        key = (
            self.config.transform_method,
            self.in_grid.x.tobytes(),
            self.in_grid.y.tobytes(),
            valid.shape,
            np.packbits(valid, axis=None).tobytes(),
        )
        cache = self._spatial_cache
        with cache.lock:
            if key != cache.center_geometry_key or cache.center_geometry is None:
                lon2d, lat2d = np.meshgrid(self.in_grid.x, self.in_grid.y)
                transformer = CoordinateTransformer(self.config.transform_method)
                transformer.fit(lon2d[valid], lat2d[valid])
                coords = transformer.transform(lon2d[valid], lat2d[valid])
                tree = cKDTree(coords)
                cache.center_geometry_key = key
                cache.center_geometry = (coords, tree, transformer)
                cache.target_geometry_cache = None
                cache.neighbor_query_cache = None
            coords, tree, transformer = cache.center_geometry
        vals = values_2d[valid].astype(np.float32)
        return coords, vals, tree, transformer

    def _target_coords(
        self,
        indices: np.ndarray,
        transformer: CoordinateTransformer,
    ) -> np.ndarray:
        """Transform reference coordinates once for a repeated time geometry."""

        indices_key = indices.tobytes()
        key = (id(transformer), indices_key, self.config.transform_method.encode())
        cache = self._spatial_cache
        with cache.lock:
            if cache.target_geometry_cache is not None and cache.target_geometry_cache[0] == key:
                return cache.target_geometry_cache[1]
            lon = self.ref_grid.x[indices[:, 1]]
            lat = self.ref_grid.y[indices[:, 0]]
            coords = transformer.transform(lon, lat)
            cache.target_geometry_cache = (key, coords)
            return coords

    def _source_blocks_for_2d(
        self, values_2d: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, cKDTree, CoordinateTransformer]:
        """Return valid block centroids and quadrature support in metric space."""

        valid = np.isfinite(values_2d)
        valid_y, valid_x = np.where(valid)
        cell_x0 = self.source_x_edges[valid_x]
        cell_x1 = self.source_x_edges[valid_x + 1]
        cell_y0 = self.source_y_edges[valid_y]
        cell_y1 = self.source_y_edges[valid_y + 1]
        centroid_lon = (cell_x0 + cell_x1) / 2.0
        centroid_lat = (cell_y0 + cell_y1) / 2.0
        transformer = CoordinateTransformer(self.config.transform_method)
        transformer.fit(centroid_lon, centroid_lat)
        centroids = transformer.transform(centroid_lon, centroid_lat)
        vals = values_2d[valid].astype(np.float32)

        points_per_axis = max(int(self.config.support_points_per_axis), 1)
        fractions = (np.arange(points_per_axis, dtype=np.float64) + 0.5) / points_per_axis
        supports = []
        for fy in fractions:
            support_lat = cell_y0 + fy * (cell_y1 - cell_y0)
            for fx in fractions:
                support_lon = cell_x0 + fx * (cell_x1 - cell_x0)
                supports.append(transformer.transform(support_lon, support_lat))
        support_coords = np.stack(supports, axis=1).astype(np.float32)
        return centroids, support_coords, vals, cKDTree(centroids), transformer

    def _neighbor_lookup(
        self,
        target_coords: np.ndarray,
        tree: cKDTree,
        source_count: int,
        radius: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return cached safe neighbor indices and valid counts."""

        k = self.config.k_neighbors
        query_key = (id(tree), target_coords.tobytes(), float(radius), k)
        cache = self._spatial_cache
        with cache.lock:
            cached = cache.neighbor_query_cache
            if cached is not None and cached[0] == query_key:
                safe_indices, valid_counts = cached[1], cached[2]
            else:
                distances, indices = tree.query(
                    target_coords,
                    k=k,
                    distance_upper_bound=radius,
                    workers=1,
                )
                distances = np.asarray(distances)
                indices = np.asarray(indices)
                if distances.ndim == 1:
                    distances = distances[:, None]
                    indices = indices[:, None]
                good = np.isfinite(distances) & (indices < source_count)
                valid_counts = np.sum(good, axis=1).astype(np.int32)
                safe_indices = np.where(good, indices, 0).astype(np.int64, copy=False)
                cache.neighbor_query_cache = (query_key, safe_indices, valid_counts)
        return safe_indices, valid_counts

    def _krige_at(
        self,
        target_coords: np.ndarray,
        tree: cKDTree,
        source_coords: np.ndarray,
        source_values: np.ndarray,
        params: VariogramParams,
    ) -> np.ndarray:
        radius = float(self.config.max_search_radius or params.rang)
        safe_indices, valid_counts = self._neighbor_lookup(
            target_coords,
            tree,
            source_values.size,
            radius,
        )
        return _solve_ordinary_kriging_grouped(
            target_coords.astype(np.float32, copy=False),
            safe_indices,
            valid_counts,
            source_coords.astype(np.float32, copy=False),
            source_values.astype(np.float32, copy=False),
            params,
        )

    def _krige_fields_at(
        self,
        target_coords: np.ndarray,
        tree: cKDTree,
        source_coords: np.ndarray,
        source_fields: np.ndarray,
        params: VariogramParams,
    ) -> np.ndarray:
        """Interpolate several fields with exactly the same kriging weights."""

        radius = float(self.config.max_search_radius or params.rang)
        safe_indices, valid_counts = self._neighbor_lookup(
            target_coords,
            tree,
            source_fields.shape[1],
            radius,
        )
        return _solve_ordinary_kriging_grouped_fields(
            target_coords.astype(np.float32, copy=False),
            safe_indices,
            valid_counts,
            source_coords.astype(np.float32, copy=False),
            source_fields.astype(np.float32, copy=False),
            params,
        )

    def _area_to_point_at(
        self,
        target_coords: np.ndarray,
        tree: cKDTree,
        block_supports: np.ndarray,
        block_values: np.ndarray,
        params: VariogramParams,
    ) -> np.ndarray:
        """Local ATPK approximation with block-block and block-point semivariances."""

        radius = self.config.max_search_radius or params.rang
        k_neighbors = min(self.config.k_neighbors, block_values.size)
        out = np.full(target_coords.shape[0], np.nan, dtype=np.float32)

        for i, coord in enumerate(target_coords):
            distances, idx = tree.query(coord, k=k_neighbors, distance_upper_bound=radius)
            distances = np.atleast_1d(distances)
            idx = np.atleast_1d(idx)
            good = np.isfinite(distances) & (idx < block_values.size)
            if good.sum() < 3:
                continue

            local_idx = idx[good]
            supports = block_supports[local_idx]
            values = block_values[local_idx]
            n = values.size
            k_matrix = np.ones((n + 1, n + 1), dtype=np.float32)
            k_matrix[n, n] = 0.0

            for a in range(n):
                for b in range(a, n):
                    diff = supports[a, :, None, :] - supports[b, None, :, :]
                    h = np.sqrt(np.sum(diff * diff, axis=2))
                    gamma = float(np.mean(spherical_semivariance(h, params)))
                    k_matrix[a, b] = gamma
                    k_matrix[b, a] = gamma

            rhs = np.ones(n + 1, dtype=np.float32)
            for a in range(n):
                diff = supports[a] - coord[None, :]
                h = np.sqrt(np.sum(diff * diff, axis=1))
                rhs[a] = float(np.mean(spherical_semivariance(h, params)))

            try:
                weights = np.linalg.solve(k_matrix, rhs)
            except np.linalg.LinAlgError:
                try:
                    weights = np.linalg.lstsq(k_matrix, rhs, rcond=None)[0]
                except np.linalg.LinAlgError:
                    continue
            out[i] = float(np.sum(weights[:n] * values))
        return out

    def _downscale_2d_with_stats(
        self,
        values_2d: np.ndarray,
    ) -> tuple[np.ndarray, dict[str, np.ndarray], VariogramParams]:
        """Compute the support-weighted result and synthetic features in one pass."""

        target = np.full(self.ref_grid.shape, np.nan, dtype=np.float32)
        stats = {
            name: np.full(self.ref_grid.shape, np.nan, dtype=np.float32)
            for name in ("mean", "median", "var", "min", "max")
        }
        mask = self.target_mask()
        indices = self._target_indices(mask)

        empty_cells: list[tuple[int, int]] = []

        for n, (iy, ix) in enumerate(indices, start=1):
            source_x, x_overlap = self.source_x_overlap_by_ref[int(ix)]
            source_y, y_overlap = self.source_y_overlap_by_ref[int(iy)]
            if source_x.size == 0 or source_y.size == 0:
                empty_cells.append((int(iy), int(ix)))
            else:
                block = values_2d[np.ix_(source_y, source_x)]
                weights = np.outer(y_overlap, x_overlap)
                valid = np.isfinite(block)
                valid_weight = float(np.sum(weights[valid]))
                if valid_weight > 0:
                    target[iy, ix] = float(np.sum(block[valid] * weights[valid]) / valid_weight)
                else:
                    empty_cells.append((int(iy), int(ix)))

            center_y = self.source_y_by_ref[int(iy)]
            center_x = self.source_x_by_ref[int(ix)]
            if center_y.size and center_x.size:
                center_block = values_2d[np.ix_(center_y, center_x)]
                finite = center_block[np.isfinite(center_block)]
                if finite.size:
                    stats["mean"][iy, ix] = np.mean(finite, dtype=np.float64)
                    stats["median"][iy, ix] = np.median(finite)
                    stats["var"][iy, ix] = np.var(finite, dtype=np.float64, ddof=0)
                    stats["min"][iy, ix] = np.min(finite)
                    stats["max"][iy, ix] = np.max(finite)
            if n % 25_000 == 0:
                LOGGER.info("Downscaled %d / %d target cells", n, indices.shape[0])

        params = VariogramParams(
            nugget=0.0,
            sill=finite_variance(values_2d),
            rang=float(self.config.max_search_radius or grid_resolution_m(self.ref_grid) * 3.0),
            source="block-mean",
        )
        if empty_cells and not self.config.fill_missing_downscale:
            LOGGER.info("Skipped %d empty target blocks; values remain NaN", len(empty_cells))
        elif empty_cells:
            if not np.isfinite(values_2d).any():
                LOGGER.info("Cannot fill empty target blocks because the source contains no finite values")
                return target, stats, params
            LOGGER.info("Running local kriging fill for %d empty target blocks", len(empty_cells))
            source_coords, source_values, tree, transformer = self._source_points_for_2d(values_2d)
            params = fit_variogram(
                source_coords,
                source_values,
                self.config.variogram_sample_size,
                self.config.max_search_radius,
                self.config.variogram_lags,
                self.config.random_seed,
            )
            LOGGER.info("Variogram: %s", params)
            lon = np.array([self.ref_grid.x[ix] for _, ix in empty_cells], dtype=np.float64)
            lat = np.array([self.ref_grid.y[iy] for iy, _ in empty_cells], dtype=np.float64)
            coords = transformer.transform(lon, lat)
            preds = self._krige_at(coords, tree, source_coords, source_values, params)
            for (iy, ix), pred in zip(empty_cells, preds):
                target[iy, ix] = pred
        return target, stats, params

    def _downscale_2d(self, values_2d: np.ndarray) -> tuple[np.ndarray, VariogramParams]:
        """Compatibility wrapper returning only the normal downscale output."""

        target, _, params = self._downscale_2d_with_stats(values_2d)
        return target, params

    def _downscale_synthetic_stats_2d(self, values_2d: np.ndarray) -> dict[str, np.ndarray]:
        """Aggregate source pixels by target cell as mean/median/variance.

        Membership follows the source-cell center rule used by the reference
        implementation from ``омела.zip``. Missing values are ignored and
        variance uses ``ddof=0``.
        """

        _, stats, _ = self._downscale_2d_with_stats(values_2d)
        return stats

    def _add_synthetic_stats(
        self,
        ds: xr.Dataset,
        stats: dict[str, np.ndarray],
        time: Optional[np.ndarray] = None,
    ) -> xr.Dataset:
        """Attach downscaling statistics to the normal scaling output."""

        dims = (
            (self.ref_grid.y_name, self.ref_grid.x_name)
            if time is None
            else ("time", self.ref_grid.y_name, self.ref_grid.x_name)
        )
        for statistic, values in stats.items():
            name = f"{self.variable}_{statistic}"
            ds[name] = (dims, values.astype(np.float32))
            ds[name].attrs.update(dict(self.ds_in[self.variable].attrs))
            ds[name].attrs.update(
                {
                    "synthetic_feature": statistic,
                    "aggregation": "source-cell centers grouped inside reference-grid cells",
                    "skipna": "true",
                    "variance_ddof": 0 if statistic == "var" else "not_applicable",
                    "dtype_policy": "float32",
                }
            )
        ds.attrs["synthetic_statistics"] = "mean, median, var (ddof=0), min, max"
        return ds

    def downscale(self) -> xr.Dataset:
        da = self._ordered_variable()
        if "time" not in da.dims:
            if da.ndim != 2:
                raise ValueError("Downscaling expects a 2D variable or a variable with a time axis.")
            values = apply_nodata_policy(da.values, self.config)
            out, stats, params = self._downscale_2d_with_stats(values)
            return self._add_synthetic_stats(
                self._build_output(out, "downscale", params),
                stats,
            )

        def run_slice(t: int) -> tuple[np.ndarray, dict[str, np.ndarray], VariogramParams]:
            LOGGER.info("Downscaling time slice %d / %d", t + 1, da.sizes["time"])
            arr = apply_nodata_policy(da.isel(time=t).values, self.config)
            return self._downscale_2d_with_stats(arr)

        time_size = da.sizes["time"]
        stacked = np.empty((time_size, *self.ref_grid.shape), dtype=np.float32)
        stacked_stats = {
            name: np.empty_like(stacked)
            for name in ("mean", "median", "var", "min", "max")
        }
        params_by_time: list[VariogramParams] = []
        batch_size = min(self.config.time_batch_size, time_size)
        for start in range(0, time_size, batch_size):
            stop = min(start + batch_size, time_size)
            if self.config.parallel_time and stop - start > 1:
                tasks = [delayed(run_slice)(t) for t in range(start, stop)]
                results = compute(*tasks, scheduler=self.config.dask_scheduler)
            else:
                results = [run_slice(t) for t in range(start, stop)]
            for t, (out, stats, params) in zip(range(start, stop), results):
                stacked[t] = out
                for name in stacked_stats:
                    stacked_stats[name][t] = stats[name]
                params_by_time.append(params)
        return self._add_synthetic_stats(
            self._build_output(stacked, "downscale", params_by_time, time=da["time"].values),
            stacked_stats,
            time=da["time"].values,
        )

    def _upscale_2d(self, values_2d: np.ndarray) -> tuple[np.ndarray, VariogramParams]:
        target = np.full(self.ref_grid.shape, np.nan, dtype=np.float32)
        mask = self.upscale_target_mask(values_2d)
        indices = self._target_indices(mask)
        if indices.size == 0:
            params = VariogramParams(
                nugget=0.0,
                sill=finite_variance(values_2d),
                rang=float(self.config.max_search_radius or grid_resolution_m(self.ref_grid) * 3.0),
                source="empty-target-footprint",
            )
            LOGGER.info("No reference cells inside source footprint; upscale output remains NaN")
            return target, params
        observation_model = self.config.upscale_observation_model.lower()
        if observation_model == "center":
            source_coords, source_values, tree, transformer = self._source_points_for_2d(values_2d)
        elif observation_model == "area":
            source_coords, source_supports, source_values, tree, transformer = self._source_blocks_for_2d(values_2d)
        else:
            raise ValueError("_upscale_2d supports center and area observation models only")
        params = fit_variogram(
            source_coords,
            source_values,
            self.config.variogram_sample_size,
            self.config.max_search_radius,
            self.config.variogram_lags,
            self.config.random_seed,
        )
        LOGGER.info("Variogram: %s", params)
        if observation_model == "center":
            target_coords = self._target_coords(indices, transformer)
            preds = self._krige_at(target_coords, tree, source_coords, source_values, params)
        else:
            lon = self.ref_grid.x[indices[:, 1]]
            lat = self.ref_grid.y[indices[:, 0]]
            target_coords = transformer.transform(lon, lat)
            preds = self._area_to_point_at(
                target_coords,
                tree,
                source_supports,
                source_values,
                params,
            )
        target[indices[:, 0], indices[:, 1]] = preds

        if observation_model == "area" and self.config.coherence_correction:
            target = self._apply_coherence_correction(target, values_2d)
        return target, params

    def _elevation_adjusted_2d(
        self,
        values_2d: np.ndarray,
    ) -> tuple[dict[str, np.ndarray], VariogramParams, int]:
        """Krig temperature and source elevation, then apply a lapse-rate correction."""

        assert self.elevation_inputs is not None
        source_elevation = self.elevation_inputs.source
        target_elevation = self.elevation_inputs.target
        combined_valid = np.isfinite(values_2d) & np.isfinite(source_elevation)
        valid_values = np.where(combined_valid, values_2d, np.nan).astype(np.float32)
        mask = self.upscale_target_mask(valid_values) & np.isfinite(target_elevation)
        indices = self._target_indices(mask)
        diagnostics = {
            "final": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "temperature_xy": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "correction": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "source_elevation_xy": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
        }
        if indices.size == 0 or np.count_nonzero(combined_valid) < 3:
            params = VariogramParams(
                nugget=0.0,
                sill=finite_variance(values_2d),
                rang=float(self.config.max_search_radius or grid_resolution_m(self.ref_grid) * 3.0),
                source="empty-elevation-adjusted-target",
            )
            return diagnostics, params, 0

        source_coords, temperature_values, tree, transformer = self._source_points_for_2d(
            values_2d,
            additional_valid=np.isfinite(source_elevation),
        )
        source_elevation_values = source_elevation[combined_valid].astype(np.float32)
        params = fit_variogram(
            source_coords,
            temperature_values,
            self.config.variogram_sample_size,
            self.config.max_search_radius,
            self.config.variogram_lags,
            self.config.random_seed,
        )
        LOGGER.info("Elevation-adjusted temperature variogram: %s", params)
        target_coords = self._target_coords(indices, transformer)
        predictions = self._krige_fields_at(
            target_coords,
            tree,
            source_coords,
            np.stack((temperature_values, source_elevation_values), axis=0),
            params,
        )
        temperature_xy, source_elevation_xy = predictions
        target_elevation_values = target_elevation[indices[:, 0], indices[:, 1]]
        correction = (
            np.float32(self.config.lapse_rate_c_per_km)
            * (target_elevation_values - source_elevation_xy)
            / np.float32(1000.0)
        ).astype(np.float32)
        clipped_count = 0
        if self.config.max_elevation_correction is not None:
            limit = np.float32(self.config.max_elevation_correction)
            clipped_count = int(np.count_nonzero(np.isfinite(correction) & (np.abs(correction) > limit)))
            correction = np.clip(correction, -limit, limit).astype(np.float32)
        final = (temperature_xy + correction).astype(np.float32)
        rows, cols = indices[:, 0], indices[:, 1]
        diagnostics["temperature_xy"][rows, cols] = temperature_xy
        diagnostics["source_elevation_xy"][rows, cols] = source_elevation_xy
        diagnostics["correction"][rows, cols] = correction
        diagnostics["final"][rows, cols] = final
        return diagnostics, params, clipped_count

    def _terrain_regression_prediction(
        self,
        values_2d: np.ndarray,
        source_elevation: np.ndarray,
        target_elevation_values: np.ndarray,
        indices: np.ndarray,
    ) -> tuple[np.ndarray, dict[str, object]]:
        """Fit a conservative ridge trend using lon, lat and elevation."""

        valid = np.isfinite(values_2d) & np.isfinite(source_elevation)
        lon2d, lat2d = np.meshgrid(self.in_grid.x, self.in_grid.y)
        source_features = np.column_stack(
            (lon2d[valid], lat2d[valid], source_elevation[valid])
        ).astype(np.float64)
        source_temperature = values_2d[valid].astype(np.float64)
        target_features = np.column_stack(
            (
                self.ref_grid.x[indices[:, 1]],
                self.ref_grid.y[indices[:, 0]],
                target_elevation_values,
            )
        ).astype(np.float64)

        feature_mean = np.mean(source_features, axis=0)
        feature_scale = np.std(source_features, axis=0)
        feature_scale = np.where(feature_scale > 1e-12, feature_scale, 1.0)
        source_standardized = (source_features - feature_mean) / feature_scale
        target_standardized = (target_features - feature_mean) / feature_scale
        source_design = np.column_stack(
            (np.ones(source_standardized.shape[0]), source_standardized)
        )
        target_design = np.column_stack(
            (np.ones(target_standardized.shape[0]), target_standardized)
        )

        penalty = np.eye(source_design.shape[1], dtype=np.float64)
        penalty[0, 0] = 0.0
        system = source_design.T @ source_design + self.config.terrain_ridge_alpha * penalty
        right_hand_side = source_design.T @ source_temperature
        try:
            coefficients = np.linalg.solve(system, right_hand_side)
        except np.linalg.LinAlgError:
            coefficients = np.linalg.lstsq(system, right_hand_side, rcond=None)[0]

        fitted = coefficients[0] + np.sum(
            source_standardized * coefficients[1:], axis=1
        )
        if not np.isfinite(coefficients).all() or not np.isfinite(target_design).all():
            raise ValueError("Terrain regression produced non-finite coefficients or features.")
        with np.errstate(over="raise", divide="raise", invalid="raise"):
            try:
                prediction_64 = coefficients[0] + np.sum(
                    target_standardized * coefficients[1:], axis=1
                )
            except FloatingPointError as exc:
                raise ValueError("Terrain regression prediction is numerically unstable.") from exc
        if not np.isfinite(prediction_64).all():
            raise ValueError("Terrain regression prediction contains non-finite values.")
        prediction = prediction_64.astype(np.float32)
        residual = source_temperature - fitted
        residual_sum = float(np.sum(residual**2))
        total_sum = float(
            np.sum((source_temperature - np.mean(source_temperature)) ** 2)
        )
        physical_slopes = coefficients[1:] / feature_scale
        try:
            influence = np.linalg.solve(system, source_design.T).T
        except np.linalg.LinAlgError:
            influence = (np.linalg.pinv(system) @ source_design.T).T
        leverage = np.sum(source_design * influence, axis=1)
        loo_denominator = np.maximum(1.0 - leverage, 1e-6)
        loo_residual = residual / loo_denominator
        stats: dict[str, object] = {
            "training_samples": int(source_temperature.size),
            "training_rmse": float(np.sqrt(np.mean(residual**2))),
            "leave_one_out_rmse": float(np.sqrt(np.mean(loo_residual**2))),
            "training_r2": float(1.0 - residual_sum / total_sum) if total_sum > 0 else 1.0,
            "intercept_standardized": float(coefficients[0]),
            "longitude_slope_c_per_degree": float(physical_slopes[0]),
            "latitude_slope_c_per_degree": float(physical_slopes[1]),
            "elevation_slope_c_per_km": float(physical_slopes[2] * 1000.0),
            "feature_mean": feature_mean.tolist(),
            "feature_scale": feature_scale.tolist(),
        }
        return prediction, stats

    def _terrain_regression_blend_2d(
        self,
        values_2d: np.ndarray,
    ) -> tuple[dict[str, np.ndarray], VariogramParams, dict[str, object]]:
        """Blend ordinary kriging with a regularized lon/lat/elevation trend."""

        assert self.elevation_inputs is not None
        source_elevation = self.elevation_inputs.source
        target_elevation = self.elevation_inputs.target
        combined_valid = np.isfinite(values_2d) & np.isfinite(source_elevation)
        valid_values = np.where(combined_valid, values_2d, np.nan).astype(np.float32)
        mask = self.upscale_target_mask(valid_values) & np.isfinite(target_elevation)
        indices = self._target_indices(mask)
        diagnostics = {
            "final": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "temperature_xy": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "terrain_regression": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "blend_delta": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
            "source_elevation_xy": np.full(self.ref_grid.shape, np.nan, dtype=np.float32),
        }
        if indices.size == 0 or np.count_nonzero(combined_valid) < 4:
            params = VariogramParams(
                nugget=0.0,
                sill=finite_variance(values_2d),
                rang=float(self.config.max_search_radius or grid_resolution_m(self.ref_grid) * 3.0),
                source="empty-terrain-regression-target",
            )
            return diagnostics, params, {"training_samples": int(np.count_nonzero(combined_valid))}

        source_coords, temperature_values, tree, transformer = self._source_points_for_2d(
            values_2d,
            additional_valid=np.isfinite(source_elevation),
        )
        source_elevation_values = source_elevation[combined_valid].astype(np.float32)
        params = fit_variogram(
            source_coords,
            temperature_values,
            self.config.variogram_sample_size,
            self.config.max_search_radius,
            self.config.variogram_lags,
            self.config.random_seed,
        )
        LOGGER.info("Terrain-regression blend temperature variogram: %s", params)
        target_coords = self._target_coords(indices, transformer)
        temperature_xy, source_elevation_xy = self._krige_fields_at(
            target_coords,
            tree,
            source_coords,
            np.stack((temperature_values, source_elevation_values), axis=0),
            params,
        )
        target_elevation_values = target_elevation[indices[:, 0], indices[:, 1]]
        terrain_regression, regression_stats = self._terrain_regression_prediction(
            values_2d,
            source_elevation,
            target_elevation_values,
            indices,
        )
        blend_weight = np.float32(self.config.terrain_blend_weight)
        final = (
            (np.float32(1.0) - blend_weight) * temperature_xy
            + blend_weight * terrain_regression
        ).astype(np.float32)
        blend_delta = (final - temperature_xy).astype(np.float32)
        rows, cols = indices[:, 0], indices[:, 1]
        diagnostics["temperature_xy"][rows, cols] = temperature_xy
        diagnostics["terrain_regression"][rows, cols] = terrain_regression
        diagnostics["blend_delta"][rows, cols] = blend_delta
        diagnostics["source_elevation_xy"][rows, cols] = source_elevation_xy
        diagnostics["final"][rows, cols] = final
        return diagnostics, params, regression_stats

    def _build_terrain_regression_output(
        self,
        diagnostics: dict[str, np.ndarray],
        params: VariogramParams | list[VariogramParams],
        regression_stats: dict[str, object] | list[dict[str, object]],
        time: Optional[np.ndarray] = None,
    ) -> xr.Dataset:
        """Build baseline, regression and blended diagnostics."""

        assert self.elevation_inputs is not None
        ds = self._build_output(diagnostics["final"], "upscale", params, time=time)
        spatial_dims = (self.ref_grid.y_name, self.ref_grid.x_name)
        value_dims = spatial_dims if time is None else ("time", *spatial_dims)
        temperature_units = str(self.ds_in[self.variable].attrs.get("units", ""))
        xy_name = f"{self.variable}_xy_kriging"
        regression_name = f"{self.variable}_terrain_regression"
        delta_name = f"{self.variable}_blend_delta"
        ds[xy_name] = (value_dims, diagnostics["temperature_xy"].astype(np.float32))
        ds[regression_name] = (
            value_dims,
            diagnostics["terrain_regression"].astype(np.float32),
        )
        ds[delta_name] = (value_dims, diagnostics["blend_delta"].astype(np.float32))
        ds["source_elevation_kriging"] = (
            value_dims,
            diagnostics["source_elevation_xy"].astype(np.float32),
        )
        target_values = np.where(
            self.target_mask(), self.elevation_inputs.target, np.nan
        ).astype(np.float32)
        ds["target_elevation"] = (spatial_dims, target_values)
        ds[xy_name].attrs.update(
            {
                "long_name": "ordinary kriging baseline without terrain regression",
                "units": temperature_units,
            }
        )
        ds[regression_name].attrs.update(
            {
                "long_name": "ridge prediction from longitude, latitude and target elevation",
                "units": temperature_units,
                "features": "longitude, latitude, elevation",
            }
        )
        ds[delta_name].attrs.update(
            {
                "long_name": "difference between blended result and ordinary kriging baseline",
                "units": temperature_units,
                "formula": "blend_weight * (terrain_regression - xy_kriging)",
            }
        )
        ds["source_elevation_kriging"].attrs.update(
            {
                "long_name": "source-surface elevation interpolated with temperature kriging weights",
                "units": "m",
            }
        )
        ds["target_elevation"].attrs.update(
            {"long_name": "target DEM elevation on the reference grid", "units": "m"}
        )
        stats = [regression_stats] if isinstance(regression_stats, dict) else regression_stats
        ds.attrs.update(
            {
                "terrain_adjustment_method": (
                    "T_final = (1 - blend_weight) * T_xy_kriging + "
                    "blend_weight * ridge(longitude, latitude, target_elevation)"
                ),
                "terrain_model": "ridge-linear",
                "terrain_model_features": "longitude,latitude,elevation",
                "terrain_blend_weight": float(self.config.terrain_blend_weight),
                "terrain_ridge_alpha": float(self.config.terrain_ridge_alpha),
                "terrain_regression_stats_by_time": json.dumps(stats, ensure_ascii=False),
                "source_elevation_path": self.elevation_inputs.source_path,
                "source_elevation_variable": self.elevation_inputs.source_variable,
                "source_elevation_derivation": self.elevation_inputs.source_derivation,
                "target_elevation_path": self.elevation_inputs.target_path,
                "target_elevation_variable": self.elevation_inputs.target_variable,
            }
        )
        return ds

    def _build_elevation_output(
        self,
        diagnostics: dict[str, np.ndarray],
        params: VariogramParams | list[VariogramParams],
        clipped_counts: int | list[int],
        time: Optional[np.ndarray] = None,
    ) -> xr.Dataset:
        """Build the required diagnostic output for elevation-adjusted mode."""

        assert self.elevation_inputs is not None
        ds = self._build_output(diagnostics["final"], "upscale", params, time=time)
        spatial_dims = (self.ref_grid.y_name, self.ref_grid.x_name)
        value_dims = spatial_dims if time is None else ("time", *spatial_dims)
        temperature_units = str(self.ds_in[self.variable].attrs.get("units", ""))
        xy_name = f"{self.variable}_xy_kriging"
        correction_name = f"{self.variable}_elevation_correction"
        ds[xy_name] = (value_dims, diagnostics["temperature_xy"].astype(np.float32))
        ds[correction_name] = (value_dims, diagnostics["correction"].astype(np.float32))
        ds["source_elevation_kriging"] = (
            value_dims,
            diagnostics["source_elevation_xy"].astype(np.float32),
        )
        target_values = np.where(
            self.target_mask(),
            self.elevation_inputs.target,
            np.nan,
        ).astype(np.float32)
        ds["target_elevation"] = (spatial_dims, target_values)
        ds[xy_name].attrs.update(
            {
                "long_name": "temperature interpolated by ordinary kriging before elevation correction",
                "units": temperature_units,
            }
        )
        ds[correction_name].attrs.update(
            {
                "long_name": "temperature correction from target minus kriged source elevation",
                "units": temperature_units,
                "formula": "lapse_rate * (target_elevation - source_elevation_kriging) / 1000",
            }
        )
        ds["source_elevation_kriging"].attrs.update(
            {
                "long_name": "source-surface elevation interpolated with temperature kriging weights",
                "units": "m",
            }
        )
        ds["target_elevation"].attrs.update(
            {
                "long_name": "target DEM elevation on the reference grid",
                "units": "m",
            }
        )
        counts = [clipped_counts] if isinstance(clipped_counts, int) else clipped_counts
        ds.attrs.update(
            {
                "elevation_adjustment_method": (
                    "T_final = T_xy + lapse_rate * "
                    "(target_elevation - source_elevation_kriging) / 1000"
                ),
                "lapse_rate_c_per_km": float(self.config.lapse_rate_c_per_km),
                "source_elevation_path": self.elevation_inputs.source_path,
                "source_elevation_variable": self.elevation_inputs.source_variable,
                "source_elevation_derivation": self.elevation_inputs.source_derivation,
                "target_elevation_path": self.elevation_inputs.target_path,
                "target_elevation_variable": self.elevation_inputs.target_variable,
                "max_elevation_correction": (
                    "disabled"
                    if self.config.max_elevation_correction is None
                    else float(self.config.max_elevation_correction)
                ),
                "clipped_correction_pixel_count": int(sum(counts)),
                "clipped_correction_pixel_count_by_time": json.dumps(counts),
            }
        )
        return ds

    def _apply_coherence_correction(self, target: np.ndarray, source: np.ndarray) -> np.ndarray:
        """Force overlap-weighted fine predictions to preserve coarse-cell means."""

        corrected = target.copy()
        constraints: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]] = []

        for iy in range(self.in_grid.y.size):
            target_y, y_overlap = _interval_overlaps(
                self.ref_y_bounds,
                self.source_y_edges[iy],
                self.source_y_edges[iy + 1],
            )
            if target_y.size == 0:
                continue
            for ix in range(self.in_grid.x.size):
                src_val = source[iy, ix]
                if not np.isfinite(src_val):
                    continue
                target_x, x_overlap = _interval_overlaps(
                    self.ref_x_bounds,
                    self.source_x_edges[ix],
                    self.source_x_edges[ix + 1],
                )
                if target_x.size == 0:
                    continue
                sub = corrected[np.ix_(target_y, target_x)]
                finite = np.isfinite(sub)
                if not finite.any():
                    continue
                weights = np.outer(y_overlap, x_overlap)
                weights = np.where(finite, weights, 0.0)
                weight_sum = float(np.sum(weights))
                if weight_sum <= 0:
                    continue
                normalized = weights / weight_sum
                constraints.append((target_y, target_x, finite, normalized, float(src_val)))

        max_iterations = 100
        tolerance = 1e-6
        max_residual = math.inf
        for _ in range(max_iterations):
            max_residual = 0.0
            for target_y, target_x, finite, weights, src_val in constraints:
                sub = corrected[np.ix_(target_y, target_x)]
                residual = src_val - float(np.sum(sub[finite] * weights[finite]))
                max_residual = max(max_residual, abs(residual))
                weight_norm = float(np.sum(weights[finite] ** 2))
                if weight_norm <= 0:
                    continue
                sub[finite] += residual * weights[finite] / weight_norm
                corrected[np.ix_(target_y, target_x)] = sub
            if max_residual <= tolerance:
                break
        if constraints and max_residual > tolerance:
            LOGGER.warning(
                "Coherence correction stopped with max overlap-weighted residual %.6g",
                max_residual,
            )
        return corrected

    def upscale(self) -> xr.Dataset:
        da = self._ordered_variable()
        if self.config.upscale_observation_model == "elevation-adjusted":
            return self._upscale_elevation_adjusted(da)
        if self.config.upscale_observation_model == "terrain-regression-blend":
            return self._upscale_terrain_regression_blend(da)
        if "time" not in da.dims:
            out, params = self._upscale_2d(apply_nodata_policy(da.values, self.config))
            return self._build_output(out, "upscale", params)

        def run_slice(t: int) -> tuple[np.ndarray, VariogramParams]:
            LOGGER.info("Upscaling time slice %d / %d", t + 1, da.sizes["time"])
            arr = apply_nodata_policy(da.isel(time=t).values, self.config)
            return self._upscale_2d(arr)

        time_size = da.sizes["time"]
        stacked = np.empty((time_size, *self.ref_grid.shape), dtype=np.float32)
        params_by_time: list[VariogramParams] = []
        batch_size = min(self.config.time_batch_size, time_size)
        for start in range(0, time_size, batch_size):
            stop = min(start + batch_size, time_size)
            if self.config.parallel_time and stop - start > 1:
                tasks = [delayed(run_slice)(t) for t in range(start, stop)]
                results = compute(*tasks, scheduler=self.config.dask_scheduler)
            else:
                results = [run_slice(t) for t in range(start, stop)]
            for t, (out, params) in zip(range(start, stop), results):
                stacked[t] = out
                params_by_time.append(params)
        return self._build_output(stacked, "upscale", params_by_time, time=da["time"].values)

    def _upscale_elevation_adjusted(self, da: xr.DataArray) -> xr.Dataset:
        """Run elevation-adjusted interpolation for static or temporal temperature."""

        if "time" not in da.dims:
            diagnostics, params, clipped_count = self._elevation_adjusted_2d(
                apply_nodata_policy(da.values, self.config)
            )
            return self._build_elevation_output(diagnostics, params, clipped_count)

        def run_slice(t: int) -> tuple[dict[str, np.ndarray], VariogramParams, int]:
            LOGGER.info(
                "Elevation-adjusted upscaling time slice %d / %d",
                t + 1,
                da.sizes["time"],
            )
            values = apply_nodata_policy(da.isel(time=t).values, self.config)
            return self._elevation_adjusted_2d(values)

        time_size = da.sizes["time"]
        stacked = {
            name: np.empty((time_size, *self.ref_grid.shape), dtype=np.float32)
            for name in ("final", "temperature_xy", "correction", "source_elevation_xy")
        }
        params_by_time: list[VariogramParams] = []
        clipped_counts: list[int] = []
        batch_size = min(self.config.time_batch_size, time_size)
        for start in range(0, time_size, batch_size):
            stop = min(start + batch_size, time_size)
            if self.config.parallel_time and stop - start > 1:
                tasks = [delayed(run_slice)(t) for t in range(start, stop)]
                results = compute(*tasks, scheduler=self.config.dask_scheduler)
            else:
                results = [run_slice(t) for t in range(start, stop)]
            for t, (diagnostics, params, clipped_count) in zip(range(start, stop), results):
                for name in stacked:
                    stacked[name][t] = diagnostics[name]
                params_by_time.append(params)
                clipped_counts.append(clipped_count)
        return self._build_elevation_output(
            stacked,
            params_by_time,
            clipped_counts,
            time=da["time"].values,
        )

    def _upscale_terrain_regression_blend(self, da: xr.DataArray) -> xr.Dataset:
        """Run the kriging/regression blend for static or temporal temperature."""

        if "time" not in da.dims:
            diagnostics, params, regression_stats = self._terrain_regression_blend_2d(
                apply_nodata_policy(da.values, self.config)
            )
            return self._build_terrain_regression_output(
                diagnostics, params, regression_stats
            )

        def run_slice(
            t: int,
        ) -> tuple[dict[str, np.ndarray], VariogramParams, dict[str, object]]:
            LOGGER.info(
                "Terrain-regression blend upscaling time slice %d / %d",
                t + 1,
                da.sizes["time"],
            )
            values = apply_nodata_policy(da.isel(time=t).values, self.config)
            return self._terrain_regression_blend_2d(values)

        time_size = da.sizes["time"]
        stacked = {
            name: np.empty((time_size, *self.ref_grid.shape), dtype=np.float32)
            for name in (
                "final",
                "temperature_xy",
                "terrain_regression",
                "blend_delta",
                "source_elevation_xy",
            )
        }
        params_by_time: list[VariogramParams] = []
        stats_by_time: list[dict[str, object]] = []
        batch_size = min(self.config.time_batch_size, time_size)
        for start in range(0, time_size, batch_size):
            stop = min(start + batch_size, time_size)
            if self.config.parallel_time and stop - start > 1:
                tasks = [delayed(run_slice)(t) for t in range(start, stop)]
                results = compute(*tasks, scheduler=self.config.dask_scheduler)
            else:
                results = [run_slice(t) for t in range(start, stop)]
            for t, (diagnostics, params, regression_stats) in zip(
                range(start, stop), results
            ):
                for name in stacked:
                    stacked[name][t] = diagnostics[name]
                params_by_time.append(params)
                stats_by_time.append(regression_stats)
        return self._build_terrain_regression_output(
            stacked,
            params_by_time,
            stats_by_time,
            time=da["time"].values,
        )

    def _build_output(
        self,
        values: np.ndarray,
        mode: str,
        params: Optional[VariogramParams | list[VariogramParams]],
        time: Optional[np.ndarray] = None,
    ) -> xr.Dataset:
        out_name = f"{self.variable}_{mode}_scaled"
        coords: dict[str, tuple[str, np.ndarray] | np.ndarray] = {
            self.ref_grid.y_name: self.ref_grid.y,
            self.ref_grid.x_name: self.ref_grid.x,
        }
        dims: tuple[str, ...]
        if time is None:
            dims = (self.ref_grid.y_name, self.ref_grid.x_name)
            data = values.astype(np.float32)
        else:
            coords["time"] = time
            dims = ("time", self.ref_grid.y_name, self.ref_grid.x_name)
            data = values.astype(np.float32)
        if self.config.output_min is not None:
            data = np.maximum(data, np.float32(self.config.output_min))
        if self.config.output_max is not None:
            data = np.minimum(data, np.float32(self.config.output_max))

        ds = xr.Dataset({out_name: (dims, data)}, coords=coords, attrs=dict(self.ds_in.attrs))
        ds[out_name].attrs.update(dict(self.ds_in[self.variable].attrs))
        if self.config.output_min is not None:
            ds[out_name].attrs["output_minimum_applied"] = float(self.config.output_min)
        if self.config.output_max is not None:
            ds[out_name].attrs["output_maximum_applied"] = float(self.config.output_max)
        ds[out_name].attrs.update(
            {
                "scaling_mode": mode,
                "support_model": (
                    "block-support numerical integration; empty blocks remain NaN"
                    if mode == "downscale"
                    else (
                        "local ordinary kriging from coarse-cell center estimates"
                        if self.config.upscale_observation_model == "center"
                        else (
                            "temperature and source elevation interpolated with common ordinary-kriging "
                            "weights plus target elevation lapse-rate correction"
                            if self.config.upscale_observation_model == "elevation-adjusted"
                            else "local area-to-point kriging with optional coarse-cell coherence correction"
                        )
                    )
                ),
                "dtype_policy": "float32",
            }
        )
        ds.attrs.update(
            {
                "reference_grid": "ReferenceSpatialGrid.nc compatible",
                "kriging_config": json.dumps(asdict(self.config), ensure_ascii=False),
            }
        )
        if isinstance(params, list):
            encoded_params = json.dumps(
                [
                    {"time": str(timestamp), **asdict(slice_params)}
                    for timestamp, slice_params in zip(time, params)
                ],
                ensure_ascii=False,
            )
            ds.attrs["variogram_params_by_time"] = encoded_params
            ds[out_name].attrs["variogram_params_by_time"] = encoded_params
        elif params is not None:
            encoded_params = json.dumps(asdict(params), ensure_ascii=False)
            ds.attrs["variogram_params"] = encoded_params
            ds[out_name].attrs["variogram_params"] = encoded_params
        if "grid8_mask" in self.ds_ref:
            ds["grid8_mask"] = self._reference_mask().astype(np.float32)
        return ds


def run_file(
    input_path: Path,
    reference_path: Path,
    variable: str,
    output_path: Path,
    config: ScalingConfig,
) -> xr.Dataset:
    LOGGER.info("Opening %s and %s", input_path, reference_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with xr.open_dataset(input_path) as ds_in, xr.open_dataset(reference_path) as ds_ref:
        if config.upscale_observation_model in ELEVATION_OBSERVATION_MODELS and variable == "all":
            raise ValueError(
                f"{config.upscale_observation_model} requires one explicitly selected temperature variable; "
                "--variable all is not supported."
            )
        variables = resolve_variables(ds_in, variable)
        elevation_inputs = (
            load_elevation_inputs(config, ds_in, ds_ref)
            if config.upscale_observation_model in ELEVATION_OBSERVATION_MODELS
            else None
        )
        outputs = []
        geometry_cache = _SpatialCache()
        for var_name in variables:
            LOGGER.info("Scaling variable %s (%d / %d)", var_name, len(outputs) + 1, len(variables))
            scaler = SupportAwareKrigingScaler(
                ds_in,
                ds_ref,
                var_name,
                config,
                geometry_cache=geometry_cache,
                elevation_inputs=elevation_inputs,
            )
            outputs.append(scaler.run())
        out = xr.merge(outputs, compat="override")
        out.attrs.update(outputs[-1].attrs)
        if len(outputs) > 1:
            params_by_variable = {}
            for var_name, variable_output in zip(variables, outputs):
                if "variogram_params_by_time" in variable_output.attrs:
                    params_by_variable[var_name] = json.loads(
                        variable_output.attrs["variogram_params_by_time"]
                    )
                elif "variogram_params" in variable_output.attrs:
                    params_by_variable[var_name] = json.loads(
                        variable_output.attrs["variogram_params"]
                    )
            out.attrs.pop("variogram_params", None)
            out.attrs.pop("variogram_params_by_time", None)
            out.attrs["variogram_params_by_variable"] = json.dumps(
                params_by_variable,
                ensure_ascii=False,
            )
        encoding = {}
        for name, da in out.data_vars.items():
            chunksizes = tuple(
                min(8, da.sizes[dim]) if dim == "time" else min(256, da.sizes[dim])
                for dim in da.dims
            )
            encoding[name] = {
                "dtype": "float32",
                "zlib": True,
                "complevel": 4,
                "shuffle": True,
                "chunksizes": chunksizes,
            }
        tmp_output_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
        out.to_netcdf(tmp_output_path, encoding=encoding)
        os.replace(tmp_output_path, output_path)
        LOGGER.info("Saved %s", output_path)
        return out


def resolve_variables(ds: xr.Dataset, variable: str) -> list[str]:
    """Resolve a variable selector into spatial data variables."""

    if variable != "all":
        if variable not in ds.data_vars:
            raise KeyError(f"Variable {variable!r} is absent from input dataset.")
        return [variable]

    grid = detect_grid(ds)
    selected = []
    spatial_dims = {grid.x_name, grid.y_name}
    for name, da in ds.data_vars.items():
        dims = set(da.dims)
        supported_dims = dims == spatial_dims or dims == spatial_dims | {"time"}
        if supported_dims and np.issubdtype(da.dtype, np.number):
            selected.append(name)
    if not selected:
        raise ValueError("No numeric spatial variables found for --variable all.")
    return selected


def _int_at_least(minimum: int, label: str):
    def parse(value: str) -> int:
        parsed = int(value)
        if parsed < minimum:
            raise argparse.ArgumentTypeError(f"{label} must be at least {minimum}")
        return parsed

    return parse


def _positive_float(label: str):
    def parse(value: str) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError(f"{label} must be positive")
        return parsed

    return parse


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Support-aware geospatial kriging scaler")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--reference", default=Path("ReferenceSpatialGrid.nc"), type=Path)
    parser.add_argument("--variable", required=True, help="Variable name or 'all' for all numeric spatial variables")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["auto", "upscale", "downscale"], default="auto")
    parser.add_argument("--transform", choices=["utm", "ecef"], default="utm")
    parser.add_argument("--k-neighbors", type=_int_at_least(3, "k-neighbors"), default=16)
    parser.add_argument("--max-search-radius", type=_positive_float("max-search-radius"), default=None)
    parser.add_argument(
        "--support-points-per-axis",
        type=_int_at_least(1, "support-points-per-axis"),
        default=3,
    )
    parser.add_argument(
        "--upscale-observation-model",
        choices=["center", "area", "elevation-adjusted", "terrain-regression-blend"],
        default="center",
        help=(
            "Interpret coarse values as center estimates (default), areal block averages, "
            "temperature adjusted by source/target elevation difference, or a 50/50 "
            "blend of kriging and regularized lat/lon/elevation regression"
        ),
    )
    parser.add_argument("--target-elevation", type=Path, default=None)
    parser.add_argument("--target-elevation-variable", default=None)
    parser.add_argument("--source-elevation", type=Path, default=None)
    parser.add_argument("--source-elevation-variable", default=None)
    parser.add_argument("--derive-source-elevation-from-target", action="store_true")
    parser.add_argument("--lapse-rate-c-per-km", type=float, default=-6.5)
    parser.add_argument(
        "--max-elevation-correction",
        type=_positive_float("max-elevation-correction"),
        default=None,
    )
    parser.add_argument("--terrain-blend-weight", type=float, default=0.5)
    parser.add_argument("--terrain-ridge-alpha", type=float, default=1.0)
    parser.add_argument("--output-min", type=float, default=None)
    parser.add_argument("--output-max", type=float, default=None)
    parser.add_argument(
        "--max-targets",
        type=_int_at_least(0, "max-targets"),
        default=None,
        help="Limit target cells for notebook/demo runs",
    )
    parser.add_argument("--no-target-mask", action="store_true")
    parser.add_argument("--no-coherence-correction", action="store_true")
    parser.add_argument(
        "--fill-missing-downscale",
        action="store_true",
        help="Interpolate empty downscale target blocks with local kriging; default leaves them as NaN",
    )
    parser.add_argument("--nodata-below", type=float, default=None, help="Treat values <= threshold as missing data")
    parser.add_argument("--nodata-above", type=float, default=None, help="Treat values >= threshold as missing data")
    parser.add_argument(
        "--allow-upscale-extrapolation",
        action="store_true",
        help="Allow predictions beyond source coverage; default keeps unsupported reference cells as NaN",
    )
    parser.add_argument("--dask-scheduler", default="threads", choices=["threads", "processes", "synchronous"])
    parser.add_argument("--no-parallel-time", action="store_true")
    parser.add_argument(
        "--time-batch-size",
        type=_int_at_least(1, "time-batch-size"),
        default=8,
        help="Maximum number of time slices submitted to Dask at once",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = ScalingConfig(
        mode=args.mode,
        transform_method=args.transform,
        k_neighbors=args.k_neighbors,
        max_search_radius=args.max_search_radius,
        support_points_per_axis=args.support_points_per_axis,
        upscale_observation_model=args.upscale_observation_model,
        target_elevation_path=(str(args.target_elevation) if args.target_elevation else None),
        target_elevation_variable=args.target_elevation_variable,
        source_elevation_path=(str(args.source_elevation) if args.source_elevation else None),
        source_elevation_variable=args.source_elevation_variable,
        derive_source_elevation_from_target=args.derive_source_elevation_from_target,
        lapse_rate_c_per_km=args.lapse_rate_c_per_km,
        max_elevation_correction=args.max_elevation_correction,
        terrain_blend_weight=args.terrain_blend_weight,
        terrain_ridge_alpha=args.terrain_ridge_alpha,
        output_min=args.output_min,
        output_max=args.output_max,
        use_target_mask=not args.no_target_mask,
        coherence_correction=not args.no_coherence_correction,
        max_targets=args.max_targets,
        dask_scheduler=args.dask_scheduler,
        parallel_time=not args.no_parallel_time,
        time_batch_size=args.time_batch_size,
        fill_missing_downscale=args.fill_missing_downscale,
        nodata_below=args.nodata_below,
        nodata_above=args.nodata_above,
        restrict_upscale_to_source_footprint=not args.allow_upscale_extrapolation,
    )
    run_file(args.input, args.reference, args.variable, args.output, config)


if __name__ == "__main__":
    main()
