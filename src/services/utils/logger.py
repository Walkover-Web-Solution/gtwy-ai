import logging
import sys

# logging.basicConfig(format='%(levelname)s - %(message)s')
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s", stream=sys.stdout)


def get_logger() -> logging.Logger:
    logger = logging.getLogger()
    return logger


logger = get_logger()
