from pydantic import BaseModel,Field
class Login(BaseModel): email:str; password:str
class DataInput(BaseModel): data:dict
class RunInput(BaseModel): name:str=Field(min_length=2,max_length=120); policy_id:int; period:str='全部'
class ReviewInput(BaseModel): note:str=Field(min_length=4,max_length=2000)
class ScheduleInput(BaseModel): name:str=Field(min_length=2,max_length=120); run_id:int; interval_seconds:int=Field(ge=5,le=86400)
class ErrataInput(BaseModel): name:str=Field(min_length=2,max_length=120); old_policy_id:int; new_policy_id:int; activities:list[str]=Field(min_length=1,max_length=100)
class ErrataReviewInput(BaseModel): note:str=Field(min_length=4,max_length=2000)
