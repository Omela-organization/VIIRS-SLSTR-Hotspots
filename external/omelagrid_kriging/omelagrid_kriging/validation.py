"""Validation helpers for the kriging scaler."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import xarray as xr
from pyproj import Geod

from .core import CoordinateTransformer, detect_grid


@dataclass(frozen=True)
class CoherenceReport:
    covered_blocks: int
    max_abs_residual: float
    residuals: list[float]


def ecef_distance_error(
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
    earth_radius_m: float = 6_371_000.0,
) -> float:
    """Relative ECEF chord-vs-geodesic error for local distances."""

    transformer = CoordinateTransformer(method="ecef")
    coords = transformer.transform(np.array([lon1, lon2]), np.array([lat1, lat2]))
    chord = float(np.linalg.norm(coords[0] - coords[1]))
    geod = Geod(ellps="WGS84")
    _, _, geodesic = geod.inv(lon1, lat1, lon2, lat2)
    if geodesic == 0:
        return 0.0
    return abs(chord - geodesic) / geodesic


def coord_edges(coord: np.ndarray) -> np.ndarray:
    coord = np.asarray(coord, dtype=float)
    if coord.size == 1:
        return np.array([coord[0] - 0.5, coord[0] + 0.5])
    mid = (coord[:-1] + coord[1:]) / 2
    return np.r_[coord[0] - (mid[0] - coord[0]), mid, coord[-1] + (coord[-1] - mid[-1])]


def coherence_report(source: xr.DataArray, prediction: xr.DataArray) -> CoherenceReport:
    """Compare coarse source values to mean fine predictions inside each coarse cell."""

    src_grid = detect_grid(source.to_dataset(name="source"))
    pred_grid = detect_grid(prediction.to_dataset(name="prediction"))
    x_edges = coord_edges(src_grid.x)
    y_edges = coord_edges(src_grid.y)
    residuals: list[float] = []

    src_values = source.values
    if source.ndim != 2:
        raise ValueError("coherence_report expects a 2D source slice.")

    for iy in range(src_grid.y.size):
        ymin, ymax = sorted([y_edges[iy], y_edges[iy + 1]])
        y_mask = (pred_grid.y >= ymin) & (pred_grid.y < ymax)
        for ix in range(src_grid.x.size):
            src_val = src_values[iy, ix]
            if not np.isfinite(src_val):
                continue
            xmin, xmax = sorted([x_edges[ix], x_edges[ix + 1]])
            x_mask = (pred_grid.x >= xmin) & (pred_grid.x < xmax)
            sub = prediction.values[np.ix_(y_mask, x_mask)]
            if np.isfinite(sub).any():
                residuals.append(float(np.nanmean(sub) - src_val))

    max_abs = max((abs(v) for v in residuals), default=math.nan)
    return CoherenceReport(len(residuals), float(max_abs), residuals)


def void_propagation_ok(prediction: xr.DataArray, max_allowed_fill_ratio: float = 0.0) -> bool:
    """Check that masked/outside areas remain mostly empty.

    This helper is intentionally generic. In tests we pass arrays where the
    expected void is encoded as NaN in the prediction itself.
    """

    values = prediction.values
    if values.size == 0:
        return True
    fill_ratio = float(np.isfinite(values).sum() / values.size)
    return fill_ratio <= max_allowed_fill_ratio
