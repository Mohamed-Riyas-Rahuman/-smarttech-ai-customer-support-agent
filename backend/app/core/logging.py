"""Structured logging configuration with secret redaction."""
import logging
import re
import sys

# Patterns to sanitize
SENSITIVE_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.]+", re.IGNORECASE),
    re.compile(r"('password':\s*')[^']*", re.IGNORECASE),
    re.compile(r'("password":\s*")[^"]*', re.IGNORECASE),
    re.compile(r"(sk-or-v1-[a-zA-Z0-9]+|gsk_[a-zA-Z0-9]+)", re.IGNORECASE),
    re.compile(r"(postgresql\+psycopg://[^:]+:)([^@]+)(@)", re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern in SENSITIVE_PATTERNS:
            if "postgresql" in pattern.pattern:
                msg = pattern.sub(r"\1****\3", msg)
            elif "Bearer" in pattern.pattern:
                msg = pattern.sub(r"\1[REDACTED]", msg)
            elif "gsk_" in pattern.pattern:
                msg = pattern.sub(r"[LLM_KEY_REDACTED]", msg)
            else:
                msg = pattern.sub(r"\1****", msg)
        return msg


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("smarttech")
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = RedactingFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = setup_logging()
