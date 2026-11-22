"""JSON-логирование через structlog (НФТ-08)."""
import logging
import sys
import structlog


def setup_logging(level: str = "INFO") -> None:
    """Инициализирует JSON-логирование для всего приложения."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "viirs_slstr_hotspots"):
    """Возвращает настроенный логгер для использования в модулях."""
    return structlog.get_logger(name)