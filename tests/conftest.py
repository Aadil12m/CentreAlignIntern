import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sim_env.app import ERP_PASSWORD, SimServer  # noqa: E402
from sim_env.seed import seed  # noqa: E402


@pytest.fixture
def sim(tmp_path, request):
    faults = getattr(request, "param", {}) or {}
    db = tmp_path / "acme.db"
    seed(db)
    with SimServer(db, faults=faults) as s:
        s.db_path = db
        yield s


@pytest.fixture
def secrets():
    return {"erp_password": ERP_PASSWORD}


@pytest.fixture
def make_sim(tmp_path):
    """Start a simulated company with a UI variant, faults and/or a data scenario; all are stopped afterwards."""
    from sim_env.scenarios import build_scenario

    started = []

    def make(ui=None, faults=None, scenario=None):
        db = tmp_path / f"acme-{len(started)}.db"
        sc = build_scenario(scenario)
        seed(db, sc)
        server = SimServer(db, faults=faults or {}, ui=ui).start()
        server.db_path, server.scenario = db, sc
        started.append(server)
        return server

    yield make
    for server in started:
        server.stop()
