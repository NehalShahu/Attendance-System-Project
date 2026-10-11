# DECISIONS.md

**1. Indexes.** On `employees`: `{emp_code}` (unique key), `{department, emp_code}`, and `{department, joined_on}` (headcount filter). On `attendance_logs`: compound unique `{emp_code, date}` (concurrency safety and fast lookups), `{date: -1, emp_code: 1}` (pagination, leaderboard, and range filters), `{status: 1, date: -1, emp_code: 1}`, and `{emp_code: 1, punch_in: -1}` (punch-out lookup). Queries execute via IXSCAN without COLLSCAN.

**2. Punch-in race.** Both concurrent requests find the employee and run `insert_one`. The unique index on `{emp_code, date}` atomically admits one document (HTTP 201). The duplicate write raises `DuplicateKeyError`, caught to return HTTP 409 without any TOCTOU window.

**3. Leaderboard ties.** The leaderboard pipeline uses `$setWindowFields` with `$rank` (ranking: 1, 2, 2, 4) and `$match: {rank: {"$lte": limit}}`. Employees tied at the rank cutoff are all included, so results may exceed `limit`. Ties are ordered deterministically by `total_late_minutes DESC, emp_code ASC`.

**4. Headcount.** The department summary starts from `employees` (`joined_on <= month_end`) with a `$lookup` to logs. Employees with zero records produce an empty array but still count toward `headcount` via `{"$sum": 1}`.

**5. 100x scale.** Maintain incremental daily attendance rollups updated on punch and regularize events, avoiding repeated whole-month `$lookup` joins and raw log aggregations.
