"""Скачивание мини-набора тестовых пролётов VIIRS (HS-03)."""
from pathlib import Path

import earthaccess

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_SET_DIR = PROJECT_ROOT / "data" / "test_sets" / "mini"
TEST_SET_DIR.mkdir(parents=True, exist_ok=True)

# Приморский край
PRIMORSKY_BBOX = (130.0, 42.0, 140.0, 50.0)

# Конфигурация сценариев — итого 16 пролётов
SCENARIOS = [
    {"name": "fire",  "product": "VNP14IMG", "dates": ("2024-07-01", "2024-08-31"), "count": 3},
    {"name": "clear", "product": "VNP09GA",  "dates": ("2024-06-01", "2024-06-30"), "count": 3},
    {"name": "snow",  "product": "VNP10A1",  "dates": ("2024-01-01", "2024-02-29"), "count": 2},
    {"name": "cloud", "product": "VNP09GA",  "dates": ("2024-07-15", "2024-07-31"), "count": 3},
    {"name": "smoke", "product": "VNP14IMG", "dates": ("2024-08-01", "2024-08-31"), "count": 3},
    {"name": "swath_edge", "product": "VNP09GA", "dates": ("2024-06-15", "2024-06-30"), "count": 2},
]


def login():
    print("Авторизация в NASA Earthdata...")
    auth = earthaccess.login(strategy="interactive")
    print("Авторизация успешна")
    return auth


def search_and_download(scenario):
    output_dir = TEST_SET_DIR / scenario["name"]
    output_dir.mkdir(exist_ok=True)

    # Проверка уже скачанных файлов
    existing = list(output_dir.glob("*.h5")) + list(output_dir.glob("*.nc"))
    if len(existing) >= scenario["count"]:
        print(f"\n[{scenario['name']}] Уже скачано: {len(existing)} файлов. Пропускаю.")
        return

    print(f"\n=== {scenario['name'].upper()} ({scenario['product']}) ===")
    print(f"Даты: {scenario['dates']}")
    print(f"Нужно гранул: {scenario['count']}")

    try:
        results = earthaccess.search_data(
            short_name=scenario["product"],
            temporal=scenario["dates"],
            bounding_box=PRIMORSKY_BBOX,
            count=scenario["count"],
        )
    except Exception as e:
        print(f"  ✗ Ошибка поиска: {e}")
        return

    if not results:
        print("  Гранулы не найдены")
        return

    print(f"  Найдено гранул: {len(results)}. Скачиваю...")
    try:
        files = earthaccess.download(results, str(output_dir))
        print(f"  ✓ Скачано файлов: {len(files)}")
    except Exception as e:
        print(f"  ✗ Ошибка скачивания: {e}")


def main():
    login()
    for scenario in SCENARIOS:
        search_and_download(scenario)

    print("\n=== Итог ===")
    total = 0
    for subdir in sorted(TEST_SET_DIR.iterdir()):
        if subdir.is_dir():
            files = list(subdir.glob("*.h5")) + list(subdir.glob("*.nc"))
            total_mb = sum(f.stat().st_size for f in files) / 1e6
            print(f"  {subdir.name}/: {len(files)} файлов ({total_mb:.1f} МБ)")
            total += len(files)
    print(f"\nВсего пролётов: {total}")


if __name__ == "__main__":
    main()