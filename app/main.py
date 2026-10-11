"""
Employee Attendance & Analytics API
====================================
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real env vars win over .env)

All code in this single file per the assignment contract.
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()  # real env vars win; load_dotenv does not override existing env vars

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB",  "attendance_db")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones
client = MongoClient(MONGO_URI, tz_aware=True)
db     = client[MONGO_DB]

IST   = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Epoch ms range: below 100_000_000_000 looks like seconds -> reject with 422
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES      = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
def create_indexes() -> None:
    db.employees.create_indexes([
        IndexModel([("emp_code", ASCENDING)], unique=True, name="emp_code_unique"),
        IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp"),
        IndexModel([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"),
    ])
    db.attendance_logs.create_indexes([
        IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"),
        IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"),
        IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date"),
        IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin"),
    ])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Epoch-ms / datetime helpers  (integer math, no float rounding)
# ---------------------------------------------------------------------------

def to_ms(dt: datetime) -> int:
    """Convert an aware datetime to integer epoch milliseconds."""
    delta = dt.astimezone(timezone.utc) - EPOCH
    return delta // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Convert integer epoch milliseconds to a UTC-aware datetime."""
    return EPOCH + timedelta(milliseconds=ms)


# ---------------------------------------------------------------------------
# Serialiser: MongoDB doc -> API-safe dict
# ---------------------------------------------------------------------------

def _convert_history_entry(entry: dict) -> dict:
    out = {
        "at":     to_ms(entry["at"]),
        "by":     entry["by"],
        "reason": entry["reason"],
    }
    changes: dict = {}
    for field, chg in (entry.get("changes") or {}).items():
        from_v = chg.get("from")
        to_v   = chg.get("to")
        if field in ("punch_in", "punch_out"):
            from_v = to_ms(from_v) if isinstance(from_v, datetime) else from_v
            to_v   = to_ms(to_v)   if isinstance(to_v, datetime)   else to_v
        changes[field] = {"from": from_v, "to": to_v}
    out["changes"] = changes
    return out


def to_api_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(doc["created_at"])
    return out


def to_api_record(doc: dict) -> dict:
    out: dict = {}
    out["emp_code"]         = doc["emp_code"]
    out["date"]             = doc["date"]
    out["status"]           = doc["status"]
    out["punch_in"]         = to_ms(doc["punch_in"])  if doc.get("punch_in")  else None
    out["punch_out"]        = to_ms(doc["punch_out"]) if doc.get("punch_out") else None
    out["work_hours"]       = doc.get("work_hours")
    out["late_minutes"]     = doc.get("late_minutes", 0)
    out["overtime_minutes"] = doc.get("overtime_minutes", 0)
    out["half_day"]         = doc.get("half_day", False)
    history = doc.get("history") or []
    out["history"] = [_convert_history_entry(h) for h in history]
    return out


def _make_json_safe(doc: Any) -> Any:
    """Safely convert any MongoDB / BSON doc into standard JSON."""
    return json.loads(json_util.dumps(doc))


# ---------------------------------------------------------------------------
# Business-rule helpers  (R1-R5)
# ---------------------------------------------------------------------------

def _truncate_to_second(dt: datetime) -> datetime:
    """R1: drop sub-second precision."""
    return dt.replace(microsecond=0)


def attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: IST calendar date for a punch-in.
    Overnight shift (shift_end <= shift_start): a punch-in whose IST time is
    strictly before shift_end belongs to the shift that started the previous day.
    """
    dt_utc = from_ms(punch_in_ms)
    dt_ist = _truncate_to_second(dt_utc.astimezone(IST))

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)

    if is_overnight:
        punch_time_min = dt_ist.hour * 60 + dt_ist.minute
        shift_end_min  = eh * 60 + em
        if punch_time_min < shift_end_min:
            return (dt_ist.date() - timedelta(days=1)).isoformat()

    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: late only if strictly > 10 min (600 s) after shift_start.
    Returns floor(seconds_since_shift_start / 60) when late, else 0.
    Builds shift_dt on date_str (attendance date) to handle overnight shifts correctly.
    """
    sh, sm   = map(int, shift_start.split(":"))
    base     = dt_date.fromisoformat(date_str)
    shift_dt = datetime(base.year, base.month, base.day, sh, sm, tzinfo=IST)
    diff_s   = int((punch_in_dt.astimezone(IST) - shift_dt).total_seconds())
    return 0 if diff_s <= 600 else diff_s // 60


def compute_overtime(punch_out_dt: datetime, shift_end: str, shift_start: str, date_str: str) -> int:
    """
    R3: floor minutes between shift_end and punch_out; 0 if < 30 min.
    For overnight shifts shift_end falls on the next calendar day.
    """
    sh, sm   = map(int, shift_start.split(":"))
    eh, em   = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base = dt_date.fromisoformat(date_str)
    end_date = base + timedelta(days=1) if is_overnight else base
    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    diff_s = int((punch_out_dt.astimezone(IST) - shift_end_ist).total_seconds())
    if diff_s < 1800:
        return 0
    return diff_s // 60


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: seconds / 3600, half-up to 2 decimals."""
    seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    raw = Decimal(seconds) / Decimal(3600)
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    return work_hours < 4.50


def _round2(x) -> float:
    """R8: 2-decimal half-up."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round4(x) -> float:
    """R8: 4-decimal half-up (rates)."""
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _validate_date_str(s: str) -> dt_date:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise ValueError(f"Invalid date format: {s}")
    return dt_date.fromisoformat(s)


def _month_range(month: str):
    y, m  = map(int, month.split("-"))
    first = dt_date(y, m, 1)
    last  = dt_date(y + 1, 1, 1) - timedelta(days=1) if m == 12 else dt_date(y, m + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def _working_days_in_range(start: dt_date, end: dt_date) -> int:
    """Count Mon-Fri days between start and end inclusive."""
    count, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _validate_epoch_ms(val: Any) -> Optional[int]:
    """Validate epoch ms: must be int in [EPOCH_MS_MIN, EPOCH_MS_MAX], not bool/float."""
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError("Must be an integer epoch timestamp in milliseconds")
    if val < EPOCH_MS_MIN or val > EPOCH_MS_MAX:
        raise ValueError("Timestamp out of valid epoch milliseconds range")
    return val


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(..., pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field(..., pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        _validate_date_str(v)
        return v

    @model_validator(mode="after")
    def check_shifts(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end cannot be equal")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[Any] = None
    status:     str = "PRESENT"

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return _validate_epoch_ms(v)


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[Any] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return _validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    regularized_by: str = Field(..., min_length=1)
    reason:         str = Field(..., min_length=1)
    status:         Optional[str] = None
    punch_in:       Optional[Any] = None
    punch_out:      Optional[Any] = None

    @field_validator("status")
    @classmethod
    def check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in")
    @classmethod
    def check_punch_in(cls, v: Any) -> Optional[int]:
        return _validate_epoch_ms(v)

    @field_validator("punch_out")
    @classmethod
    def check_punch_out(cls, v: Any) -> Optional[int]:
        return _validate_epoch_ms(v)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# ---------------------------------------------------------------------------
# POST /employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Employee with this emp_code already exists")
    doc.pop("_id", None)
    return to_api_employee(doc)


# ---------------------------------------------------------------------------
# GET /employees
# ---------------------------------------------------------------------------

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [to_api_employee(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# POST /attendance/punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    now_ms   = to_ms(datetime.now(tz=timezone.utc))
    raw_ms   = body.punched_at if body.punched_at is not None else now_ms
    raw_ms   = (raw_ms // 1000) * 1000   # R1: truncate to whole seconds
    punch_dt = from_ms(raw_ms)           # UTC-aware

    date_str  = attendance_date(raw_ms, emp["shift_start"], emp["shift_end"])
    late      = compute_late_minutes(punch_dt, emp["shift_start"], date_str)

    doc = {
        "emp_code":         body.emp_code,
        "date":             date_str,
        "status":           body.status,
        "punch_in":         punch_dt,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    doc.pop("_id", None)
    return to_api_record(doc)


# ---------------------------------------------------------------------------
# POST /attendance/punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    now_ms   = to_ms(datetime.now(tz=timezone.utc))
    raw_ms   = body.punched_at if body.punched_at is not None else now_ms
    raw_ms   = (raw_ms // 1000) * 1000
    punch_dt = from_ms(raw_ms)

    # Find the most recent record with punch_in <= punch_out time
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="That record is already punched out")

    pi_dt = record["punch_in"]
    if punch_dt <= pi_dt:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (punch_dt - pi_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punched_at more than 24 hours after punch_in")

    # Compute derived values
    wh = compute_work_hours(_truncate_to_second(pi_dt), _truncate_to_second(punch_dt))
    ot = compute_overtime(_truncate_to_second(punch_dt), emp["shift_end"], emp["shift_start"], record["date"])
    hd = compute_half_day(wh)

    # Atomic update: match punch_out=None ensures concurrent punch-outs yield exactly one 200
    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_dt, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="That record is already punched out")

    return to_api_record(result)


# ---------------------------------------------------------------------------
# GET /attendance
# ---------------------------------------------------------------------------

def _build_attendance_query(
    emp_code:  Optional[str],
    date_from: Optional[str],
    date_to:   Optional[str],
    status:    Optional[str],
) -> dict:
    if date_from:
        try:
            _validate_date_str(date_from)
        except ValueError:
            raise HTTPException(422, detail="Invalid date_from")
    if date_to:
        try:
            _validate_date_str(date_to)
        except ValueError:
            raise HTTPException(422, detail="Invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail="Invalid status")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to:   Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status:    Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q     = _build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [to_api_record(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        _validate_date_str(date)
    except ValueError:
        raise HTTPException(422, detail="Invalid date format in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, detail="No attendance record for that date")

    new_status = body.status or record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="ABSENT/LEAVE cannot have punch times")
        new_pi_dt = None
        new_po_dt = None
    else:
        existing_pi = record.get("punch_in")
        new_pi_dt   = from_ms((body.punch_in // 1000) * 1000) if body.punch_in is not None else existing_pi
        if new_pi_dt is None:
            raise HTTPException(422, detail="Presence status requires punch_in")

        # R1: punch_in must map to the record's date
        pi_date_check = attendance_date(
            to_ms(_truncate_to_second(new_pi_dt)), emp["shift_start"], emp["shift_end"]
        )
        if pi_date_check != date:
            raise HTTPException(422, detail="punch_in (R1) does not match record date")

        existing_po = record.get("punch_out")
        new_po_dt   = from_ms((body.punch_out // 1000) * 1000) if body.punch_out is not None else existing_po

        if new_po_dt is not None:
            if new_po_dt <= new_pi_dt:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_po_dt - new_pi_dt).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE") or new_pi_dt is None:
        new_late = 0
        new_wh   = None
        new_ot   = 0
        new_hd   = False
    else:
        new_late = compute_late_minutes(_truncate_to_second(new_pi_dt), emp["shift_start"], date)
        if new_po_dt is not None:
            new_wh = compute_work_hours(_truncate_to_second(new_pi_dt), _truncate_to_second(new_po_dt))
            new_ot = compute_overtime(_truncate_to_second(new_po_dt), emp["shift_end"], emp["shift_start"], date)
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    old_pi_ms = to_ms(record["punch_in"])  if record.get("punch_in")  else None
    old_po_ms = to_ms(record["punch_out"]) if record.get("punch_out") else None
    new_pi_ms_v = to_ms(_truncate_to_second(new_pi_dt)) if new_pi_dt else None
    new_po_ms_v = to_ms(_truncate_to_second(new_po_dt)) if new_po_dt else None

    changes: dict = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    if new_pi_ms_v != old_pi_ms:
        changes["punch_in"] = {"from": old_pi_ms, "to": new_pi_ms_v}
    if new_po_ms_v != old_po_ms:
        changes["punch_out"] = {"from": old_po_ms, "to": new_po_ms_v}
    if new_wh != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_ot != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
    if new_hd != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No fields changed")

    # Store punch_in/punch_out changes as BSON datetimes
    bson_changes: dict = {}
    for field, chg in changes.items():
        if field in ("punch_in", "punch_out"):
            bson_changes[field] = {
                "from": from_ms(chg["from"]) if chg["from"] is not None else None,
                "to":   from_ms(chg["to"])   if chg["to"]   is not None else None,
            }
        else:
            bson_changes[field] = chg

    history_entry = {
        "at":      datetime.now(tz=timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": bson_changes,
    }

    set_payload: dict = {
        "status":           new_status,
        "punch_in":         _truncate_to_second(new_pi_dt) if new_pi_dt else None,
        "punch_out":        _truncate_to_second(new_po_dt) if new_po_dt else None,
        "work_hours":       new_wh,
        "late_minutes":     new_late,
        "overtime_minutes": new_ot,
        "half_day":         new_hd,
    }

    # Optimistic lock: match on history length AND current status, punch_in, punch_out
    # to prevent silent overwrite of concurrent punch-outs or edits
    cur_len = len(record.get("history") or [])
    update_filter = {
        "_id":       record["_id"],
        "status":    record["status"],
        "punch_in":  record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr":     {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, cur_len]},
    }
    result = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": set_payload, "$push": {"history": history_entry}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent edit conflict; please retry")

    return to_api_record(result)


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------

def _build_employee_monthly_pipeline(emp_code: str, first_str: str, last_str: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_str, "$lte": last_str}}},
        {
            "$group": {
                "_id":                    None,
                "late_count":             {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes":     {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                "leave_days":             {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": {
                            "if": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "then": {
                                "$cond": {
                                    "if": {"$and": [
                                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                    ]},
                                    "then": {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                    "else": 0,
                                }
                            },
                            "else": 0,
                        }
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")

    first_str, last_str = _month_range(month)
    first_date = dt_date.fromisoformat(first_str)
    last_date  = dt_date.fromisoformat(last_str)
    joined     = dt_date.fromisoformat(emp["joined_on"])
    wd_start   = max(joined, first_date)
    working_days = _working_days_in_range(wd_start, last_date) if wd_start <= last_date else 0

    pipeline = _build_employee_monthly_pipeline(emp_code, first_str, last_str)
    rows = list(db.attendance_logs.aggregate(pipeline, hint="emp_date_unique"))
    r = rows[0] if rows else {
        "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0,
        "leave_days": 0, "present_days": 0.0,
    }

    pct = _round2(r["present_days"] / working_days * 100) if working_days > 0 else None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           r["present_days"],
        "leave_days":             r["leave_days"],
        "late_count":             r["late_count"],
        "total_late_minutes":     r["total_late_minutes"],
        "total_overtime_minutes": r["total_overtime_minutes"],
        "attendance_pct":         pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------

def _build_dept_summary_pipeline(month: str, department: Optional[str]) -> list:
    first_str, last_str = _month_range(month)
    emp_match: dict = {"joined_on": {"$lte": last_str}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from":         "attendance_logs",
                "localField":   "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first_str, "$lte": last_str}}}],
                "as":           "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "in": {
                                "$cond": {
                                    "if": {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    "then": {
                                        "$cond": {
                                            "if": {"$and": [
                                                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 2]},
                                                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 6]},
                                            ]},
                                            "then": {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                            "else": 0,
                                        }
                                    },
                                    "else": 0,
                                }
                            }
                        }
                    }
                },
                "late_count":         {"$size": {"$filter": {"input": "$logs", "cond": {"$gt": ["$$this.late_minutes", 0]}}}},
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count":        {"$size": {"$filter": {"input": "$logs", "cond": {"$eq": ["$$this.status", "LEAVE"]}}}},
                "on_duty_count":      {"$size": {"$filter": {"input": "$logs", "cond": {"$eq": ["$$this.status", "ON_DUTY"]}}}},
                "wh_cents": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$isNumber": "$$this.work_hours"},
                                    ]
                                },
                            }
                        },
                        "in": {"$round": [{"$multiply": ["$$this.work_hours", 100]}, 0]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id":                "$department",
                "headcount":          {"$sum": 1},
                "present_days":       {"$sum": "$present_days"},
                "late_count":         {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count":        {"$sum": "$leave_count"},
                "on_duty_count":      {"$sum": "$on_duty_count"},
                "wh_cents_sum":       {"$sum": {"$sum": "$wh_cents"}},
                "wh_count":           {"$sum": {"$size": "$wh_cents"}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = _build_dept_summary_pipeline(month, department)
    rows     = list(db.employees.aggregate(pipeline, hint="dept_joined"))
    items = []
    for r in rows:
        wh_count = r["wh_count"]
        if wh_count > 0:
            avg_wh = float(
                (Decimal(int(r["wh_cents_sum"])) / Decimal(wh_count) / Decimal(100))
                .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        else:
            avg_wh = None

        items.append({
            "department":         r["_id"],
            "headcount":          r["headcount"],
            "present_days":       r["present_days"],
            "avg_work_hours":     avg_wh,
            "late_count":         r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count":        r["leave_count"],
            "on_duty_count":      r["on_duty_count"],
        })
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------

def _build_late_leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list:
    first_str, last_str = _month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_str, "$lte": last_str}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count":         {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from":         "employees",
                "localField":   "_id",
                "foreignField": "emp_code",
                "as":           "emp",
            }
        },
        # Drop logs whose employee no longer exists
        {"$match": {"emp": {"$not": {"$size": 0}}}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id":                0,
                "rank":               1,
                "emp_code":           "$_id",
                "name":               "$emp.name",
                "department":         "$emp.department",
                "total_late_minutes": 1,
                "late_count":         1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _build_late_leaderboard_pipeline(month, department, limit)
    rows     = list(db.attendance_logs.aggregate(pipeline, hint="date_desc_emp"))
    return {"month": month, "items": rows}


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------

def _build_dept_trend_pipeline(department: str, from_str: str, to_str: str) -> list:
    emp_docs = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    emp_codes = [e["emp_code"] for e in emp_docs]
    start_dt = datetime.fromisoformat(from_str + "T00:00:00+00:00")
    end_dt   = datetime.fromisoformat(to_str + "T23:59:59+00:00")

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {
                                "date": from_str,
                                "status": None,
                                "half_day": False,
                                "late_minutes": 0,
                                "_seed": True,
                            }
                        ]
                    }
                ]
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"joined_on": 1, "_id": 0}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_dt"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_dt"}, 6]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {
                            "documents": [-6, "current"],
                        },
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
                "attendance_rate_raw": 1,
                "moving_avg_7d_raw": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_str:   str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_str:     str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_date = _validate_date_str(from_str)
        to_date   = _validate_date_str(to_str)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))

    if to_date < from_date:
        raise HTTPException(422, detail="'to' must be >= 'from'")
    if (to_date - from_date).days > 91:
        raise HTTPException(422, detail="Date range exceeds 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Unknown department")

    pipeline = _build_dept_trend_pipeline(department, from_str, to_str)
    rows     = list(db.attendance_logs.aggregate(pipeline, hint="date_desc_emp"))

    items = []
    for r in rows:
        rate = _round4(r["attendance_rate_raw"]) if r["attendance_rate_raw"] is not None else None
        mavg = _round4(r["moving_avg_7d_raw"])   if r["moving_avg_7d_raw"] is not None else None
        items.append({
            "date":            r["date"],
            "is_working_day":  r["is_working_day"],
            "headcount":       r["headcount"],
            "present_count":   r["present_count"],
            "late_count":      r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d":   mavg,
        })

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to:    Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status:     Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    from_str:   Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_str:     Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    def req(val, name):
        if val is None:
            raise HTTPException(422, detail=f"Missing required parameter: {name}")
        return val

    if endpoint == "attendance_list":
        q    = _build_attendance_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        # Use db.command with executionStats for consistent verbosity with other explain endpoints
        cmd  = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}
        doc  = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _make_json_safe(doc)}

    if endpoint == "employee_monthly":
        req(emp_code, "emp_code"); req(month, "month")
        first_str, last_str = _month_range(month)
        pipeline = _build_employee_monthly_pipeline(emp_code, first_str, last_str)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
            "hint": "emp_date_unique",
        }
        doc = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _make_json_safe(doc)}

    if endpoint == "department_summary":
        req(month, "month")
        pipeline = _build_dept_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
            "hint": "dept_joined",
        }
        doc = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": _make_json_safe(doc)}

    if endpoint == "late_leaderboard":
        req(month, "month")
        pipeline = _build_late_leaderboard_pipeline(month, department, limit)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
            "hint": "date_desc_emp",
        }
        doc = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _make_json_safe(doc)}

    if endpoint == "department_trend":
        req(department, "department"); req(from_str, "from"); req(to_str, "to")
        try:
            from_d = _validate_date_str(from_str)
            to_d   = _validate_date_str(to_str)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        if to_d < from_d:
            raise HTTPException(422, detail="'to' must be >= 'from'")
        if (to_d - from_d).days > 91:
            raise HTTPException(422, detail="Date range exceeds 92 days")
        if not db.employees.find_one({"department": department}):
            raise HTTPException(404, detail="Unknown department")

        pipeline = _build_dept_trend_pipeline(department, from_str, to_str)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
            "hint": "date_desc_emp",
        }
        doc = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _make_json_safe(doc)}

    raise HTTPException(422, detail="Unknown endpoint")
