import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix='sandbox-api-'))
os.environ.setdefault('DATABASE_URL', f'sqlite:///{ROOT / "api.db"}')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('SEED_DEMO', 'true')

import pytest
from fastapi.testclient import TestClient

from src.config.database import Base, Session, engine
from src.main import app
from src.models.entities import User
from src.services.sandbox_service import seed_orders
from src.utils.security import hash_password


@pytest.fixture(scope='session', autouse=True)
def schema():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'operator', 'viewer'):
            email = f'{role}@hengshi.local'
            if db.query(User).filter(User.email == email).first() is None:
                db.add(User(email=email, role=role, password_hash=hash_password('Drill2026!')))
        seed_orders(db)
        db.commit()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def login(client, role='operator'):
    response = client.post('/auth/login', json={'email': f'{role}@hengshi.local',
                                                'password': 'Drill2026!'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def operator(client):
    headers = login(client, 'operator')
    client.post('/sandbox/reset', headers=login(client, 'admin'))
    return headers


@pytest.fixture()
def viewer(client):
    return login(client, 'viewer')


@pytest.fixture()
def admin(client):
    return login(client, 'admin')
