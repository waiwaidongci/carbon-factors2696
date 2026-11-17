from sqlalchemy import Column,Integer,String,JSON,Text,Float,Boolean
from src.config.database import Base
class Run(Base):
 __tablename__='carbon_runs'
 id=Column(Integer,primary_key=True); name=Column(String(120)); status=Column(String(20)); snapshot=Column(JSON); checksum=Column(String(64)); result=Column(JSON); error=Column(Text); created=Column(Float); approved=Column(Boolean,default=False); review=Column(Text,default=''); restated_from=Column(Integer); errata_batch_id=Column(Integer); started=Column(Float); queued_at=Column(Float)
class Schedule(Base):
 __tablename__='delivery_schedules'
 id=Column(Integer,primary_key=True); name=Column(String(120)); run_id=Column(Integer); interval_seconds=Column(Integer); next_at=Column(Float); enabled=Column(Boolean,default=True); generation=Column(Integer,default=0); last_errata_batch_id=Column(Integer)
class Delivery(Base):
 __tablename__='deliveries'
 id=Column(Integer,primary_key=True); schedule_id=Column(Integer); run_id=Column(Integer); due_key=Column(String(120),unique=True); created=Column(Float); payload=Column(JSON); errata_batch_id=Column(Integer)
