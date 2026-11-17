"""轻量向前兼容迁移：为已有库补上勘误功能新增的可空列。

SQLAlchemy create_all 只建缺失的表，不会给已存在的表加列；升级旧演示库时在启动阶段补齐。
"""
from sqlalchemy import inspect, text
from src.config.database import engine

COLUMNS = {
    'carbon_runs': [('restated_from', 'INTEGER'), ('errata_batch_id', 'INTEGER'),
                    ('started', 'FLOAT'), ('queued_at', 'FLOAT')],
    'delivery_schedules': [('generation', 'INTEGER'), ('last_errata_batch_id', 'INTEGER')],
    'deliveries': [('errata_batch_id', 'INTEGER')],
}


def upgrade():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in COLUMNS.items():
            if table not in tables:
                continue
            existing = {c['name'] for c in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in existing:
                    continue
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
