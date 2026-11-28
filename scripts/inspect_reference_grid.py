"""Визуализация эталонной сетки 240 м (HS-03)."""
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import xarray as xr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GRID_PATH = PROJECT_ROOT / "external" / "omelagrid_kriging" / "ReferenceSpatialGrid.nc"

ds = xr.open_dataset(GRID_PATH)
y = ds.y.values
x = ds.x.values
mask = ds.grid8_mask.values

print(f"CRS: {ds.attrs.get('crs')}")
print(f"cell_size_m: {ds.attrs.get('cell_size_m')}")
print(f"Покрытие: {y.min():.3f}–{y.max():.3f}°N, {x.min():.3f}–{x.max():.3f}°E")
print(f"Размер: {len(y)} × {len(x)} = {len(y)*len(x):,} ячеек")
print(f"Активных ячеек: {int((mask == 1).sum()):,}")

fig, ax = plt.subplots(1, 2, figsize=(14, 6))

# Левая: карта покрытия сетки
ax[0].imshow(mask == 1, extent=[x.min(), x.max(), y.min(), y.max()],
             origin="upper", cmap="gray", vmin=0, vmax=1)
ax[0].set_title(f"Сетка Никиты: {len(y)}×{len(x)} ячеек")
ax[0].set_xlabel("Долгота, °E")
ax[0].set_ylabel("Широта, °N")
ax[0].grid(True, alpha=0.3)

# Правая: сравнение с Приморьем
ax[1].imshow(mask == 1, extent=[x.min(), x.max(), y.min(), y.max()],
             origin="upper", cmap="gray", vmin=0, vmax=1)
# Рамка Приморского края
ax[1].add_patch(plt.Rectangle((130, 42), 10, 8, fill=False,
                              edgecolor="red", linewidth=2, linestyle="--"))
ax[1].set_xlim(129.5, 140.5)
ax[1].set_ylim(41.5, 50.5)
ax[1].set_title("Сетка Никиты (серый) vs Приморье (красная рамка)")
ax[1].set_xlabel("Долгота, °E")
ax[1].grid(True, alpha=0.3)

plt.tight_layout()
out = PROJECT_ROOT / "docs" / "figures" / "reference_grid_coverage.png"
out.parent.mkdir(parents=True, exist_ok=True)
plt.savefig(out, dpi=150)
print(f"Сохранено: {out}")
plt.show()