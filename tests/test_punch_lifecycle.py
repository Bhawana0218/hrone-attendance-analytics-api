import pytest

from conftest import ist_ms, utc_dt


def test_punch_in_creates_record(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    response = api.post("/attendance/punch-in",
                        json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
                         "late_minutes", "overtime_minutes", "half_day", "history"}
    assert body["date"] == "2026-07-06"
    assert body["status"] == "PRESENT"
    assert body["punch_in"] == ist_ms(2026, 7, 6, 9, 28)
    assert body["punch_out"] is None
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert "_id" in stored


@pytest.mark.parametrize("moment,expected", [
    (ist_ms(2026, 7, 6, 9, 29, 59), 0),
    (ist_ms(2026, 7, 6, 9, 40, 0), 0),
    (ist_ms(2026, 7, 6, 9, 40, 1), 10),
    (ist_ms(2026, 7, 6, 9, 40, 59), 10),
    (ist_ms(2026, 7, 6, 10, 15, 59), 45),
    (ist_ms(2026, 7, 6, 9, 30, 0), 0),
])
def test_late_minutes_rule_r2(api, seed, moment, expected):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05", "09:30", "18:30")
    body = api.post("/attendance/punch-in",
                    json={"emp_code": "EMP0001", "punched_at": moment}).json()
    assert body["late_minutes"] == expected


def test_instant_is_truncated_to_whole_second(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    body = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ist_ms(2026, 7, 6, 10, 15, 59, millis=900)}).json()
    assert body["punch_in"] == ist_ms(2026, 7, 6, 10, 15, 59)
    assert body["late_minutes"] == 45


def test_duplicate_punch_in_conflicts(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    payload = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)}
    assert api.post("/attendance/punch-in", json=payload).status_code == 201
    assert api.post("/attendance/punch-in", json=payload).status_code == 409


def test_punch_in_unknown_employee(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_in_uses_ist_calendar_date(api, seed, db):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    body = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 20, 30)}).json()
    assert body["date"] == "2026-07-06"
    late_night = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 2, 0)}).json()
    assert late_night["date"] == "2026-07-07"


def test_punch_in_overnight_shift_uses_previous_day(api, seed):
    seed.employee("EMP0005", "Meera Nair", "Support", "2026-03-10", "22:00", "06:00")
    seed.employee("EMP0006", "Karan Bedi", "Support", "2026-03-10", "22:00", "06:00")
    early = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 7, 3, 0)}).json()
    assert early["date"] == "2026-07-06"
    assert early["late_minutes"] == 300
    same_day = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0006", "punched_at": ist_ms(2026, 7, 6, 21, 55)}).json()
    assert same_day["date"] == "2026-07-06"
    assert same_day["late_minutes"] == 0
    assert api.post("/attendance/punch-in", json={
        "emp_code": "EMP0006", "punched_at": ist_ms(2026, 7, 7, 5, 59)}).status_code == 409


def test_punch_out_computes_derived_fields(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    body = api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 18, 35)}).json()
    assert body["punch_out"] == ist_ms(2026, 7, 6, 18, 35)
    assert body["work_hours"] == 9.12
    assert body["overtime_minutes"] == 0
    assert body["half_day"] is False
    assert body["history"] == []


def test_punch_out_rounds_half_up(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 30)})
    body = api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 11, 45)}).json()
    assert body["work_hours"] == 2.25


def test_punch_out_half_day_threshold(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 30)})
    below = api.post("/attendance/punch-out",
                     json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 14, 0)}).json()
    assert below["work_hours"] == 4.5
    assert below["half_day"] is False
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 9, 30)})
    at_threshold = api.post("/attendance/punch-out",
                            json={"emp_code": "EMP0001",
                                  "punched_at": ist_ms(2026, 7, 7, 13, 59)}).json()
    assert at_threshold["work_hours"] == 4.48
    assert at_threshold["half_day"] is True


@pytest.mark.parametrize("out_hour,out_minute,expected", [
    (18, 45, 0),
    (18, 59, 0),
    (19, 0, 30),
    (19, 40, 70),
])
def test_punch_out_overtime_rule_r3(api, seed, db, out_hour, out_minute, expected):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    body = api.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": ist_ms(2026, 7, 6, out_hour, out_minute)}).json()
    assert body["overtime_minutes"] == expected


def test_punch_out_across_midnight_for_overnight_shift(api, seed):
    seed.employee("EMP0005", "Meera Nair", "Support", "2026-03-10", "22:00", "06:00")
    api.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 6, 21, 55)})
    body = api.post("/attendance/punch-out", json={
        "emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 7, 6, 40)}).json()
    assert body["date"] == "2026-07-06"
    assert body["work_hours"] == 8.75
    assert body["overtime_minutes"] == 40


def test_punch_out_picks_most_recent_open_record(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 9, 28)})
    body = api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 18, 0)}).json()
    assert body["date"] == "2026-07-07"
    assert body["work_hours"] == 8.53


def test_punch_out_errors(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001",
                          "punched_at": ist_ms(2026, 7, 6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in",
             json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28)})
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001",
                          "punched_at": ist_ms(2026, 7, 6, 9, 28)}).status_code == 422
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001",
                          "punched_at": ist_ms(2026, 7, 7, 9, 29)}).status_code == 422
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001",
                          "punched_at": ist_ms(2026, 7, 7, 9, 28)}).status_code == 200
    assert api.post("/attendance/punch-out",
                    json={"emp_code": "EMP0001",
                          "punched_at": ist_ms(2026, 7, 6, 20, 0)}).status_code == 409


def test_punch_in_status_variants(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    body = api.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 28), "status": "WFH"}).json()
    assert body["status"] == "WFH"
    assert api.post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 7, 9, 28),
        "status": "ABSENT"}).status_code == 422


def test_attendance_list_sorting_filtering_and_paging(api, seed):
    seed.employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
    seed.employee("EMP0002", "Vikram Shah", "Engineering", "2026-01-05")
    seed.log("EMP0001", "2026-07-06", work_hours=9.0)
    seed.log("EMP0002", "2026-07-06", work_hours=9.0)
    seed.log("EMP0001", "2026-07-07", status="LEAVE")
    seed.log("EMP0001", "2026-07-05", work_hours=8.0)
    body = api.get("/attendance").json()
    assert [(item["date"], item["emp_code"]) for item in body["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"),
        ("2026-07-06", "EMP0002"), ("2026-07-05", "EMP0001")]
    assert body["total"] == 4
    ranged = api.get("/attendance", params={"date_from": "2026-07-06",
                                            "date_to": "2026-07-06"}).json()
    assert ranged["total"] == 2
    by_status = api.get("/attendance", params={"status": "LEAVE"}).json()
    assert by_status["total"] == 1
    by_employee = api.get("/attendance", params={"emp_code": "EMP0002"}).json()
    assert by_employee["total"] == 1
    paged = api.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert [(item["date"], item["emp_code"]) for item in paged["items"]] == [
        ("2026-07-06", "EMP0002"), ("2026-07-05", "EMP0001")]
    assert paged["total"] == 4
    assert "id" not in paged["items"][0]
    assert "_id" not in paged["items"][0]


def test_attendance_list_rejects_inverted_range(api):
    assert api.get("/attendance", params={"date_from": "2026-07-10",
                                          "date_to": "2026-07-01"}).status_code == 422


def test_attendance_list_legacy_document(api, seed):
    seed.employee("EMP0002", "Vikram Shah", "Engineering", "2026-07-13")
    seed.raw_log({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                  "punch_in": utc_dt(2026, 7, 14, 4, 2), "punch_out": utc_dt(2026, 7, 14, 13, 1),
                  "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    body = api.get("/attendance", params={"emp_code": "EMP0002"}).json()
    item = body["items"][0]
    assert item["half_day"] is False
    assert item["history"] == []
    assert item["work_hours"] == 8.98