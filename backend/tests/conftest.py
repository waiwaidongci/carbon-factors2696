import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PROJECT_JSON'] = str(ROOT / 'project.json')
os.environ['JWT_SECRET'] = 'test-secret'
DB_PATH = tempfile.mkdtemp(prefix='errata-test-') + '/test.db'
os.environ['DATABASE_URL'] = 'sqlite:///' + DB_PATH
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'

import pytest
from fastapi.testclient import TestClient
from src.config.database import Base, engine, Session
from src.models.identity import User
from src.models.source import Policy
from src.utils.security import password_hash
from src.services import queue
from src.main import app


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@demo.local', role=role, password=password_hash('Demo2026!')))
        db.commit()
    yield


@pytest.fixture
def enqueued(monkeypatch):
    jobs = {'report-jobs': [], 'errata-jobs': []}

    def fake(job, ids):
        jobs[job].extend(ids)

    def fake_one(job, item_id):
        jobs[job].append(item_id)

    monkeypatch.setattr(queue, 'enqueue_many', fake)
    monkeypatch.setattr(queue, 'enqueue', fake_one)
    return jobs


@pytest.fixture
def client(enqueued):
    with TestClient(app) as c:
        yield c


def login(client, role='admin'):
    r = client.post('/auth/login', json={'email': role + '@demo.local', 'password': 'Demo2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def policy(version, electricity=0.57, scope='合成研究用活动量；仅覆盖电力、柴油、通勤，不是组织完整碳盘查'):
    return {'version': version, 'scope': scope,
            'factors': {'电力': {'unit': 'kWh', 'kg_per_unit': electricity},
                        '柴油': {'unit': 'L', 'kg_per_unit': 2.68},
                        '通勤': {'unit': 'km', 'kg_per_unit': 0.12}}}
