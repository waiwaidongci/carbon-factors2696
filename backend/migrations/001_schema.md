# 初始结构

SQLAlchemy启动创建以下业务表：activity_periods, activities, emission_factors, carbon_runs, delivery_schedules, deliveries、users、audits。Source专属JSON字段由对应validation严格校验；Policy为不可覆盖的版本记录；Run保存source/policy冻结快照；Period仅用于已有期间选择器，不计入核心复杂度。已有数据库升级需先备份，后续字段演进应以独立迁移执行。

## 因子勘误成组重述（附加迁移）
新增 errata_batches（勘误批次：旧/新因子版本、受影响活动、状态 calculating/review/reviewed/switched/failed）、errata_items（每个原报告的重述明细，唯一约束 batch_id+source_run_id）、errata_schedule_targets（批次关联的交付计划、旧报告、期望代次）。carbon_runs 增加 restated_from、errata_batch_id、started、queued_at；delivery_schedules 增加 generation、last_errata_batch_id；deliveries 增加 errata_batch_id，payload 增加 run_id/errata_batch_id。已有数据库升级需先备份；`src/config/migrations.py` 在 backend/worker 启动时幂等地为已有表补建上述可空列（`ALTER TABLE ... ADD COLUMN`），新表仍由 SQLAlchemy create_all 创建。
