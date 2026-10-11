import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

MONGO_TEST_URI = os.getenv("MONGO_TEST_URI", os.getenv("MONGO_URI", "mongodb://localhost:27017"))
MONGO_TEST_DB = os.getenv("MONGO_TEST_DB", "attendance_test_db")

os.environ["MONGO_URI"] = MONGO_TEST_URI
os.environ["MONGO_DB"] = MONGO_TEST_DB

from app.main import app

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ist_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
           millis: int = 0) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int((moment - EPOCH).total_seconds()) * 1000 + millis


def utc_dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


UNSET = object()


class Seed:
    def __init__(self, db):
        self.db = db

    def employee(self, emp_code: str, name: str, department: str, joined_on: str,
                 shift_start: str = "09:30", shift_end: str = "18:30") -> dict:
        document = {
            "emp_code": emp_code,
            "name": name,
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
            "created_at": utc_dt(2026, 1, 1),
        }
        self.db.employees.insert_one(document)
        return document

    def log(self, emp_code: str, day: str, status: str = "PRESENT",
            work_hours: float | None = None, late_minutes: int = 0,
            overtime_minutes: int = 0, half_day: bool = False,
            punch_in=UNSET, punch_out: datetime | None = None,
            history: list | None = None) -> dict:
        document = {
            "emp_code": emp_code,
            "date": day,
            "status": status,
            "punch_in": utc_dt(2026, 7, 6, 4, 0) if punch_in is UNSET else punch_in,
            "punch_out": punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
        if history is not None:
            document["history"] = history
        self.db.attendance_logs.insert_one(document)
        return document

    def raw_log(self, document: dict) -> dict:
        self.db.attendance_logs.insert_one(document)
        return document


@pytest.fixture(scope="session")
def mongo_client():
    client = MongoClient(MONGO_TEST_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except PyMongoError as error:
        client.close()
        pytest.skip(
            f"MongoDB is not reachable at {MONGO_TEST_URI} ({type(error).__name__}); "
            "every test in this file is unverified"
        )
    yield client
    client.drop_database(MONGO_TEST_DB)
    client.close()


@pytest.fixture(scope="session")
def api(mongo_client):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(mongo_client, api):
    database = mongo_client[MONGO_TEST_DB]
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})
    yield database
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})


@pytest.fixture
def seed(db):
    return Seed(db)


ANALYTICS_EMPLOYEES = [
    ("EMP0001", "Asha Rao", "Engineering", "2026-01-05", "09:30", "18:30"),
    ("EMP0002", "Vikram Shah", "Engineering", "2026-07-20", "09:30", "18:30"),
    ("EMP0003", "Neha Iyer", "Sales", "2026-01-05", "09:30", "18:30"),
    ("EMP0004", "Rohit Das", "Sales", "2026-01-05", "22:00", "06:00"),
    ("EMP0005", "Meera Nair", "Support", "2026-07-01", "09:30", "18:30"),
    ("EMP0006", "Karan Bedi", "Support", "2026-08-01", "09:30", "18:30"),
    ("EMP0007", "Grace Menon", "Finance", "2026-01-01", "09:30", "18:30"),
]


def analytics_dataset(seed):
    for emp_code, name, department, joined_on, shift_start, shift_end in ANALYTICS_EMPLOYEES:
        seed.employee(emp_code, name, department, joined_on, shift_start, shift_end)
    seed.log("EMP0001", "2026-07-06", work_hours=9.12, late_minutes=0)
    seed.log("EMP0001", "2026-07-07", work_hours=3.50, late_minutes=35, half_day=True)
    seed.log("EMP0001", "2026-07-08", status="LEAVE")
    seed.log("EMP0001", "2026-07-09", status="ABSENT")
    seed.log("EMP0001", "2026-07-10", work_hours=None, late_minutes=0)
    seed.log("EMP0001", "2026-07-11", work_hours=8.00, late_minutes=10)
    seed.log("EMP0002", "2026-07-20", work_hours=9.00, late_minutes=20)
    seed.log("EMP0002", "2026-07-21", work_hours=9.50, late_minutes=0)
    seed.log("EMP0003", "2026-07-06", work_hours=9.08, late_minutes=35, overtime_minutes=40)
    seed.log("EMP0003", "2026-07-07", work_hours=4.50, late_minutes=0)
    seed.log("EMP0003", "2026-07-08", status="LEAVE")
    seed.log("EMP0003", "2026-07-11", work_hours=8.00, late_minutes=0)
    seed.log("EMP0004", "2026-07-06", work_hours=8.75, late_minutes=0, overtime_minutes=40)
    seed.log("EMP0004", "2026-07-07", work_hours=9.00, late_minutes=0)
    seed.log("EMP0005", "2026-07-01", work_hours=8.00, late_minutes=0)
    seed.log("EMP0005", "2026-07-02", status="ON_DUTY", work_hours=None, late_minutes=0)
    seed.raw_log({"emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT",
                  "punch_in": None, "punch_out": None, "work_hours": 12.0,
                  "late_minutes": 500, "overtime_minutes": 900, "half_day": False})