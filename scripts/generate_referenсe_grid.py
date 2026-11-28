"""Генерация эталонной сетки 240 м для Приморского края (HS-03)."""
import numpy as np
import xarray as xr
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GRID_DIR = PROJECT_ROOT / "data" / "external" / "grids"
GRID_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = GRID_DIR / "ReferenceSpatialGrid.nc"


def main():
    # Границы Приморского края
    lat_min, lat_max = 42.0, 50.0
    lon_min, lon_max = 130.0, 140.0

    # Параметры сетки
    cell_size_m = 240
    mid_lat = 46.0  # Центр Приморья для расчёта шага долготы

    # Метры в одном градусе
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * np.cos(np.radians(mid_lat))

    # Шаги в градусах
    step_lat = cell_size_m / m_per_deg_lat
    step_lon = cell_size_m / m_per_deg_lon

    print(f"Шаг широты: {step_lat:.6f}° ({cell_size_m} м)")
    print(f"Шаг долготы: {step_lon:.6f}° ({cell_size_m} м на {mid_lat}°N)")

    # Координаты (y — широта от севера к югу, как в оригинальном файле)
    lat = np.arange(lat_max, lat_min, -step_lat)
    lon = np.arange(lon_min, lon_max, step_lon)

    print(f"Размер сетки: {len(lat)} × {len(lon)} = {len(lat) * len(lon):,} ячеек")

    # Маска: 1 = активная ячейка (для разработки заполняем весь прямоугольник)
    mask = np.ones((len(lat), len(lon)), dtype=np.int8)

    # Создаём датасет с теми же атрибутами, что ожидает кригинг-модель
    ds = xr.Dataset(
        {"grid8_mask": (("y", "x"), mask)},
        coords={"y": lat, "x": lon},
        attrs={
            "description": "Reference grid 240m for Primorsky Krai",
            "crs": "EPSG:4326",
            "cell_size_m": np.int32(240),
            "hls_pixel_m": np.int32(30),
            "grid_pixels": np.int32(8),
            "step_lat_deg": np.float64(step_lat),
            "step_lon_deg": np.float64(step_lon),
            "mid_lat_ref": np.float64(mid_lat),
        }
    )
    ds["grid8_mask"].attrs = {
        "long_name": "Grid mask: 1=active (int8), fill(-128)=outside"
    }

    # Сохраняем
    ds.to_netcdf(OUTPUT_PATH)
    print(f"\nСохранено: {OUTPUT_PATH}")
    print(f"Размер файла: {OUTPUT_PATH.stat().st_size / 1e6:.2f} МБ")


if __name__ == "__main__":
    main()