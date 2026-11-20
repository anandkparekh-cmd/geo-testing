import pandas as pd
import pytest

from geo_engine.ingest import ingest
from geo_engine.map_geo import map_geo
from geo_engine.synthetic import WorldConfig, generate_world


@pytest.fixture(scope="session")
def world42():
    return generate_world(WorldConfig(seed=42))


@pytest.fixture(scope="session")
def history42(world42):
    return world42.history()


@pytest.fixture(scope="session")
def ingested42(history42):
    return ingest("synthetic_history.csv", raw=history42.astype(str))


@pytest.fixture(scope="session")
def areas42(ingested42):
    return map_geo(ingested42.data)


@pytest.fixture(scope="session")
def events42(world42):
    ev = world42.events.copy()
    ev["start"] = pd.to_datetime(ev["start"])
    ev["end"] = ev["start"] + pd.to_timedelta(ev["days"] - 1, unit="D")
    return ev[ev["start"] <= world42.history_end].reset_index(drop=True)
