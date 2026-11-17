import time
import traceback
from pathlib import Path
import redis
from src.config.database import Base, engine, Session
from src.config.settings import REDIS_URL
from src.models import identity, source, run, errata  # noqa: F401  (register tables)
from src.services import compute, errata as errata_service
from src.services.delivery import tick

Base.metadata.create_all(engine)
try:
    from src.config.migrations import upgrade
    upgrade()
except Exception:
    traceback.print_exc()
r = redis.Redis.from_url(REDIS_URL)


def drain(job, handler):
    while True:
        item = r.lpop(job)
        if item is None:
            return
        try:
            with Session() as db:
                handler(db, int(item))
        except Exception:
            traceback.print_exc()


def main():
    recovered_at = 0.0
    while True:
        Path('/tmp/worker-heartbeat').write_text(str(time.time()))
        try:
            drain('report-jobs', compute.process_run)
            drain('errata-jobs', errata_service.process_item)
            if time.time() - recovered_at > 30:
                recovered_at = time.time()
                with Session() as db:
                    errata_service.recover_items(db)
                    compute.recover(db)
            with Session() as db:
                tick(db)
        except Exception:
            traceback.print_exc()
        time.sleep(1)


if __name__ == '__main__':
    main()
