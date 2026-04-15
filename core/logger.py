"""H.E.L.I.O.S. Logger — estruturado, colorido no terminal, ficheiro helios.log"""

import logging
import logging.handlers
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "helios.log"

COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
    "RESET":    "\033[0m",
}


class ColorFormatter(logging.Formatter):
    FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        color  = COLORS.get(record.levelname, COLORS["RESET"])
        reset  = COLORS["RESET"]
        record.levelname = f"{color}{record.levelname}{reset}"
        return logging.Formatter(self.FMT, datefmt="%H:%M:%S").format(record)


def setup_logger(level: int = logging.INFO):
    root = logging.getLogger("helios")
    root.setLevel(level)

    if root.handlers:
        return  # já configurado

    # Terminal (colorido)
    sh = logging.StreamHandler()
    sh.setFormatter(ColorFormatter())
    root.addHandler(sh)

    # Ficheiro rotativo (5 MB × 3 backups)
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)
