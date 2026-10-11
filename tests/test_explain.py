import pytest

from conftest import analytics_dataset

TARGETS = [
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31",
                         "status": "PRESENT"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 3}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-06",
                          "to": "2026-07-12"}),
]


@pytest.mark.parametrize("endpoint,params", TARGETS)
def test_explain_returns_a_plan(api, seed, endpoint, params):
    analytics_dataset(seed)
    response = api.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"endpoint", "collection", "explain"}
    assert body["endpoint"] == endpoint
    assert body["collection"] in {"attendance_logs", "employees"}
    assert "queryPlanner" in body["explain"] or "stages" in body["explain"]


def test_explain_reports_the_collection_it_ran_on(api, seed):
    analytics_dataset(seed)
    list_plan = api.get("/admin/explain/attendance_list").json()
    assert list_plan["collection"] == "attendance_logs"
    monthly = api.get("/admin/explain/employee_monthly",
                      params={"emp_code": "EMP0001", "month": "2026-07"}).json()
    assert monthly["collection"] == "attendance_logs"
    trend = api.get("/admin/explain/department_trend",
                    params={"department": "Engineering", "from": "2026-07-06",
                            "to": "2026-07-12"}).json()
    assert trend["collection"] == "employees"


def test_explain_accepts_pagination(api, seed):
    analytics_dataset(seed)
    plan = api.get("/admin/explain/attendance_list",
                   params={"page": 3, "page_size": 5}).json()["explain"]
    assert "queryPlanner" in plan or "stages" in plan


@pytest.mark.parametrize("endpoint,params", [
    ("employee_monthly", {"month": "2026-07"}),
    ("employee_monthly", {"emp_code": "EMP0001"}),
    ("department_summary", {}),
    ("late_leaderboard", {}),
    ("department_trend", {"from": "2026-07-06", "to": "2026-07-12"}),
    ("department_trend", {"department": "Engineering", "to": "2026-07-12"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-06"}),
])
def test_explain_requires_endpoint_parameters(api, seed, endpoint, params):
    analytics_dataset(seed)
    assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422


def test_explain_attendance_list_needs_no_parameters(api, seed):
    analytics_dataset(seed)
    assert api.get("/admin/explain/attendance_list").status_code == 200


def test_explain_rejects_unknown_endpoint(api):
    assert api.get("/admin/explain/employee_yearly", params={"month": "2026-07"}).status_code == 422