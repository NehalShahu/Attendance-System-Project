# Employee Attendance & Analytics API

A FastAPI + MongoDB backend for employee attendance. Employees punch in and out, HR can correct a day's record (with a history of every correction), and there are analytics endpoints for monthly summaries, department summaries, a late-comers leaderboard and a daily trend. It follows `openapi.yaml` and the business rules R1 to R10.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (tested on MongoDB 7.0)

## How to run

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install the dependencies
pip install -r requirements.txt

# 3. Set the database settings (real environment variables win over a local .env)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 4. Start the app (indexes are created automatically on startup)
uvicorn app.main:app --port 8000
```

- Health check: `GET /health`
- Interactive docs (Swagger): http://localhost:8000/docs
- Optional: to load the sample data from the candidate kit, run `python sample_seed.py` from the kit folder.

## Things to know

- **One file.** All the code (endpoints, models, queries and pipelines) is in `app/main.py`.
- **Indexes.** They are created at startup and are safe to run many times. I checked on about 100,000 records that the main queries use an index scan and no collection scan. You can see this yourself with `GET /admin/explain/{endpoint}`.
- **No double punch-in.** A unique index on `(emp_code, date)` lets only one punch-in per employee per day succeed. The other request gets 409.
- **No lost corrections.** `PATCH /attendance/{emp_code}/{date}` uses optimistic locking. If two people edit the same record at the same time, one succeeds and the other gets 409.
- **Time zones.** The API uses integer epoch milliseconds. MongoDB stores UTC datetimes. Shifts and attendance dates use IST (UTC+05:30), including overnight shifts such as 22:00 to 06:00.

## What is missing

Nothing that I know of. All endpoints in `openapi.yaml` are implemented.

## More documents

- `REVIEW.md`: the defects I found in the starter code and how I fixed them.
- `DECISIONS.md`: my answers about indexes, concurrency, ties, headcount and scaling.
