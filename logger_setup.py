# logger_setup.py
import logging
import sys


def setup_logger():
    """Configures a logger to output to both console and a file."""
    # Create logger
    logger = logging.getLogger("MT5_EA")
    logger.setLevel(logging.INFO)

    # Create handlers
    # Console handler
    c_handler = logging.StreamHandler(sys.stdout)
    c_handler.setLevel(logging.INFO)

    # File handler
    f_handler = logging.FileHandler('ea_activity.log', mode='a')
    f_handler.setLevel(logging.INFO)

    # Create formatters and add it to handlers
    log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    c_handler.setFormatter(log_format)
    f_handler.setFormatter(log_format)

    # Add handlers to the logger
    if not logger.handlers:
        logger.addHandler(c_handler)
        logger.addHandler(f_handler)

    return logger


# Create a single instance of the logger to be imported by other modules
log = setup_logger()