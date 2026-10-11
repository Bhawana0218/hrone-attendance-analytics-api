# DECISIONS.md

1. **Indexes.** `employees` gets a unique `emp_code` (business key, and it lets a
   `$lookup` join be an index seek), `(department, joined_on)` for the headcount rules
   R9 and the leaderboard's department filter, and a plain `joined_on` because the
   headcount aggregation runs unfiltered when no department is requested.
   `attendance_logs` gets a unique `(emp_code, date)`, which both enforces one record per
   attendance day and answers "most recent record for this employee", and a compound
   `(date desc, emp_code asc)` which serves the list endpoint's sort *and* the month
   ranges of all four analytics. The direction matters: `(date asc, emp_code asc)`
   cannot satisfy the required `date desc, emp_code asc` sort and MongoDB falls back to a
   collection scan. I rejected a `status`-led index: `status` has five values, so it
   selects a third of the collection and buys nothing over filtering the date window.

2. **Punch-in race.** The date is resolved first (R1, in IST), then the record is
   inserted directly — no read-then-write. The unique `(emp_code, date)` index is the
   arbiter: one insert commits and returns 201, the rest get `DuplicateKeyError`, which I
   translate to 409. Two simultaneous punches can never both succeed, and no lock is
   needed. Punch-out is the same idea with a condition instead of an index: I read the
   most recent record whose `punch_in` is at or before `punched_at`, then close it with
   `find_one_and_update({_id, punch_out: null})`. The winner gets the document back; a
   loser gets `None` and returns 409. Because the selection is by instant rather than by
   date, it works across midnight for overnight shifts.

3. **Ties.** The leaderboard groups by `emp_code` in MongoDB, then uses
   `$setWindowFields` with `$rank`, which is standard competition ranking: equal totals
   share a rank and the next rank skips. `limit` is applied *after* ranking by matching
   `rank <= limit`, so two employees tied on rank 2 are both returned even when the limit
   is 1. Sorting by the array position would renumber the ties and would cut the tie
   group in half, which is exactly what the contract warns against.

4. **Headcount.** It comes from `employees`, never from the logs, and that is the whole
   trick. A second aggregation counts, per department, the employees whose `joined_on`
   is on or before the last day of the month (R9). It is a left join onto the log
   metrics: a department with people but no records still appears, with zeros and a null
   average, and a department whose headcount is zero for the month is omitted. Counting
   heads from the logs would drop exactly the employees the contract wants counted.
   Records whose `emp_code` matches no employee are dropped by the `$lookup`, since they
   belong to no department.

5. **One thing I would change at 100x.** The department trend. It densifies the calendar
   and then issues two correlated `$lookup`s per day, about 24 for an average request,
   so its cost is linear in the range rather than constant. At 100x I would keep the
   employee `joined_on` list in Redis or in a small `headcount_by_department_day`
   collection refreshed by a change stream, turning each day into an equality lookup and
   making the whole endpoint a single indexed pass. Everything else is already driven by
   a narrow index range.

---

## Further notes

*Supporting detail, beyond the five questions above.*

**Attendance state transitions.** A record moves `absent -> open (punched in) -> closed
(punched out)`, or straight to `ABSENT`/`LEAVE` through a correction. The database holds
the invariant that there is at most one document per `(emp_code, date)`; the unique index
holds it even when two requests race. State changes are expressed as a single
`find_one_and_update` with a condition in the filter (`punch_out: null`), so the check and
the write are one atomic operation.

**Audit consistency.** `history` is an embedded array, not a separate collection, so the
record update and its audit entry are a single `$set` + `$push` on the same document.
There is no window in which a correction can be applied without its audit trail, which is
why I did not reach for a transaction — one would add a deployment requirement (a replica
set) for no additional guarantee. Concurrent corrections use a compare-and-swap: the
update filter matches the values I read plus the current length of `history`, so a
correction computed against a stale snapshot matches nothing and returns 409 instead of
silently overwriting the other entry. I wrote the comparison as `$expr` with `$ifNull`
rather than plain equality because `{half_day: false}` does *not* match a legacy document
where the field is absent; my own first version had that bug and a regression test
(`test_regularize_legacy_record_without_history`) now pins it.

**Timestamps and time zones.** Instants are stored as BSON dates in UTC and cross the
API boundary as integer epoch milliseconds. Instants are converted with integer
arithmetic and truncated to whole seconds (R1); only shift boundaries are ever built in
IST (`timezone(timedelta(hours=5, minutes=30))`), so there is exactly one place where the
local zone enters. Calendar values stay strings. One trap worth remembering: the
overnight rule in R1 makes the attendance date depend on the employee's shift, not only on
the punch instant, so a 03:00 punch-in for a 22:00-06:00 shift belongs to the previous day
and is 300 minutes late.

**Aggregation pipelines.** Each analytics endpoint has one builder function that returns
the pipeline, and the `/admin/explain` endpoint calls the very same builder, so the
explained plan cannot drift from the real one. `employee_monthly` matches on
`(emp_code, date range)` and groups once. `department_summary` matches the month,
`$lookup`s the employee for the department, and groups by department; the average is
`sum(work_hours) / count(work_hours)` over records, which is a mean over records rather
than a mean of per-employee means. `late_leaderboard` groups by employee, `$lookup`s the
name, then ranks. `department_trend` seeds one document from the department, anchors it on
the first day of the range, lets `$densify` fill the calendar (its upper bound is
exclusive, so I pass `to + 1 day`), resolves headcount and the day's totals with two
correlated `$lookup`s, and computes `moving_avg_7d` with `$setWindowFields` using
`window: {documents: [-6, 0]}`, which ignores nulls and therefore averages only the rows
that actually have a rate.

**Rounding.** R8 asks for half-up, but MongoDB's `$round` is round-half-to-even: on
7.0 it turns 9.125 into 9.12. Inside pipelines I round half-up explicitly with
`$trunc(x * 10^p ± 0.5) / 10^p` in `Decimal128`, which is exact and propagates `null`
untouched; in Python I use `Decimal` with `ROUND_HALF_UP` over integer seconds. I
verified the difference against a real server rather than trusting the manual.

**Testing.** 145 pytest tests run against a real MongoDB (`TestClient` plus PyMongo,
no mocks), covering every status code, R1-R5 boundaries, both concurrency races with 12
threads released from a barrier, the audit trail, analytics against hand-computed
expectations, and a 109,800-record dataset that asserts every explain plan contains
`IXSCAN` and no `COLLSCAN`. The expected analytics numbers were derived by hand from the
rules, not captured from my own output.