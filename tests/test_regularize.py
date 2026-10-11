from conftest import ist_ms, utc_dt

BASE = {"emp_code": "EMP0001", "date": "2026-07-06"}


def regularize(api, **payload):
    return api.patch(f"/attendance/{BASE['emp_code']}/{BASE['date']}", json=payload)


def test_regularize_recomputes_and_appends_history(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    response = regularize(api, punch_in=ist_ms(2026, 7, 6, 9, 28),
                          reason="biometric glitch", regularized_by="hr.admin")
    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == ist_ms(2026, 7, 6, 9, 28)
    assert body["late_minutes"] == 0
    assert body["work_hours"] == 9.03
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert len(body["history"]) == 1
    entry = body["history"][0]
    assert entry["by"] == "hr.admin"
    assert entry["reason"] == "biometric glitch"
    assert isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert entry["changes"]["punch_in"] == {"from": ist_ms(2026, 7, 6, 10, 5),
                                            "to": ist_ms(2026, 7, 6, 9, 28)}
    assert entry["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert entry["changes"]["work_hours"] == {"from": 8.42, "to": 9.03}
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert len(stored["history"]) == 1


def test_regularize_only_lists_fields_that_changed(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="WFH",
             punch_in=utc_dt(2026, 7, 6, 4, 30), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.5, late_minutes=30, overtime_minutes=0, half_day=False, history=[])
    body = regularize(api, status="PRESENT", reason="wrong work mode",
                      regularized_by="hr.admin").json()
    assert set(body["history"][0]["changes"]) == {"status"}
    assert body["history"][0]["changes"]["status"] == {"from": "WFH", "to": "PRESENT"}


def test_regularize_appends_and_keeps_order(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    regularize(api, punch_in=ist_ms(2026, 7, 6, 9, 28), reason="first correction",
               regularized_by="hr.admin")
    second = regularize(api, punch_out=ist_ms(2026, 7, 6, 19, 0),
                        reason="second correction", regularized_by="hr.manager").json()
    assert len(second["history"]) == 2
    assert [entry["by"] for entry in second["history"]] == ["hr.admin", "hr.manager"]
    assert set(second["history"][1]["changes"]) == {"punch_out", "overtime_minutes", "work_hours"}
    assert second["overtime_minutes"] == 30
    assert second["work_hours"] == 9.53
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    assert len(stored["history"]) == 2


def test_regularize_absent_clears_punch_times(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 40),
             work_hours=9.08, late_minutes=35, overtime_minutes=40, half_day=False, history=[])
    body = regularize(api, status="LEAVE", reason="approved leave",
                      regularized_by="hr.admin").json()
    assert body["status"] == "LEAVE"
    assert body["punch_in"] is None
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert set(body["history"][0]["changes"]) == {
        "status", "punch_in", "punch_out", "work_hours", "late_minutes",
        "overtime_minutes"}
    assert body["history"][0]["changes"]["punch_in"]["to"] is None
    assert body["history"][0]["changes"]["punch_out"]["from"] == ist_ms(2026, 7, 6, 19, 10)


def test_regularize_restores_presence_status(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="ABSENT", punch_in=None, punch_out=None,
             work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False, history=[])
    body = regularize(api, status="PRESENT", punch_in=ist_ms(2026, 7, 6, 9, 28),
                      punch_out=ist_ms(2026, 7, 6, 18, 30), reason="was on site",
                      regularized_by="hr.admin").json()
    assert body["status"] == "PRESENT"
    assert body["work_hours"] == 9.03
    assert body["late_minutes"] == 0


def test_regularize_rejects_inconsistent_requests(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    assert regularize(api, status="ABSENT", punch_in=ist_ms(2026, 7, 6, 9, 28),
                      reason="cannot carry times", regularized_by="hr").status_code == 422
    assert regularize(api, status="LEAVE", punch_out=ist_ms(2026, 7, 6, 18, 30),
                      reason="cannot carry times", regularized_by="hr").status_code == 422
    assert regularize(api, punch_in=ist_ms(2026, 7, 7, 9, 28),
                      reason="wrong attendance day", regularized_by="hr").status_code == 422
    assert regularize(api, punch_out=ist_ms(2026, 7, 6, 9, 0),
                      reason="before punch in", regularized_by="hr").status_code == 422
    assert regularize(api, punch_out=ist_ms(2026, 7, 7, 11, 0),
                      reason="too long", regularized_by="hr").status_code == 422
    assert regularize(api, reason="no field changed", regularized_by="hr").status_code == 422
    assert regularize(api, punch_in=ist_ms(2026, 7, 6, 10, 5), reason="same value",
                      regularized_by="hr").status_code == 422


def test_regularize_presence_status_needs_punch_in(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="ABSENT", punch_in=None, punch_out=None,
             work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False, history=[])
    assert regularize(api, status="PRESENT", reason="no times supplied",
                      regularized_by="hr").status_code == 422
    assert regularize(api, punch_in=ist_ms(2026, 7, 6, 9, 28),
                      reason="absent cannot carry times", regularized_by="hr").status_code == 422


def test_regularize_overnight_record_keeps_attendance_date(api, seed):
    seed.employee("EMP0005", "Meera Nair", "Support", "2026-03-10", "22:00", "06:00")
    seed.log("EMP0005", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 16, 25), punch_out=utc_dt(2026, 7, 7, 1, 10),
             work_hours=8.75, late_minutes=0, overtime_minutes=40, half_day=False, history=[])
    response = api.patch("/attendance/EMP0005/2026-07-06",
                         json={"punch_in": ist_ms(2026, 7, 7, 3, 0),
                               "reason": "corrected punch", "regularized_by": "hr"})
    assert response.status_code == 200
    body = response.json()
    assert body["punch_in"] == ist_ms(2026, 7, 7, 3, 0)
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 300


def test_regularize_not_found(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT", punch_in=utc_dt(2026, 7, 6, 4, 0))
    assert api.patch("/attendance/EMP9999/2026-07-06",
                     json={"reason": "unknown", "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-07",
                     json={"reason": "missing date", "regularized_by": "hr"}).status_code == 404


def test_regularize_legacy_record_without_history(api, seed):
    seed.employee("EMP0002", "Vikram Shah", "Engineering", "2026-07-13")
    seed.raw_log({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                  "punch_in": utc_dt(2026, 7, 14, 4, 2), "punch_out": utc_dt(2026, 7, 14, 13, 1),
                  "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    listed = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert listed["half_day"] is False
    assert listed["history"] == []
    assert api.patch("/attendance/EMP0002/2026-07-14",
                     json={"reason": "legacy record", "regularized_by": "hr"}).status_code == 422
    body = api.patch("/attendance/EMP0002/2026-07-14", json={
        "punch_in": ist_ms(2026, 7, 14, 9, 28), "reason": "legacy record corrected",
        "regularized_by": "hr"}).json()
    assert len(body["history"]) == 1
    assert set(body["history"][0]["changes"]) == {"punch_in", "work_hours"}


def test_regularize_ignores_derived_fields_in_request(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", status="PRESENT",
             punch_in=utc_dt(2026, 7, 6, 4, 35), punch_out=utc_dt(2026, 7, 6, 13, 0),
             work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False, history=[])
    body = api.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ist_ms(2026, 7, 6, 9, 28), "reason": "ignore derived",
        "regularized_by": "hr", "work_hours": 99.0, "half_day": True}).json()
    assert body["work_hours"] == 9.03
    assert body["half_day"] is False