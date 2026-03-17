"""
結構化日誌模組
"""
import logging
import logging.handlers
from pathlib import Path
from rich.logging import RichHandler
from rich.console import Console

console = Console()


def setup_logger(name: str, config: dict, log_file: str = None) -> logging.Logger:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_file or log_cfg.get("file", "logs/trading.log")
    max_bytes = log_cfg.get("max_bytes", 10_485_760)
    backup_count = log_cfg.get("backup_count", 5)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    rich_handler = RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)
    rich_handler.setLevel(level)
    logger.addHandler(rich_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(file_handler)

    return logger
