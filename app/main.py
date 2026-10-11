"""Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)
Docs: http://127.0.0.1:8000/docs

Contract: openapi.yaml (paths, rules R1-R10). Storage: DATA_MODEL.md.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

EPOCH_MILLIS_MIN = 100_000_000_000
EPOCH_MILLIS_MAX = 4_102_444_800_000

GRACE_MINUTES = 10
OVERTIME_MINIMUM_MINUTES = 30
HALF_DAY_THRESHOLD_HOURS = 4.50
MAX_TREND_DAYS = 92

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")

DERIVED_FIELDS = ("work_hours", "late_minutes", "overtime_minutes", "half_day")
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours",
                  "late_minutes", "overtime_minutes", "half_day")

INDEXES = (
    ("employees", [("emp_code", ASCENDING)], {"name": "uniq_emp_code", "unique": True}),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
    ("employees", [("joined_on", ASCENDING)], {"name": "joined_on"}),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)],
     {"name": "uniq_emp_code_date", "unique": True}),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_emp_code"}),
)

ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
WEEKDAY_CODES = [2, 3, 4, 5, 6]

logger = logging.getLogger("hrone.attendance")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def truncate_to_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def to_epoch_millis(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int((value - EPOCH).total_seconds()) * 1000


def from_epoch_millis(value: int) -> datetime:
    return truncate_to_second(EPOCH + timedelta(milliseconds=value))


def round_half_up(value: float, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":"))
    return clock_time(hour, minute)


def minutes_of(value: str) -> int:
    parsed = parse_hhmm(value)
    return parsed.hour * 60 + parsed.minute


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date_of(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and minutes_of(local.strftime("%H:%M")) < minutes_of(shift_end):
        day -= timedelta(days=1)
    return day


def shift_boundary(attendance_day: date, shift_time: str, add_day: bool) -> datetime:
    local = datetime.combine(attendance_day, parse_hhmm(shift_time), tzinfo=IST)
    if add_day:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def compute_late_minutes(attendance_day: date, shift_start: str, punch_in: datetime) -> int:
    start = shift_boundary(attendance_day, shift_start, add_day=False)
    seconds = int((punch_in - start).total_seconds())
    if seconds <= GRACE_MINUTES * 60:
        return 0
    return seconds // 60


def compute_overtime_minutes(attendance_day: date, shift_start: str, shift_end: str,
                              punch_out: datetime) -> int:
    end = shift_boundary(attendance_day, shift_end, add_day=is_overnight(shift_start, shift_end))
    seconds = int((punch_out - end).total_seconds())
    if seconds < 0:
        return 0
    minutes = seconds // 60
    return minutes if minutes >= OVERTIME_MINIMUM_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float | None) -> bool:
    return work_hours is not None and work_hours < HALF_DAY_THRESHOLD_HOURS


def derive_metrics(attendance_day: date, shift_start: str, shift_end: str,
                   status: str, punch_in: datetime | None,
                   punch_out: datetime | None) -> dict[str, Any]:
    is_present = status in PRESENCE_STATUSES
    closed = is_present and punch_in is not None and punch_out is not None
    work_hours = compute_work_hours(punch_in, punch_out) if closed else None
    return {
        "work_hours": work_hours,
        "late_minutes": (
            compute_late_minutes(attendance_day, shift_start, punch_in)
            if is_present and punch_in is not None else 0
        ),
        "overtime_minutes": (
            compute_overtime_minutes(attendance_day, shift_start, shift_end, punch_out)
            if closed else 0
        ),
        "half_day": compute_half_day(work_hours),
    }


def month_bounds(month: str) -> tuple[str, str]:
    year, month_number = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def working_days_between(joined_on: str, start: date, end: date) -> int:
    first = max(date.fromisoformat(joined_on), start)
    if first > end:
        return 0
    total = 0
    day = first
    while day <= end:
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total


EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MILLIS_MIN, le=EPOCH_MILLIS_MAX)]
EpochMillisOptional = Annotated[EpochMillis | None, Field(default=None)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Page = Annotated[int, Field(ge=1)]
PageSize = Annotated[int, Field(ge=1, le=100)]
LeaderboardLimit = Annotated[int, Field(ge=1, le=50)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class ExplainTarget(str, Enum):
    ATTENDANCE_LIST = "attendance_list"
    EMPLOYEE_MONTHLY = "employee_monthly"
    DEPARTMENT_SUMMARY = "department_summary"
    LATE_LEADERBOARD = "late_leaderboard"
    DEPARTMENT_TREND = "department_trend"


class HealthOut(BaseModel):
    status: Literal["ok"]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: date


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMillis


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class PunchInIn(BaseModel):
    emp_code: Annotated[str, Field(min_length=1, max_length=50)]
    punched_at: EpochMillisOptional = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutIn(BaseModel):
    emp_code: Annotated[str, Field(min_length=1, max_length=50)]
    punched_at: EpochMillisOptional = None


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillisOptional = None
    punch_out: EpochMillisOptional = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


class ChangeOut(BaseModel):
    from_: Any = Field(alias="from")
    to: Any


class HistoryEntryOut(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, ChangeOut]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: EpochMillisOptional = None
    punch_out: EpochMillisOptional = None
    work_hours: float | None = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryOut]


class AttendancePageOut(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItemOut]


class LeaderboardItemOut(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItemOut]


class TrendItemOut(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class TrendOut(BaseModel):
    department: str
    items: list[TrendItemOut]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


def employee_to_api(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": to_epoch_millis(document.get("created_at")),
    }


def normalised_record(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": document["status"],
        "punch_in": document.get("punch_in"),
        "punch_out": document.get("punch_out"),
        "work_hours": document.get("work_hours"),
        "late_minutes": int(document.get("late_minutes") or 0),
        "overtime_minutes": int(document.get("overtime_minutes") or 0),
        "half_day": bool(document.get("half_day") or False),
    }


def change_value_to_api(field: str, value: Any) -> Any:
    if field in ("punch_in", "punch_out"):
        return to_epoch_millis(value)
    return value


def history_entry_to_api(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": to_epoch_millis(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {
                "from": change_value_to_api(field, pair.get("from")),
                "to": change_value_to_api(field, pair.get("to")),
            }
            for field, pair in (entry.get("changes") or {}).items()
        },
    }


def record_to_api(document: dict[str, Any]) -> dict[str, Any]:
    record = normalised_record(document)
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": record["status"],
        "punch_in": to_epoch_millis(record["punch_in"]),
        "punch_out": to_epoch_millis(record["punch_out"]),
        "work_hours": record["work_hours"],
        "late_minutes": record["late_minutes"],
        "overtime_minutes": record["overtime_minutes"],
        "half_day": record["half_day"],
        "history": [history_entry_to_api(entry) for entry in (document.get("history") or [])],
    }


def is_weekday_expression(date_expression: Any) -> dict[str, Any]:
    return {"$in": [date_expression, WEEKDAY_CODES]}


def weekday_of_date_string() -> dict[str, Any]:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}


def present_days_expression() -> dict[str, Any]:
    return {
        "$cond": [
            {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]},
                      is_weekday_expression(weekday_of_date_string())]},
            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
            0,
        ]
    }


def present_records_expression() -> dict[str, Any]:
    return {
        "$cond": [{"$in": ["$status", list(PRESENCE_STATUSES)]},
                  {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                  0]
    }


def late_count_expression() -> dict[str, Any]:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def status_count_expression(status: str) -> dict[str, Any]:
    return {"$cond": [{"$eq": ["$status", status]}, 1, 0]}


def has_work_hours_expression() -> dict[str, Any]:
    return {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            {"$in": [{"$type": "$work_hours"}, ["double", "int", "long", "decimal"]]},
        ]
    }


def round_half_up_expression(expression: Any, places: int) -> dict[str, Any]:
    scale = 10 ** places
    decimal_value = {"$toDecimal": expression}
    return {
        "$toDouble": {
            "$divide": [
                {
                    "$trunc": {
                        "$add": [
                            {"$multiply": [decimal_value, scale]},
                            {"$cond": [{"$lt": [decimal_value, 0]}, -0.5, 0.5]},
                        ]
                    }
                },
                scale,
            ]
        }
    }


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_days_expression()},
                "leave_days": {"$sum": status_count_expression("LEAVE")},
                "late_count": {"$sum": late_count_expression()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
            }
        },
    ]


def department_headcount_pipeline(month_end: str,
                                  department: str | None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1}},
        {"$sort": {"department": ASCENDING}},
    ]


def department_metrics_pipeline(month_start: str, month_end: str,
                                department: str | None) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": {"$eq": department}}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$employee.department",
                    "present_days": {"$sum": present_days_expression()},
                    "late_count": {"$sum": late_count_expression()},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": status_count_expression("LEAVE")},
                    "on_duty_count": {"$sum": status_count_expression("ON_DUTY")},
                    "work_hours_sum": {"$sum": {"$cond": [has_work_hours_expression(),
                                                           "$work_hours", 0]}},
                    "work_hours_count": {"$sum": {"$cond": [has_work_hours_expression(), 1, 0]}},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$work_hours_count", 0]},
                            round_half_up_expression(
                                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2
                            ),
                            None,
                        ]
                    },
                }
            },
            {"$sort": {"department": ASCENDING}},
        ]
    )
    return pipeline


def late_leaderboard_pipeline(month_start: str, month_end: str,
                              emp_codes: list[str] | None,
                              limit: int) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"date": {"$gte": month_start, "$lte": month_end}}
    if emp_codes is not None:
        match["emp_code"] = {"$in": emp_codes}
    return [
        {"$match": match},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": late_count_expression()},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$match": {"employee.emp_code": {"$exists": True}}},
        {"$unwind": "$employee"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]


def department_trend_pipeline(department: str, date_from: date, date_to: date) -> list[dict[str, Any]]:
    bounds = [
        datetime.combine(date_from, clock_time.min, tzinfo=timezone.utc),
        datetime.combine(date_to + timedelta(days=1), clock_time.min, tzinfo=timezone.utc),
    ]
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": bounds[0]}}},
        {"$densify": {"field": "day", "range": {"step": 86_400_000, "unit": "millisecond",
                                                "bounds": bounds}}},
        {
            "$set": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department,
                                "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "count"},
                ],
                "as": "headcount_rows",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$first": "$headcount_rows.count"}, 0]}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "pipeline": [
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": {"$eq": department}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": present_records_expression()},
                            "late_count": {"$sum": late_count_expression()},
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": [{"$first": "$daily.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$daily.late_count"}, 0]},
                "is_working_day": is_weekday_expression({"$dayOfWeek": "$day"}),
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": round_half_up_expression("$moving_avg_7d", 4),
            }
        },
        {"$sort": {"day": ASCENDING}},
    ]


def compare_and_swap_expression(before: dict[str, Any],
                                history_length: int) -> list[dict[str, Any]]:
    defaults: dict[str, Any] = {
        "status": None,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    predicates = [
        {"$expr": {"$eq": [{"$ifNull": [f"${field}", default]}, before[field]]}}
        for field, default in defaults.items()
    ]
    predicates.append(
        {"$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_length]}}
    )
    return predicates


def attendance_filter(emp_code: str | None, date_from: date | None, date_to: date | None,
                      status: Status | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status.value
    if date_from is not None or date_to is not None:
        window: dict[str, Any] = {}
        if date_from is not None:
            window["$gte"] = date_from.isoformat()
        if date_to is not None:
            window["$lte"] = date_to.isoformat()
        query["date"] = window
    return query


def ensure_indexes(db: Database) -> None:
    for collection_name, keys, options in INDEXES:
        db[collection_name].create_index(keys, **options)


def get_db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return db


def require_employee(db: Database, emp_code: str) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    return employee


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging()
    client = MongoClient(
        MONGO_URI,
        tz_aware=True,
        serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
    )
    application.state.client = client
    application.state.db = client[MONGO_DB]
    try:
        ensure_indexes(application.state.db)
        logger.info("mongodb ready db=%s indexes=verified", MONGO_DB)
    except PyMongoError as error:
        logger.error("index creation failed db=%s error=%s", MONGO_DB, type(error).__name__)
    try:
        yield
    finally:
        client.close()
        logger.info("mongodb connection closed")


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    summary="Punch in/out, manual regularization with an audit trail, and MongoDB-backed analytics.",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    logger.info(
        "method=%s path=%s status=%d duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        (time.perf_counter() - started) * 1000,
    )
    return response


@app.exception_handler(PyMongoError)
async def handle_database_error(request: Request, error: PyMongoError):
    logger.error(
        "database error method=%s path=%s error=%s detail=%s",
        request.method,
        request.url.path,
        type(error).__name__,
        error,
    )
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


ERROR_RESPONSE = {
    "description": "Error",
    "content": {"application/json": {"schema": {"type": "object", "required": ["detail"],
                                                  "properties": {"detail": {"type": "string"}}}}},
}
NOT_FOUND = {404: ERROR_RESPONSE}
CONFLICT = {409: ERROR_RESPONSE}
NOT_FOUND_OR_CONFLICT = {**NOT_FOUND, **CONFLICT}
SERVICE_UNAVAILABLE = {503: ERROR_RESPONSE}


@app.get("/health", response_model=HealthOut, tags=["system"], summary="Readiness probe",
         responses=SERVICE_UNAVAILABLE,
         description="Returns 200 only when MongoDB answers a ping, otherwise 503.")
def health(request: Request) -> dict[str, str]:
    db = get_db(request)
    try:
        db.command("ping")
    except PyMongoError as error:
        logger.warning("health check failed error=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=EmployeeOut, tags=["employees"],
          summary="Create an employee", responses=CONFLICT,
          description="The client supplies the customer's own emp_code, which is unique and enforced by a "
                      "database unique index. shift_start must differ from shift_end. created_at is set by "
                      "the server.")
def create_employee(body: EmployeeIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    if body.shift_start == body.shift_end:
        raise bad_request("shift_start must differ from shift_end")
    document = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": utc_now(),
    }
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(document)


@app.get("/employees", response_model=EmployeePageOut, tags=["employees"], summary="List employees",
         description="Sorted by emp_code ascending. total counts the filtered set.")
def list_employees(
    request: Request,
    department: Annotated[str | None, Query(max_length=50)] = None,
    page: Page = 1,
    page_size: PageSize = 20,
) -> dict[str, Any]:
    db = get_db(request)
    query: dict[str, Any] = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    items = [employee_to_api(document)
             for document in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut,
          tags=["attendance"], summary="Record a punch-in", responses=NOT_FOUND_OR_CONFLICT,
          description="Creates the day's record. date follows R1 and late_minutes follows R2. The insert is "
                      "guarded by a unique index on (emp_code, date), so concurrent punches for the same day "
                      "yield exactly one 201 and the rest 409.")
def punch_in(body: PunchInIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    employee = require_employee(db, body.emp_code)
    punched_at = truncate_to_second(
        from_epoch_millis(body.punched_at) if body.punched_at is not None else utc_now()
    )
    attendance_day = attendance_date_of(
        punched_at, employee["shift_start"], employee["shift_end"]
    )
    status = body.status.value
    document = {
        "emp_code": employee["emp_code"],
        "date": attendance_day.isoformat(),
        "status": status,
        "punch_in": punched_at,
        "punch_out": None,
        "history": [],
        **derive_metrics(attendance_day, employee["shift_start"], employee["shift_end"],
                         status, punched_at, None),
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_to_api(document)


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut, tags=["attendance"],
          summary="Record a punch-out", responses=NOT_FOUND_OR_CONFLICT,
          description="Closes the employee's most recent record whose punch_in is at or before punched_at, which "
                      "covers overnight shifts. Computes work_hours (R4), overtime_minutes (R3) and half_day "
                      "(R5). A conditional update guarantees that concurrent punch-outs yield exactly one 200.")
def punch_out(body: PunchOutIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    employee = require_employee(db, body.emp_code)
    punched_at = truncate_to_second(
        from_epoch_millis(body.punched_at) if body.punched_at is not None else utc_now()
    )
    candidate = db.attendance_logs.find_one(
        {"emp_code": employee["emp_code"], "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="no punch-in found at or before punched_at")
    punched_in_at = candidate["punch_in"]
    elapsed = punched_at - punched_in_at
    if elapsed <= timedelta(0):
        raise bad_request("punched_at must be after punch_in")
    if elapsed > timedelta(hours=24):
        raise bad_request("punched_at must be within 24 hours of punch_in")
    attendance_day = date.fromisoformat(candidate["date"])
    metrics = derive_metrics(attendance_day, employee["shift_start"], employee["shift_end"],
                             candidate["status"], punched_in_at, punched_at)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **metrics}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return record_to_api(updated)


@app.get("/attendance", response_model=AttendancePageOut, tags=["attendance"],
         summary="List attendance records",
         description="Sorted by date descending, then emp_code ascending. Sorting and paging happen in MongoDB, "
                      "so the collection is never loaded into memory.")
def list_attendance(
    request: Request,
    emp_code: Annotated[str | None, Query(max_length=50)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
) -> dict[str, Any]:
    db = get_db(request)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise bad_request("date_from must not be after date_to")
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(ATTENDANCE_SORT)
    items = [record_to_api(document)
             for document in cursor.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut,
           tags=["attendance"], summary="Regularize one day's record",
           responses=NOT_FOUND_OR_CONFLICT,
           description="Corrects a record addressed by (emp_code, date). Omitted fields are unchanged; the "
                       "derived fields are recomputed from the final times and exactly one audit entry is "
                       "appended. The update is a compare-and-swap, so a concurrent correction loses with 409 "
                       "instead of overwriting the other history entry.")
def regularize_attendance(
    body: RegularizeIn,
    request: Request,
    emp_code: Annotated[str, Path(min_length=1, max_length=50)],
    attendance_date: Annotated[date, Path(alias="date")],
) -> dict[str, Any]:
    db = get_db(request)
    employee = require_employee(db, emp_code)
    date_str = attendance_date.isoformat()
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if current is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")
    history_before = current.get("history") or []
    before = normalised_record(current)
    after_status = body.status.value if body.status is not None else before["status"]
    supplies_times = body.punch_in is not None or body.punch_out is not None
    if after_status in NON_PRESENCE_STATUSES:
        if supplies_times:
            raise bad_request("ABSENT and LEAVE records cannot carry punch times")
        punch_in_at: datetime | None = None
        punch_out_at: datetime | None = None
    else:
        punch_in_at = from_epoch_millis(body.punch_in) if body.punch_in is not None \
            else before["punch_in"]
        punch_out_at = from_epoch_millis(body.punch_out) if body.punch_out is not None \
            else before["punch_out"]
        if punch_in_at is None:
            raise bad_request("a presence status requires punch_in")
        resolved_day = attendance_date_of(punch_in_at, employee["shift_start"], employee["shift_end"])
        if resolved_day != attendance_date:
            raise bad_request("punch_in must stay on the record's attendance date")
        if punch_out_at is not None:
            if punch_out_at <= punch_in_at:
                raise bad_request("punch_out must be after punch_in")
            if punch_out_at - punch_in_at > timedelta(hours=24):
                raise bad_request("punch_out must be within 24 hours of punch_in")
    after = {
        "status": after_status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        **derive_metrics(attendance_date, employee["shift_start"], employee["shift_end"],
                         after_status, punch_in_at, punch_out_at),
    }
    changes: dict[str, dict[str, Any]] = {}
    for field in TRACKED_FIELDS:
        if before[field] != after[field]:
            changes[field] = {"from": before[field], "to": after[field]}
    if not changes:
        raise bad_request("the request does not change anything")
    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "$and": compare_and_swap_expression(before, len(history_before))},
        {"$set": after, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409,
                            detail="record changed concurrently; re-read it and retry")
    return record_to_api(updated)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut,
         tags=["analytics"], summary="Monthly attendance summary for one employee",
         responses=NOT_FOUND,
         description="Backed by a MongoDB aggregation that reads the stored derived values. working_days "
                     "follows R7 from joined_on; attendance_pct is null when the month has no working days.")
def employee_monthly(
    request: Request,
    emp_code: Annotated[str, Path(min_length=1, max_length=50)],
    month: Month = ...,
) -> dict[str, Any]:
    db = get_db(request)
    employee = require_employee(db, emp_code)
    month_start, month_end = month_bounds(month)
    totals = next(
        iter(db.attendance_logs.aggregate(
            employee_monthly_pipeline(employee["emp_code"], month_start, month_end)
        )),
        {},
    )
    working_days = working_days_between(
        employee["joined_on"],
        date.fromisoformat(month_start),
        date.fromisoformat(month_end),
    )
    present_days = float(totals.get("present_days") or 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    )
    return {
        "emp_code": employee["emp_code"],
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(totals.get("leave_days") or 0),
        "late_count": int(totals.get("late_count") or 0),
        "total_late_minutes": int(totals.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes") or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut,
         tags=["analytics"], summary="Department summary for a month",
         description="One item per department, sorted ascending. headcount comes from the employees collection "
                     "so people with no logs still count and people who have not joined yet do not; the log "
                     "metrics come from an aggregation on attendance_logs.")
def department_summary(
    request: Request,
    month: Month = ...,
    department: Annotated[str | None, Query(max_length=50)] = None,
) -> dict[str, Any]:
    db = get_db(request)
    month_start, month_end = month_bounds(month)
    headcount = {
        row["department"]: int(row["headcount"])
        for row in db.employees.aggregate(department_headcount_pipeline(month_end, department))
    }
    metrics = {
        row["department"]: row
        for row in db.attendance_logs.aggregate(
            department_metrics_pipeline(month_start, month_end, department)
        )
    }
    items = []
    for name in sorted(headcount):
        if headcount[name] <= 0:
            continue
        row = metrics.get(name, {})
        items.append(
            {
                "department": name,
                "headcount": headcount[name],
                "present_days": float(row.get("present_days") or 0),
                "avg_work_hours": row.get("avg_work_hours"),
                "late_count": int(row.get("late_count") or 0),
                "total_late_minutes": int(row.get("total_late_minutes") or 0),
                "leave_count": int(row.get("leave_count") or 0),
                "on_duty_count": int(row.get("on_duty_count") or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut, tags=["analytics"],
         summary="Late-comers leaderboard for a month",
         description="Ranks employees by total late_minutes with standard competition ranking, so ties share a "
                     "rank and the next rank is skipped. limit is applied after ranking.")
def late_leaderboard(
    request: Request,
    month: Month = ...,
    limit: LeaderboardLimit = 10,
    department: Annotated[str | None, Query(max_length=50)] = None,
) -> dict[str, Any]:
    db = get_db(request)
    month_start, month_end = month_bounds(month)
    emp_codes = None
    if department is not None:
        emp_codes = [row["emp_code"]
                     for row in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    rows = list(
        db.attendance_logs.aggregate(
            late_leaderboard_pipeline(month_start, month_end, emp_codes, limit)
        )
    )
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut, tags=["analytics"],
         summary="Daily attendance trend for a department", responses=NOT_FOUND,
         description="Exactly one row per calendar day from from to inclusive, weekends and empty days "
                     "included, produced with $densify. moving_avg_7d averages the non-null attendance_rate "
                     "values of the current row and up to six preceding rows inside the range.")
def department_trend(
    request: Request,
    department: Annotated[str, Path(min_length=1, max_length=50)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> dict[str, Any]:
    db = get_db(request)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="unknown department")
    validate_trend_range(date_from, date_to)
    rows = list(db.employees.aggregate(department_trend_pipeline(department, date_from, date_to)))
    return {"department": department, "items": rows}


def validate_trend_range(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise bad_request("to must not be before from")
    if (date_to - date_from).days + 1 > MAX_TREND_DAYS:
        raise bad_request(f"the range must not exceed {MAX_TREND_DAYS} days")


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut, tags=["admin"],
         summary="MongoDB explain for an endpoint's main query",
         description="Runs explain with executionStats on the main query or pipeline of the named endpoint, "
                      "built by the same function the endpoint uses.")
def explain_endpoint(
    request: Request,
    endpoint: ExplainTarget,
    emp_code: Annotated[str | None, Query(max_length=50)] = None,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    department: Annotated[str | None, Query(max_length=50)] = None,
    limit: LeaderboardLimit = 10,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Status | None = None,
    date_from_trend: Annotated[date | None, Query(alias="from")] = None,
    date_to_trend: Annotated[date | None, Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
) -> dict[str, Any]:
    db = get_db(request)
    collection, command = build_explain_command(
        db, endpoint, emp_code, month, department, limit,
        date_from, date_to, status, date_from_trend, date_to_trend, page, page_size,
    )
    try:
        raw = db.command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as error:
        logger.error("explain failed endpoint=%s error=%s", endpoint.value, type(error).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")
    explained = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint.value, "collection": collection, "explain": explained}


def build_explain_command(
    db: Database,
    endpoint: ExplainTarget,
    emp_code: str | None,
    month: str | None,
    department: str | None,
    limit: int,
    date_from: date | None,
    date_to: date | None,
    status: Status | None,
    trend_from: date | None,
    trend_to: date | None,
    page: int,
    page_size: int,
) -> tuple[str, dict[str, Any]]:
    if endpoint is ExplainTarget.ATTENDANCE_LIST:
        if date_from is not None and date_to is not None and date_from > date_to:
            raise bad_request("date_from must not be after date_to")
        return "attendance_logs", {
            "find": "attendance_logs",
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "projection": {"_id": 0},
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    if endpoint is ExplainTarget.DEPARTMENT_TREND:
        if department is None:
            raise bad_request("department is required for department_trend")
        if trend_from is None or trend_to is None:
            raise bad_request("from and to are required for department_trend")
        validate_trend_range(trend_from, trend_to)
        return "employees", {
            "aggregate": "employees",
            "pipeline": department_trend_pipeline(department, trend_from, trend_to),
            "cursor": {},
        }
    if month is None:
        raise bad_request(f"month is required for {endpoint.value}")
    month_start, month_end = month_bounds(month)
    if endpoint is ExplainTarget.EMPLOYEE_MONTHLY:
        if emp_code is None:
            raise bad_request("emp_code is required for employee_monthly")
        return "attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(emp_code, month_start, month_end),
            "cursor": {},
        }
    if endpoint is ExplainTarget.DEPARTMENT_SUMMARY:
        return "attendance_logs", {
            "aggregate": "attendance_logs",
            "pipeline": department_metrics_pipeline(month_start, month_end, department),
            "cursor": {},
        }
    emp_codes = None
    if department is not None:
        emp_codes = [row["emp_code"] for row in
                     db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    return "attendance_logs", {
        "aggregate": "attendance_logs",
        "pipeline": late_leaderboard_pipeline(month_start, month_end, emp_codes, limit),
        "cursor": {},
    }
