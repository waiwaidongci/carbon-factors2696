import redis
from src.config.settings import REDIS_URL

def enqueue(job, item_id):
    redis.Redis.from_url(REDIS_URL).rpush(job, str(item_id))

def enqueue_many(job, ids):
    if not ids:
        return
    client = redis.Redis.from_url(REDIS_URL)
    pipeline = client.pipeline()
    for item_id in ids:
        pipeline.rpush(job, str(item_id))
    pipeline.execute()
