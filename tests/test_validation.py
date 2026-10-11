import pytest

from conftest import ist_ms


@pytest.mark.parametrize("punched_at", [
    1783312500,
    1783312500.0,
    1783312500000.5,
    "1783312500000",
    True,
    99_999_999_999,
    4_102_444_800_001,
    [],
])
def test_punch_in_rejects_bad_timestamps(api, seed, punched_at):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    response = api.post("/attendance/punch-in",
                        json={"emp_code": "EMP0001", "punched_at": punched_at})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_punch_in_requires_emp_code(api):
    assert api.post("/attendance/punch-in", json={}).status_code == 422


def test_punch_out_rejects_bad_timestamps(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": "nope"}).status_code == 422


@pytest.mark.parametrize("payload", [
    {"emp_code": "emp1", "name": "A", "email": "a@b.c", "department": "X",
     "joined_on": "2026-01-05"},
    {"emp_code": "EMP0001", "name": "", "email": "a@b.c", "department": "X",
     "joined_on": "2026-01-05"},
    {"emp_code": "EMP0001", "name": "A", "email": "not-an-email", "department": "X",
     "joined_on": "2026-01-05"},
    {"emp_code": "EMP0001", "name": "A", "email": "a@b.c", "department": "",
     "joined_on": "2026-01-05"},
    {"emp_code": "EMP0001", "name": "A", "email": "a@b.c", "department": "X",
     "joined_on": "05-01-2026"},
    {"emp_code": "EMP0001", "name": "A", "email": "a@b.c", "department": "X",
     "joined_on": "2026-01-05", "shift_start": "25:00"},
    {"emp_code": "EMP0001", "name": "A", "email": "a@b.c", "department": "X",
     "joined_on": "2026-01-05", "shift_end": "18:99"},
    {"emp_code": "EMP0001", "name": "A", "email": "a@b.c", "department": "X"},
])
def test_employee_create_validation(api, payload):
    assert api.post("/employees", json=payload).status_code == 422


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "26-07", "2026-7", "july"])
def test_month_pattern(api, month):
    assert api.get("/analytics/departments/summary",
                   params={"month": month}).status_code == 422
    assert api.get("/analytics/leaderboard/late",
                   params={"month": month}).status_code == 422
    assert api.get("/analytics/employees/EMP0001/monthly",
                   params={"month": month}).status_code == 422


@pytest.mark.parametrize("limit", [0, 51, -1])
def test_leaderboard_limit_bounds(api, limit):
    assert api.get("/analytics/leaderboard/late",
                   params={"month": "2026-07", "limit": limit}).status_code == 422


def test_unknown_endpoint_explain(api):
    assert api.get("/admin/explain/nope").status_code == 422


def test_unknown_status_filter(api):
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422


def test_patch_requires_reason_and_actor(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.patch("/attendance/EMP0001/2026-07-06",
                     json={"reason": "why"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-06",
                     json={"reason": "valid reason",
                           "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-06",
                     json={"regularized_by": "hr"}).status_code == 422


@pytest.mark.parametrize("payload", [
    {"reason": "x", "regularized_by": "hr"},
    {"reason": "x" * 201, "regularized_by": "hr"},
    {"reason": "long enough", "regularized_by": ""},
])
def test_patch_field_bounds(api, seed, payload):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.patch("/attendance/EMP0001/2026-07-06", json=payload).status_code == 422


def test_patch_rejects_bad_date_in_path(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.patch("/attendance/EMP0001/06-07-2026",
                     json={"reason": "long enough", "regularized_by": "hr"}).status_code == 422


def test_patch_rejects_bad_status(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.patch("/attendance/EMP0001/2026-07-06",
                     json={"status": "HOLIDAY", "reason": "long enough",
                           "regularized_by": "hr"}).status_code == 422


def test_punch_in_accepts_default_status_and_now(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert response.status_code == 201
    assert response.json()["status"] == "PRESENT"
    assert response.json()["punch_in"] > ist_ms(2026, 1, 1)