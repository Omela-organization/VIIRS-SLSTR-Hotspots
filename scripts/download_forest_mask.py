"""Лесная маска Hansen GFC 30 м, тайл 50N_130E (Приморье). HS-03, Д-6."""
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MASK_DIR = PROJECT_ROOT / "data" / "external" / "masks"
MASK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = MASK_DIR / "forest_mask_30m.tif"

# Тайл 50N_130E: верхний левый угол 50N, 130E -> покрывает 40-50N, 130-140E (Приморье)
COG_URL = (
    "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2019-v1.7/"
    "Hansen_GFC-2019-v1.7_treecover2000_50N_130E.tif"
)

# Границы окна Приморья (запала, юг, восток, север)
AOI = (130.0, 42.0, 140.0, 50.0)

# FAO-порог: покрытие лесом >= 30% = лес
TREECOVER_THRESHOLD = 30


def main():
    print(f"Открываю COG: {COG_URL}")
    with rasterio.open(COG_URL) as src:
        print(f"  CRS: {src.crs}")
        print(f"  Bounds тайла: {src.bounds}")
        print(f"  Полный размер: {src.width} x {src.height}")

        window = from_bounds(*AOI, src.transform)
        print(f"  Окно: col_off={int(window.col_off)}, row_off={int(window.row_off)}, "
              f"width={int(window.width)}, height={int(window.height)}")

        treecover = src.read(1, window=window)
        out_transform = src.window_transform(window)
        out_crs = src.crs

    valid = treecover != 255
    forest = np.where(valid & (treecover >= TREECOVER_THRESHOLD), 1, 0).astype(np.uint8)
    forest[~valid] = 255  # сохраняем nodata

    with rasterio.open(
        OUTPUT_PATH, "w",
        driver="GTiff",
        height=forest.shape[0],
        width=forest.shape[1],
        count=1,
        dtype="uint8",
        crs=out_crs,
        transform=out_transform,
        compress="lzw",
        nodata=255,
    ) as dst:
        dst.write(forest, 1)

    total = forest.size
    forest_px = int(np.sum(forest == 1))
    nodata_px = int(np.sum(forest == 255))
    print(f"\nЗаписано: {OUTPUT_PATH}")
    print(f"  Размер: {forest.shape[1]} x {forest.shape[0]} пикселей @ 30 м")
    print(f"  Лес: {forest_px / total * 100:.1f}%")
    print(f"  Не лес: {(total - forest_px - nodata_px) / total * 100:.1f}%")
    print(f"  Нет данных: {nodata_px / total * 100:.1f}%")


if __name__ == "__main__":
    main()