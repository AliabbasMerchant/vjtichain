import logging
from datetime import datetime

from . import constants as consts

for name in ['werkzeug', 'bottle', 'waitress']:
    log = logging.getLogger(name)
    log.setLevel(logging.CRITICAL)
    log.disabled = True

logger = logging.getLogger("somechain")
logger.propagate = False

formatter = logging.Formatter("%(asctime)s %(levelname)-10s %(message)s", consts.DATE_FORMAT)
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(consts.LOG_DIRECTORY + datetime.strftime(datetime.now(), consts.DATE_FORMAT) + ".log")
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.DEBUG)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(consts.LOG_LEVEL)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)
