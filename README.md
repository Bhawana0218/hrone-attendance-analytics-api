# Employee Attendance & Analytics API

FastAPI + MongoDB backend for punch in/out, manual regularization with an audit trail,
and HR analytics. Employees are identified by a customer-supplied `emp_code`, attendance
records are addressed by `(emp_code, date)`, and every instant crosses the API boundary
as integer epoch milliseconds.

## Implemented

| Endpoint | Notes |
|---|---|
| `GET /health` | 200 only when MongoDB answers a ping, otherwise 503 |
| `POST /employees` | Unique `emp_code` enforced by a database unique index, 409 on duplicate |
| `GET /employees` | Department filter, sorted by `emp_code`, paginated, `total` reflects filters |
| `POST /attendance/punch-in` | R1 attendance date, R2 lateness, race-safe through a unique index |
| `POST /attendance/punch-out` | R3/R4/R5, closes the most recent record at or before the punch, works across midnight |
| `GET /attendance` | Sorted `date` desc then `emp_code` asc, sorted and paged in MongoDB |
| `PATCH /attendance/{emp_code}/{date}` | Recomputes derived fields, appends exactly one history entry, compare-and-swap |
| `GET /analytics/employees/{emp_code}/monthly` | Aggregation over the stored derived values |
| `GET /analytics/departments/summary` | Headcount from `employees`, log metrics from `attendance_logs` |
| `GET /analytics/leaderboard/late` | Competition ranking with ties, `limit` applied after ranking |
| `GET /analytics/departments/{department}/trend` | `$densify` gap filling plus a 7-row moving average |
| `GET /admin/explain/{endpoint}` | `executionStats` for any of the five main queries, built by the same functions |

All application code is in `app/main.py`, as the contract requires. The file is ordered
top to bottom: configuration, time and business-rule helpers (pure functions, no I/O),
request/response models, serialisation, aggregation builders, database lifecycle, routes.

## Stack

Python 3.11+, FastAPI, PyMongo, Pydantic v2, MongoDB 6.0+ (developed and tested against
7.0.31). `pytest` and `httpx` are test-only.

## Prerequisites

- Python 3.11 or newer
- A reachable MongoDB 6.0+ instance

## MongoDB

```bash
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
```

MongoDB Atlas (including the free M0 tier) works as well; put its connection string in
`MONGO_URI`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit if your database differs
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | Connection string |
| `MONGO_DB` | `attendance_db` | Database name |
| `MONGO_SERVER_SELECTION_TIMEOUT_MS` | `5000` | How long startup waits for MongoDB |
| `LOG_LEVEL` | `INFO` | Standard library log level |

Real environment variables always take priority over `.env`. No credentials are stored in
the repository; `.env` is git-ignored and `.env.example` holds safe local placeholders only.

## Run

```bash
python sample_seed.py            # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```

All indexes are created at startup, idempotently; there are no manual steps. The app is
ready when `GET /health` returns 200.

## API documentation

Interactive docs at `http://127.0.0.1:8000/docs`, schema at `/openapi.json`. The OpenAPI
document is generated from the Pydantic models, so field names, ranges and enums match the
contract (`openapi.yaml`), including the epoch-millisecond bounds and the declared 404, 409
and 503 responses.

## Tests

```bash
pip install -r requirements.txt
pytest -q                                  # 145 tests
pytest -q tests/test_analytics.py          # one area
```

The tests need a real MongoDB; they never mock the database or the aggregation results.
They use `MONGO_TEST_URI` (default `MONGO_URI`, else `mongodb://localhost:27017`) and a
separate `MONGO_TEST_DB` (default `attendance_test_db`), which is dropped afterwards. If
MongoDB cannot be reached the suite skips with an explicit message rather than reporting
passes it cannot substantiate.

`tests/test_scale.py` builds roughly 110,000 attendance records and asserts that every
explain plan contains `IXSCAN` and no `COLLSCAN`, and that each endpoint answers in well
under two seconds.

## Known limitations

- `attendance_pct` and `working_days` are computed in Python: the denominator comes from
  the employee's `joined_on` and the calendar, not from the documents, so it is a scalar
  division over one aggregation result rather than a second pass over the data.
- The department trend issues two correlated `$lookup`s per day in the range, so it is
  linear in the number of days (capped at 92 by the contract) rather than constant.
- `history` is embedded in the attendance document, so it cannot grow without bound on a
  single record. That is the shape the data model specifies.
- There is no authentication: `regularized_by` is free text, as the assignment states.

## Submission contents

`app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, `DECISIONS.md`, the contract
files that came with the kit (`openapi.yaml`, `DATA_MODEL.md`, `PROBLEM_STATEMENT.docx`,
`sample_data/`, `sample_seed.py`) and `tests/`. No `.env`, no secrets, no Dockerfile, no
virtual environment.

`DECISIONS.md` answers the five questions in the template and then, in a clearly separated
section, records the supporting detail (state transitions, audit consistency, the pipeline
design, timezone handling, rounding and testing strategy).