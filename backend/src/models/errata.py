from sqlalchemy import Column,Integer,String,JSON,Text,Float,Boolean,UniqueConstraint
from src.config.database import Base
class ErrataBatch(Base):
 __tablename__='errata_batches'
 id=Column(Integer,primary_key=True); name=Column(String(120)); old_policy_id=Column(Integer); new_policy_id=Column(Integer); activities=Column(JSON); status=Column(String(20)); created=Column(Float); created_by=Column(String(120)); review=Column(Text,default=''); reviewed_by=Column(String(120)); reviewed_at=Column(Float); switched_at=Column(Float); error=Column(Text)
class ErrataItem(Base):
 __tablename__='errata_items'
 __table_args__=(UniqueConstraint('batch_id','source_run_id',name='uq_errata_item_source'),)
 id=Column(Integer,primary_key=True); batch_id=Column(Integer); source_run_id=Column(Integer); run_id=Column(Integer); status=Column(String(20)); error=Column(Text); attempts=Column(Integer,default=0); queued_at=Column(Float); started=Column(Float)
class ErrataScheduleTarget(Base):
 __tablename__='errata_schedule_targets'
 __table_args__=(UniqueConstraint('batch_id','schedule_id',name='uq_errata_target_schedule'),)
 id=Column(Integer,primary_key=True); batch_id=Column(Integer); schedule_id=Column(Integer); old_run_id=Column(Integer); expected_generation=Column(Integer); new_run_id=Column(Integer); switched=Column(Boolean,default=False)
