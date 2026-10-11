import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import analytics_dataset

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR"]
EMPLOYEE_COUNT = 300
DAY_COUNT = 366
MONTH_START = "2026-07-01"
MONTH_END = "2026-07-31"


@pytest.fixture(scope="module")
def large_dataset(api):
    db = api.app.state.db
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([
        {
            "emp_code": f"EMP{index:04d}",
            "name": f"Person {index}",
            "email": f"person{index}@example.com",
            "department": DEPARTMENTS[index % len(DEPARTMENTS)],
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-06",
            "created_at": datetime(2025, 1, 6, tzinfo=timezone.utc),
        }
        for index in range(EMPLOYEE_COUNT)
    ])
    statuses = ["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"]
    start = datetime(2025, 7, 1, tzinfo=timezone.utc)
    batch = []
    for day_offset in range(DAY_COUNT):
        day = start + timedelta(days=day_offset)
        day_text = day.date().isoformat()
        for index in range(EMPLOYEE_COUNT):
            status = statuses[(day_offset + index) % len(statuses)]
            present = status in ("PRESENT", "WFH", "ON_DUTY")
            worked = present and (day_offset + index) % 9 != 0
            batch.append({
                "emp_code": f"EMP{index:04d}",
                "date": day_text,
                "status": status,
                "punch_in": day if worked else None,
                "punch_out": day + timedelta(hours=9) if worked else None,
                "work_hours": round(6.0 + (index % 40) / 10, 2) if worked else None,
                "late_minutes": (index * 3) % 50 if worked else 0,
                "overtime_minutes": (index * 7) % 60 if worked else 0,
                "half_day": bool(worked and index % 17 == 0),
            })
            if len(batch) >= 20000:
                db.attendance_logs.insert_many(batch)
                batch = []
    if batch:
        db.attendance_logs.insert_many(batch)
    yield db
    db.attendance_logs.delete_many({})
    db.employees.delete_many({})


SCALE_TARGETS = [
    ("attendance_list", {}),
    ("attendance_list", {"page": 40, "page_size": 100}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": MONTH_START, "date_to": MONTH_END}),
    ("attendance_list", {"status": "PRESENT", "date_from": MONTH_START}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 5}),
    ("department_trend", {"department": "Engineering", "from": MONTH_START, "to": MONTH_END}),
]


def test_dataset_is_large_enough(api, large_dataset):
    assert large_dataset.attendance_logs.count_documents({}) > 100_000


@pytest.mark.parametrize("endpoint,params", SCALE_TARGETS)
def test_explain_uses_an_index_at_scale(api, large_dataset, endpoint, params):
    response = api.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200
    plan = json.dumps(response.json()["explain"])
    assert "IXSCAN" in plan, plan[:400]
    assert "COLLSCAN" not in plan, plan[:400]


@pytest.mark.parametrize("path,params", [
    ("/attendance", {"page": 40, "page_size": 100}),
    ("/attendance", {"emp_code": "EMP0001"}),
    ("/analytics/employees/EMP0001/monthly", {"month": "2026-07"}),
    ("/analytics/departments/summary", {"month": "2026-07"}),
    ("/analytics/leaderboard/late", {"month": "2026-07"}),
    ("/analytics/departments/Engineering/trend", {"from": MONTH_START, "to": MONTH_END}),
])
def test_endpoints_are_fast_at_scale(api, large_dataset, path, params):
    started = time.perf_counter()
    response = api.get(path, params=params)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert response.status_code == 200
    assert elapsed_ms < 2000, f"{path} took {elapsed_ms:.0f}ms"


def test_analytics_are_stable_against_the_large_dataset(api, large_dataset):
    monthly = api.get("/analytics/employees/EMP0001/monthly",
                      params={"month": "2026-07"}).json()
    assert monthly["working_days"] == 23
    assert monthly["present_days"] > 0
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [item["department"] for item in summary["items"]] == sorted(DEPARTMENTS)
    leaderboard = api.get("/analytics/leaderboard/late",
                          params={"month": "2026-07", "limit": 5}).json()
    totals = [item["total_late_minutes"] for item in leaderboard["items"]]
    assert totals == sorted(totals, reverse=True)
    assert all(item["total_late_minutes"] > 0 for item in leaderboard["items"])
    trend = api.get("/analytics/departments/Engineering/trend",
                    params={"from": MONTH_START, "to": MONTH_END}).json()
    assert len(trend["items"]) == 31
    assert trend["items"][0]["date"] == MONTH_START
    assert trend["items"][-1]["date"] == MONTH_END


def test_analytics_after_scale_dataset_is_replaced(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert len(body["items"]) == 4