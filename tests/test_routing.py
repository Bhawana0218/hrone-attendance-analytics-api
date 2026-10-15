"""Routing regression: unknown paths are 404, contract paths exist.

Runs without MongoDB: unknown-path 404 is returned by FastAPI routing
before any database access, and the route table is inspected statically.
"""
from fastapi.testclient import TestClient

from app.main import app

CONTRACT_PATHS = {
    "/health",
    "/employees",
    "/attendance/punch-in",
    "/attendance/punch-out",
    "/attendance",
    "/attendance/{emp_code}/{date}",
    "/analytics/employees/{emp_code}/monthly",
    "/analytics/departments/summary",
    "/analytics/leaderboard/late",
    "/analytics/departments/{department}/trend",
    "/admin/explain/{endpoint}",
}


def test_unknown_paths_are_404_without_touching_db():
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/", "/v1/models"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}


def test_contract_paths_are_registered():
    registered = {route.path for route in app.routes if hasattr(route, "path")}
    assert CONTRACT_PATHS <= registered
