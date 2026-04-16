from db import make_db
from schema import init_schema
import logging

logger = logging.getLogger("orchestrator")

db = make_db(logger)
init_schema(db, logger)
