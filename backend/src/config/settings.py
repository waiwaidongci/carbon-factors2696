import os,json
from pathlib import Path
def _config_path():
 candidates=[Path(os.getenv('PROJECT_JSON','')),Path('/app/project.json'),Path(__file__).resolve().parents[2]/'project.json']
 for path in candidates:
  if path and path.is_file(): return path
 raise FileNotFoundError('project.json 不存在，可用 PROJECT_JSON 指定')
CONFIG=json.loads(_config_path().read_text())
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
SECRET=os.environ['JWT_SECRET']
