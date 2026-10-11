import pytest

from conftest import MONGO_TEST_DB, MONGO_TEST_URI, ist_ms, utc_dt


def test_health_reports_ok(api):
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_employee_create_and_conflict(api, db):
    payload = {"emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
               "department": "Engineering", "joined_on": "2026-01-05"}
    response = api.post("/employees", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start",
                         "shift_end", "joined_on", "created_at"}
    assert body["shift_start"] == "09:30"
    assert body["shift_end"] == "18:30"
    assert isinstance(body["created_at"], int)
    assert body["created_at"] > 1_700_000_000_000
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1
    assert api.post("/employees", json=payload).status_code == 409


def test_employee_create_shift_times_must_differ(api):
    response = api.post("/employees", json={
        "emp_code": "EMP0002", "name": "Vikram", "email": "v@example.com",
        "department": "Sales", "shift_start": "09:00", "shift_end": "09:00",
        "joined_on": "2026-01-05"})
    assert response.status_code == 422


def test_employee_list_paging_sorts_and_filters(api, seed):
    for code, department in [("EMP0003", "Sales"), ("EMP0001", "Engineering"),
                             ("EMP0002", "Engineering")]:
        seed.employee(code, f"name {code}", department, "2026-01-05")
    response = api.get("/employees")
    assert response.status_code == 200
    body = response.json()
    assert [item["emp_code"] for item in body["items"]] == ["EMP0001", "EMP0002", "EMP0003"]
    assert body["total"] == 3
    assert body["page"] == 1 and body["page_size"] == 20
    filtered = api.get("/employees", params={"department": "Engineering"}).json()
    assert filtered["total"] == 2
    assert [item["emp_code"] for item in filtered["items"]] == ["EMP0001", "EMP0002"]
    paged = api.get("/employees", params={"page": 2, "page_size": 1}).json()
    assert [item["emp_code"] for item in paged["items"]] == ["EMP0002"]
    assert paged["total"] == 3


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_employee_list_pagination_bounds(api, params):
    assert api.get("/employees", params=params).status_code == 422


def test_startup_creates_indexes(mongo_client, api):
    db = mongo_client[MONGO_TEST_DB]
    assert db.employees.index_information().keys() >= {"uniq_emp_code", "dept_joined_on",
                                                       "joined_on"}
    assert db.attendance_logs.index_information().keys() >= {"uniq_emp_code_date",
                                                             "date_emp_code"}
    assert db.employees.index_information()["uniq_emp_code"]["unique"] is True
    assert db.attendance_logs.index_information()["uniq_emp_code_date"]["unique"] is True


def test_health_reports_503_when_mongodb_is_unreachable(api):
    from pymongo import MongoClient

    healthy = api.app.state.db
    unreachable = MongoClient("mongodb://127.0.0.1:1/", serverSelectionTimeoutMS=250,
                              connect=False)
    api.app.state.db = unreachable["attendance_test_db"]
    try:
        response = api.get("/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
    finally:
        unreachable.close()
        api.app.state.db = healthy
    assert api.get("/health").status_code == 200