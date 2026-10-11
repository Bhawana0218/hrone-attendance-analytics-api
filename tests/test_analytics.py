from conftest import analytics_dataset


def test_employee_monthly(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert body["emp_code"] == "EMP0001"
    assert body["month"] == "2026-07"
    assert body["working_days"] == 23
    assert body["present_days"] == 2.5
    assert body["leave_days"] == 1
    assert body["late_count"] == 2
    assert body["total_late_minutes"] == 45
    assert body["total_overtime_minutes"] == 0
    assert body["attendance_pct"] == 10.87


def test_employee_monthly_weekend_excluded_from_present_but_counted_late(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert body["present_days"] == 2.5
    assert body["total_late_minutes"] == 45


def test_employee_monthly_mid_month_joiner(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 10
    assert body["present_days"] == 2.0
    assert body["late_count"] == 1
    assert body["total_late_minutes"] == 20
    assert body["attendance_pct"] == 20.0


def test_employee_monthly_not_yet_joined(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0006/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 0
    assert body["present_days"] == 0.0
    assert body["attendance_pct"] is None


def test_employee_monthly_no_records(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0007/monthly", params={"month": "2026-07"}).json()
    assert body["working_days"] == 23
    assert body["present_days"] == 0.0
    assert body["leave_days"] == 0
    assert body["late_count"] == 0
    assert body["attendance_pct"] == 0.0


def test_employee_monthly_on_duty_counts_as_present(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/employees/EMP0005/monthly", params={"month": "2026-07"}).json()
    assert body["present_days"] == 2.0
    assert body["working_days"] == 23
    assert body["attendance_pct"] == 8.7


def test_employee_monthly_unknown_employee(api, seed):
    analytics_dataset(seed)
    assert api.get("/analytics/employees/EMP9999/monthly",
                   params={"month": "2026-07"}).status_code == 404


def test_department_summary(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert body["month"] == "2026-07"
    assert [item["department"] for item in body["items"]] == [
        "Engineering", "Finance", "Sales", "Support"]
    engineering = body["items"][0]
    assert engineering["headcount"] == 2
    assert engineering["present_days"] == 4.5
    assert engineering["avg_work_hours"] == 7.82
    assert engineering["late_count"] == 3
    assert engineering["total_late_minutes"] == 65
    assert engineering["leave_count"] == 1
    assert engineering["on_duty_count"] == 0
    finance = body["items"][1]
    assert finance["headcount"] == 1
    assert finance["present_days"] == 0.0
    assert finance["avg_work_hours"] is None
    sales = body["items"][2]
    assert sales["headcount"] == 2
    assert sales["present_days"] == 4.0
    assert sales["avg_work_hours"] == 7.87
    assert sales["on_duty_count"] == 0
    support = body["items"][3]
    assert support["headcount"] == 1
    assert support["on_duty_count"] == 1


def test_department_summary_filters(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/departments/summary",
                   params={"month": "2026-07", "department": "Sales"}).json()
    assert [item["department"] for item in body["items"]] == ["Sales"]


def test_late_leaderboard(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()
    assert [item["emp_code"] for item in body["items"]] == ["EMP0001", "EMP0003", "EMP0002"]
    assert [item["rank"] for item in body["items"]] == [1, 2, 3]
    assert body["items"][0]["total_late_minutes"] == 45
    assert body["items"][0]["name"] == "Asha Rao"
    assert body["items"][0]["department"] == "Engineering"


def test_late_leaderboard_ties_share_rank(api, seed):
    seed.employee("EMP0001", "A", "Engineering", "2026-01-05")
    seed.employee("EMP0002", "B", "Engineering", "2026-01-05")
    seed.employee("EMP0003", "C", "Engineering", "2026-01-05")
    seed.employee("EMP0004", "D", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", late_minutes=60)
    seed.log("EMP0002", "2026-07-06", late_minutes=40)
    seed.log("EMP0003", "2026-07-06", late_minutes=40)
    seed.log("EMP0004", "2026-07-06", late_minutes=30)
    body = api.get("/analytics/leaderboard/late",
                   params={"month": "2026-07", "limit": 2}).json()
    assert [(item["emp_code"], item["rank"]) for item in body["items"]] == [
        ("EMP0001", 1), ("EMP0002", 2), ("EMP0003", 2)]


def test_late_leaderboard_limit_after_ranking_tie_includes_next_row(api, seed):
    seed.employee("EMP0001", "A", "Engineering", "2026-01-05")
    seed.employee("EMP0002", "B", "Engineering", "2026-01-05")
    seed.employee("EMP0003", "C", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", late_minutes=40)
    seed.log("EMP0002", "2026-07-06", late_minutes=40)
    seed.log("EMP0003", "2026-07-06", late_minutes=10)
    body = api.get("/analytics/leaderboard/late",
                   params={"month": "2026-07", "limit": 1}).json()
    assert [(item["emp_code"], item["rank"]) for item in body["items"]] == [
        ("EMP0001", 1), ("EMP0002", 1)]


def test_late_leaderboard_filters_and_empty(api, seed):
    analytics_dataset(seed)
    sales = api.get("/analytics/leaderboard/late",
                    params={"month": "2026-07", "department": "Sales"}).json()
    assert [item["emp_code"] for item in sales["items"]] == ["EMP0003"]
    empty = api.get("/analytics/leaderboard/late", params={"month": "2026-01"}).json()
    assert empty["items"] == []


def test_late_leaderboard_ignores_unknown_employee(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()
    assert all(item["emp_code"] != "EMP9999" for item in body["items"])


def test_department_trend_gap_filling(api, seed):
    analytics_dataset(seed)
    body = api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-06", "to": "2026-07-12"}).json()
    assert body["department"] == "Engineering"
    rows = body["items"]
    assert [row["date"] for row in rows] == [
        "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09",
        "2026-07-10", "2026-07-11", "2026-07-12"]
    assert rows[0] == {"date": "2026-07-06", "is_working_day": True, "headcount": 1,
                       "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0,
                       "moving_avg_7d": 1.0}
    assert rows[1] == {"date": "2026-07-07", "is_working_day": True, "headcount": 1,
                       "present_count": 0.5, "late_count": 1, "attendance_rate": 0.5,
                       "moving_avg_7d": 0.75}
    assert rows[3] == {"date": "2026-07-09", "is_working_day": True, "headcount": 1,
                       "present_count": 0.0, "late_count": 0, "attendance_rate": 0.0,
                       "moving_avg_7d": 0.375}
    assert rows[4] == {"date": "2026-07-10", "is_working_day": True, "headcount": 1,
                       "present_count": 1.0, "late_count": 0, "attendance_rate": 1.0,
                       "moving_avg_7d": 0.5}
    assert rows[5] == {"date": "2026-07-11", "is_working_day": False, "headcount": 1,
                       "present_count": 1.0, "late_count": 1, "attendance_rate": None,
                       "moving_avg_7d": 0.5}
    assert rows[6] == {"date": "2026-07-12", "is_working_day": False, "headcount": 1,
                       "present_count": 0.0, "late_count": 0, "attendance_rate": None,
                       "moving_avg_7d": 0.5}


def test_department_trend_headcount_grows_on_join_day(api, seed):
    analytics_dataset(seed)
    rows = api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-17", "to": "2026-07-22"}).json()["items"]
    assert [row["headcount"] for row in rows] == [1, 1, 1, 2, 2, 2]
    assert [row["attendance_rate"] for row in rows] == [0.0, None, None, 0.5, 0.5, 0.0]
    assert [row["moving_avg_7d"] for row in rows] == [0.0, 0.0, 0.0, 0.25, 0.3333, 0.25]


def test_department_trend_half_day_and_weekend_record(api, seed):
    analytics_dataset(seed)
    rows = api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-06", "to": "2026-07-07"}).json()["items"]
    assert rows[1]["present_count"] == 0.5
    assert rows[1]["late_count"] == 1
    assert rows[1]["attendance_rate"] == 0.5


def test_department_trend_empty_department(api, seed):
    analytics_dataset(seed)
    rows = api.get("/analytics/departments/Finance/trend",
                   params={"from": "2026-07-06", "to": "2026-07-07"}).json()["items"]
    assert len(rows) == 2
    assert rows[0]["headcount"] == 1
    assert rows[0]["present_count"] == 0.0
    assert rows[0]["attendance_rate"] == 0.0
    assert rows[0]["moving_avg_7d"] == 0.0


def test_department_trend_errors(api, seed):
    analytics_dataset(seed)
    assert api.get("/analytics/departments/Nope/trend",
                   params={"from": "2026-07-06", "to": "2026-07-07"}).status_code == 404
    assert api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert api.get("/analytics/departments/Engineering/trend",
                   params={"from": "2026-07-01", "to": "2026-10-05"}).status_code == 422
    assert api.get("/analytics/departments/Engineering/trend",
                   params={"to": "2026-07-07"}).status_code == 422