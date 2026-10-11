import itertools
import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import ist_ms, utc_dt

WORKERS = 12


def run_parallel(callable_, count=WORKERS):
    barrier = threading.Barrier(count)

    def worker():
        barrier.wait()
        return callable_()

    with ThreadPoolExecutor(max_workers=count) as pool:
        return [future.result() for future in [pool.submit(worker) for _ in range(count)]]


def test_concurrent_employee_create_one_wins(api, seed, db):
    payload = {"emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
               "department": "Engineering", "joined_on": "2026-01-05"}
    codes = run_parallel(lambda: api.post("/employees", json=payload).status_code)
    assert codes.count(201) == 1
    assert codes.count(409) == WORKERS - 1
    assert db.employees.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_in_one_wins(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    payload = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)}
    codes = run_parallel(lambda: api.post("/attendance/punch-in", json=payload).status_code)
    assert codes.count(201) == 1
    assert codes.count(409) == WORKERS - 1
    assert db.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_concurrent_punch_out_one_wins(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 0), punch_out=None)
    payload = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 18, 0)}
    codes = run_parallel(lambda: api.post("/attendance/punch-out", json=payload).status_code)
    assert codes.count(200) == 1
    assert codes.count(409) == WORKERS - 1
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["work_hours"] == 8.5
    assert stored["punch_out"] == utc_dt(2026, 7, 6, 12, 30)


def test_concurrent_regularize_does_not_lose_history(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])

    def correct():
        return api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": ist_ms(2026, 7, 6, 9, 28), "reason": "concurrent correction",
            "regularized_by": "hr"}).status_code

    codes = run_parallel(correct)
    assert codes.count(200) == 1
    assert set(codes) <= {200, 409, 422}
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == 1
    assert stored["punch_in"] == utc_dt(2026, 7, 6, 3, 58)


def test_concurrent_regularize_with_distinct_values_keeps_every_entry(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    counter = itertools.count(20)
    lock = threading.Lock()

    def correct():
        with lock:
            minute = next(counter)
        return api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": ist_ms(2026, 7, 6, 9, minute),
            "reason": f"correction to minute {minute}", "regularized_by": "hr"}).status_code

    codes = run_parallel(correct)
    assert set(codes) <= {200, 409}
    assert codes.count(200) >= 1
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == codes.count(200)


def test_sequential_regularize_keeps_every_entry(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    for minute in (28, 25, 22):
        response = api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": ist_ms(2026, 7, 6, 9, minute),
            "reason": f"correction to 09:{minute}", "regularized_by": "hr"})
        assert response.status_code == 200
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == 3
    assert stored["punch_in"] == utc_dt(2026, 7, 6, 3, 52)