import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from shorts_factory.state import get_engine


@pytest.fixture
def db_session(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_FACTORY_DATA_DIR", str(tmp_path / "data"))
    yield
