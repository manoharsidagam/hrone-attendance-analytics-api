"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from enum import Enum


from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StrictInt
from fastapi import Query
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]


# Create MongoDB indexes when the application starts
db.employees.create_index("emp_code", unique=True)

db.employees.create_index(
    [("department", 1), ("emp_code", 1)]
)

db.attendance_logs.create_index(
    [("emp_code", 1), ("date", -1)]
)

db.attendance_logs.create_index(
    [("emp_code", 1), ("date", 1)],
    unique=True,
)

db.attendance_logs.create_index(
    [("date", -1), ("status", 1)]
)

db.attendance_logs.create_index(
    [("emp_code", 1), ("punch_in", -1)]
)


IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers

def attendance_response(doc: dict) -> dict:
    result = {}

    for field in (
        "emp_code",
        "date",
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    ):
        value = doc.get(field)

        if field == "half_day" and value is None:
            value = False
        elif field == "history" and value is None:
            value = []

        if field in ("punch_in", "punch_out") and value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            value = int(value.timestamp() * 1000)

        result[field] = value

    return result

# --------------------------------------------------------------------------- #


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str
) -> int:
    # Convert punch-in timestamp to IST
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=IST)
    else:
        punch_in = punch_in.astimezone(IST)

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    punch_minutes = punch_in.hour * 60 + punch_in.minute

    # For overnight shifts, early-morning punches belong
    # to the shift that started on the previous day.
    overnight_shift = end_minutes <= start_minutes

    if overnight_shift and punch_minutes < end_minutes:
        shift_date = punch_in.date() - timedelta(days=1)
    else:
        shift_date = punch_in.date()

    shift_start_time = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        start_h,
        start_m,
        tzinfo=IST
    )

    delay_seconds = (punch_in - shift_start_time).total_seconds()

    # Ignore lateness of 10 minutes or less.
    if delay_seconds <= 600:
        return 0

    # Count only whole minutes.
    return int(delay_seconds // 60)




def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:
    seconds = (punch_out - punch_in).total_seconds()

    return float(
        (Decimal(str(seconds)) / Decimal("3600")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str



class PunchInStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: StrictInt | None = None
    status: PunchInStatus = PunchInStatus.PRESENT



class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: StrictInt | None = None



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    from pymongo.errors import DuplicateKeyError

    # Create the employee document
    doc = body.model_dump()

    # Store created_at as a UTC datetime in MongoDB
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    # Return API timestamps as epoch milliseconds
    response = {
        key: value
        for key, value in doc.items()
        if key != "_id"
    }
    response["created_at"] = int(created_at.timestamp() * 1000)

    return response




@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    records = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = []

    for record in records:
        if record.get("created_at") is not None:
            created_at = record["created_at"]

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            record["created_at"] = int(created_at.timestamp() * 1000)

        items.append(record)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    from pymongo.errors import DuplicateKeyError

    # 1. Check whether the employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).astimezone(IST)
    else:
        ts = datetime.now(IST)

    
    ts = ts.replace(microsecond=0)

    
    attendance_date = ts.date().isoformat()

    
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": ts.astimezone(timezone.utc).replace(tzinfo=None),
        "punch_out": None,
        "work_hours": None,
        "late_minutes":compute_late_minutes(
    ts,
    emp["shift_start"],
    emp["shift_end"]
),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )

    
    return attendance_response(doc)




@app.get("/attendance")
def list_attendance(
    emp_code: str = None,
    date_from: str = None,
    date_to: str = None,
    status: str = None,
    page: int = 1,
    page_size: int = 20,
):
    # Validate pagination
    if page < 1:
        raise HTTPException(422, detail="page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            422,
            detail="page_size must be between 1 and 100",
        )

    # Validate date format
    for field_name, value in (
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if value is not None:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                if parsed.strftime("%Y-%m-%d") != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    422,
                    detail=f"{field_name} must be YYYY-MM-DD",
                )

    # Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422,
            detail="date_from cannot be after date_to",
        )

    # Validate status
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }
    if status is not None and status not in allowed_statuses:
        raise HTTPException(422, detail="Invalid attendance status")

    # Build MongoDB filters
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    # Count matching records before pagination
    total = db.attendance_logs.count_documents(query)

    # Fetch only the requested page
    records = (
        db.attendance_logs
        .find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    # Convert timestamps to epoch milliseconds.
    # Do not return MongoDB's internal ID.
    items = [attendance_response(record) for record in records]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



from decimal import Decimal, ROUND_HALF_UP


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Check whether the employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 2. Get punch-out time in IST and remove fractions of a second
    if body.punched_at is not None:
        ts_ist = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).astimezone(IST)
    else:
        ts_ist = datetime.now(IST)

    ts_ist = ts_ist.replace(microsecond=0)

    # 3. Convert punch-out time to UTC for MongoDB
    ts_utc = ts_ist.astimezone(timezone.utc)
    ts_utc_naive = ts_utc.replace(tzinfo=None)

    # 4. Find the latest open punch-in for this employee
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$lte": ts_utc_naive},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No open punch-in record found",
        )

    # 5. Interpret the stored punch-in timestamp as UTC
    punch_in_raw = record["punch_in"]

    if punch_in_raw.tzinfo is None:
        punch_in_utc = punch_in_raw.replace(tzinfo=timezone.utc)
    else:
        punch_in_utc = punch_in_raw.astimezone(timezone.utc)

    punch_in_ist = punch_in_utc.astimezone(IST)

    # 6. Validate punch-out time
    if ts_utc <= punch_in_utc:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    elapsed_seconds = int(
        (ts_utc - punch_in_utc).total_seconds()
    )

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    # 7. Calculate work hours using HALF-UP rounding
    work_hours = float(
        (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )

    # 8. Calculate the correct shift-end time in IST
    shift_start_h, shift_start_m = map(
        int, emp["shift_start"].split(":")
    )
    shift_end_h, shift_end_m = map(
        int, emp["shift_end"].split(":")
    )

    start_minutes = shift_start_h * 60 + shift_start_m
    end_minutes = shift_end_h * 60 + shift_end_m

    shift_end_ist = punch_in_ist.replace(
        hour=shift_end_h,
        minute=shift_end_m,
        second=0,
        microsecond=0,
    )

    # For an overnight shift, determine which day's shift end applies
    if end_minutes <= start_minutes:
        punch_in_minutes = (
            punch_in_ist.hour * 60 + punch_in_ist.minute
        )

        if punch_in_minutes >= start_minutes:
            shift_end_ist += timedelta(days=1)

    # 9. Overtime counts only when it reaches 30 minutes
    overtime_seconds = max(
        0,
        int((ts_ist - shift_end_ist).total_seconds()),
    )
    overtime_minutes = overtime_seconds // 60

    if overtime_minutes < 30:
        overtime_minutes = 0

    # 10. Save the punch-out timestamp in UTC
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_utc_naive,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
    )

    # 11. Prevent two simultaneous requests from closing the same record
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    # 12. Return the required response format
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return attendance_response(updated)




class AttendanceUpdateIn(BaseModel):
    status: Optional[str] = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str
    regularized_by: str


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: AttendanceUpdateIn,
):
    # 1. Validate date
    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()
        if requested_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be YYYY-MM-DD",
        )

    # 2. Validate correction details
    reason = body.reason.strip()
    by = body.regularized_by.strip()

    if len(reason) < 5 or len(reason) > 200:
        raise HTTPException(
            status_code=422,
            detail="reason must contain 5 to 200 characters",
        )

    if not by or len(by) > 50:
        raise HTTPException(
            status_code=422,
            detail="regularized_by must contain 1 to 50 characters",
        )

    # 3. Find employee and attendance record
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    # 4. Get only fields supplied in the request
    requested = body.model_dump(exclude_unset=True)
    requested.pop("reason", None)
    requested.pop("regularized_by", None)

    if not requested:
        raise HTTPException(422, detail="No changes supplied")

    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    new_status = requested.get("status", record["status"])

    if new_status not in allowed_statuses:
        raise HTTPException(422, detail="Invalid attendance status")

    absent_status = new_status in {"ABSENT", "LEAVE"}

    # ABSENT/LEAVE cannot be submitted together with punch-time fields
    if absent_status and (
        "punch_in" in requested or "punch_out" in requested
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT and LEAVE cannot include punch times",
        )

    # 5. Convert epoch milliseconds to IST
    def epoch_to_ist(value):
        if value is None:
            return None

        return datetime.fromtimestamp(
            value / 1000,
            tz=timezone.utc,
        ).astimezone(IST).replace(microsecond=0)

    # MongoDB stores timestamps as naive UTC datetimes
    def stored_to_ist(value):
        if value is None:
            return None

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)

        return value.astimezone(IST).replace(microsecond=0)

    def to_utc_naive(value):
        if value is None:
            return None

        return value.astimezone(timezone.utc).replace(
            tzinfo=None,
            microsecond=0,
        )

    def epoch_ms(value):
        if value is None:
            return None

        return int(value.timestamp() * 1000)

    old_in = stored_to_ist(record.get("punch_in"))
    old_out = stored_to_ist(record.get("punch_out"))

    new_in = (
        epoch_to_ist(requested["punch_in"])
        if "punch_in" in requested
        else old_in
    )

    new_out = (
        epoch_to_ist(requested["punch_out"])
        if "punch_out" in requested
        else old_out
    )

    # 6. Validate punch times against the attendance status
    if absent_status:
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

        if new_in.date().isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must be on the attendance date",
            )

        if new_out is not None:
            duration_seconds = int(
                (
                    new_out.astimezone(timezone.utc)
                    - new_in.astimezone(timezone.utc)
                ).total_seconds()
            )

            if duration_seconds <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )

            if duration_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # 7. Calculate late minutes using the shift's start date
    late_minutes = 0

    if new_in is not None:
        sh, sm = map(int, emp["shift_start"].split(":"))
        eh, em = map(int, emp["shift_end"].split(":"))

        shift_start_date = new_in.date()

        # If an overnight shift's punch-in is after midnight
        # but before its shift-end time, the shift started yesterday.
        if (eh, em) <= (sh, sm) and (
            new_in.hour * 60 + new_in.minute < eh * 60 + em
        ):
            shift_start_date -= timedelta(days=1)

        shift_start = datetime(
            shift_start_date.year,
            shift_start_date.month,
            shift_start_date.day,
            sh,
            sm,
            tzinfo=IST,
        )

        delay_seconds = int(
            (
                new_in.astimezone(timezone.utc)
                - shift_start.astimezone(timezone.utc)
            ).total_seconds()
        )

        # More than 10 minutes late; count from the shift start
        if delay_seconds > 600:
            late_minutes = delay_seconds // 60

    # 8. Recalculate work hours, overtime and half-day
    work_hours = None
    overtime_minutes = 0
    half_day = False

    if new_in is not None and new_out is not None:
        seconds = int(
            (
                new_out.astimezone(timezone.utc)
                - new_in.astimezone(timezone.utc)
            ).total_seconds()
        )

        work_hours = float(
            (Decimal(seconds) / Decimal(3600)).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

        half_day = work_hours < 4.50

        sh, sm = map(int, emp["shift_start"].split(":"))
        eh, em = map(int, emp["shift_end"].split(":"))

        shift_end_date = new_in.date()
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        punch_in_minutes = new_in.hour * 60 + new_in.minute

        if end_minutes <= start_minutes:
            if punch_in_minutes >= start_minutes:
                shift_end_date += timedelta(days=1)

        shift_end = datetime(
            shift_end_date.year,
            shift_end_date.month,
            shift_end_date.day,
            eh,
            em,
            tzinfo=IST,
        )

        overtime_seconds = int(
            (
                new_out.astimezone(timezone.utc)
                - shift_end.astimezone(timezone.utc)
            ).total_seconds()
        )

        if overtime_seconds >= 1800:
            overtime_minutes = overtime_seconds // 60

    # 9. Compare old and new values for the audit history
    old_values = {
        "status": record["status"],
        "punch_in": epoch_ms(old_in),
        "punch_out": epoch_ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": epoch_ms(new_in),
        "punch_out": epoch_ms(new_out),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    actual_changes = {}

    for field, new_value in new_values.items():
        if old_values[field] != new_value:
            actual_changes[field] = {
                "from": old_values[field],
                "to": new_value,
            }

    if not actual_changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change the record",
        )

    # 10. Add exactly one history entry for this correction
    history_entry = {
        "at": int(datetime.now(IST).timestamp() * 1000),
        "by": by,
        "reason": reason,
        "changes": actual_changes,
    }

    # Compare-and-update prevents concurrent edits from overwriting history
    update_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }

    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        update_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": to_utc_naive(new_in),
                "punch_out": to_utc_naive(new_out),
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Record changed concurrently; fetch it and retry",
        )

    # 11. Return the standard attendance response
    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    return attendance_response(updated)


from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month format: YYYY-MM
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    # Calculate the first day of the next month
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1,
        )

    start_str = month_start.strftime("%Y-%m-%d")
    end_str = next_month.strftime("%Y-%m-%d")

    # Check whether the employee exists
    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Aggregate attendance records for the requested month
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lt": end_str},
            }
        },
        {
            "$addFields": {
                "_day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,

                # Weekdays only; a half day counts as 0.5
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$_day_of_week",
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                # Count every LEAVE record in the month
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                # Count records with late minutes greater than zero
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    stats = results[0] if results else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    # Count Monday-Friday working days from the joining date
    joined_on = datetime.strptime(emp["joined_on"], "%Y-%m-%d")
    effective_start = max(month_start, joined_on)

    working_days = 0

    if effective_start < next_month:
        for offset in range((next_month - effective_start).days):
            current_date = effective_start + timedelta(days=offset)

            if current_date.weekday() < 5:
                working_days += 1

    present_days = stats["present_days"]

    # Monthly attendance percentage: round HALF-UP to 2 decimals
    if working_days > 0:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                * Decimal("100")
                / Decimal(working_days)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }



@app.get("/analytics/departments/summary")
def department_summary(month: str):
    # Validate month format: YYYY-MM
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    # Calculate the first day of the next month
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1,
        )

    start_date = month_start.strftime("%Y-%m-%d")
    end_date = next_month.strftime("%Y-%m-%d")

    # Aggregate employees and their attendance in MongoDB.
    # Employees who joined before the next month are included,
    # even if they have no attendance records.
    pipeline = [
        {
            "$match": {
                "joined_on": {"$lt": end_date},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {"$gte": ["$date", start_date]},
                                    {"$lt": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,

                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$_day_of_week",
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        "$half_day",
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },

                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },

                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes",
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": [
                                        "$late_minutes",
                                        0,
                                    ]
                                }
                            },

                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "LEAVE",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "ON_DUTY",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_stats",
            }
        },
        {
            "$unwind": {
                "path": "$attendance_stats",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},

                "present_days": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.present_days",
                            0,
                        ]
                    }
                },

                "work_hours_sum": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.work_hours_sum",
                            0,
                        ]
                    }
                },

                "work_hours_count": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.work_hours_count",
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.late_count",
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.total_late_minutes",
                            0,
                        ]
                    }
                },

                "leave_count": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.leave_count",
                            0,
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$ifNull": [
                            "$attendance_stats.on_duty_count",
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))

    departments = []

    for item in results:
        # Average across attendance records with work_hours,
        # not across employee averages.
        count = item["work_hours_count"]

        if count > 0:
            avg_work_hours = float(
                (
                    Decimal(str(item["work_hours_sum"]))
                    / Decimal(count)
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )
        else:
            avg_work_hours = None

        departments.append({
            "department": item["_id"],
            "headcount": item["headcount"],
            "present_days": item["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": item["late_count"],
            "total_late_minutes": item["total_late_minutes"],
            "leave_count": item["leave_count"],
            "on_duty_count": item["on_duty_count"],
        })

    return {
        "month": month,
        "departments": departments,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    # Validate month
    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 100",
        )

    # Calculate month boundaries
    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year,
            month_start.month + 1,
            1,
        )

    start_date = month_start.strftime("%Y-%m-%d")
    end_date = next_month.strftime("%Y-%m-%d")

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lt": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "late_count": {"$sum": 1},
                "total_late_minutes": {"$sum": "$late_minutes"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        # Ignore attendance logs for employees who do not exist
        {"$unwind": "$employee"},
    ]

    # Apply department filter before ranking
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department,
            }
        })

    # Competition ranking: 1, 2, 2, 4
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        # Keep all employees whose rank is within the limit.
        # Ties may therefore produce more than 'limit' rows.
        {
            "$match": {
                "rank": {"$lte": limit},
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "late_count": 1,
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ])

    leaderboard = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "leaderboard": leaderboard,
    }



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate date formats
    try:
        start_dt = datetime.strptime(from_date, "%Y-%m-%d")
        end_dt = datetime.strptime(to_date, "%Y-%m-%d")

        if start_dt.strftime("%Y-%m-%d") != from_date:
            raise ValueError

        if end_dt.strftime("%Y-%m-%d") != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if end_dt < start_dt:
        raise HTTPException(
            status_code=422,
            detail="to must not be before from",
        )

    day_count = (end_dt - start_dt).days + 1

    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )



    start_bson = start_dt
    end_bson = end_dt

    pipeline = [
        {"$match": {"department": department}},
        {
            "$addFields": {
                "_joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "format": "%Y-%m-%d",
                    }
                },
                "_start_date": {"$literal": start_bson},
                "_end_date": {"$literal": end_bson},
            }
        },
        {
            "$addFields": {
                "_days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_start_date",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_days"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "code": "$emp_code",
                    "day": "$_days",
                    "joined": "$_joined_date",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {
                                        "$eq": [
                                            "$date",
                                            {
                                                "$dateToString": {
                                                    "format": "%Y-%m-%d",
                                                    "date": "$$day",
                                                }
                                            },
                                        ]
                                    },
                                    {"$lte": ["$$joined", "$$day"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_attendance",
            }
        },
        {
            "$unwind": {
                "path": "$_attendance",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$_days",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$_joined_date", "$_days"]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$_attendance.status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            "$_attendance.half_day",
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$_attendance.late_minutes",
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                    }
                },
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$_id"},
                        5,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        
{
    "$divide": [
        {
            "$floor": [
                {
                    "$add": [
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                10000,
                            ]
                        },
                        0.5,
                    ]
                }
            ]
        },
        10000,
    ]
},

                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": 1,
                "attendance_rate": 1,
                
"moving_avg_7d": {
    "$divide": [
        {
            "$floor": [
                {
                    "$add": [
                        {
                            "$multiply": [
                                "$_moving_avg",
                                10000,
                            ]
                        },
                        0.5,
                    ]
                }
            ]
        },
        10000,
    ]
},

            }
        },
        {"$sort": {"date": 1}},
    ]

    trend = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "trend": trend,
    }




@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,    
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 10,
    page: int = 1,
    page_size: int = 20,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
):
    endpoint = endpoint.lower().strip()

    # Validate pagination
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "Invalid page or page_size")

    # Use current month when month is not provided
    if month is None:
        month = datetime.now().strftime("%Y-%m")

    try:
        month_start = datetime.strptime(month, "%Y-%m")
        if month_start.strftime("%Y-%m") != month:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")

    if month_start.month == 12:
        next_month = datetime(month_start.year + 1, 1, 1)
    else:
        next_month = datetime(
            month_start.year, month_start.month + 1, 1
        )

    start_date = month_start.strftime("%Y-%m-%d")
    end_date = next_month.strftime("%Y-%m-%d")

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        
        result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return result

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                422, "Pass emp_code as a query parameter"
            )

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lt": end_date},
                }
            },
            {
                "$addFields": {
                    "_day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$in": [
                                                "$_day_of_week",
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "department_summary":
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lt": end_date}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date",
                                                            "format": "%Y-%m-%d",
                                                        }
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    if endpoint == "late_leaderboard":
        if limit < 1 or limit > 100:
            raise HTTPException(422, "limit must be between 1 and 100")

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lt": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "late_count": {"$sum": 1},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {"$limit": limit},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
        ]

        return db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    
    if endpoint == "department_trend":
        if not department:
            raise HTTPException(
                status_code=422,
                detail="Pass department as a query parameter",
            )

        if not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="Pass from and to as YYYY-MM-DD query parameters",
            )

        try:
            start_dt = datetime.strptime(from_date, "%Y-%m-%d")
            end_dt = datetime.strptime(to_date, "%Y-%m-%d")

            if (
                start_dt.strftime("%Y-%m-%d") != from_date
                or end_dt.strftime("%Y-%m-%d") != to_date
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format",
            )

        if end_dt < start_dt:
            raise HTTPException(
                status_code=422,
                detail="to must not be before from",
            )

        day_count = (end_dt - start_dt).days + 1
        if day_count > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        trend_start = from_date
        trend_end = (
            end_dt + timedelta(days=1)
        ).strftime("%Y-%m-%d")


        start_bson = datetime.strptime(trend_start, "%Y-%m-%d")
        end_bson = datetime.strptime(trend_end, "%Y-%m-%d")
        day_count = (end_bson - start_bson).days

        pipeline = [
            {"$match": {"department": department}},
            {
                "$addFields": {
                    "_joined_date": {
                        "$dateFromString": {
                            "dateString": "$joined_on",
                            "format": "%Y-%m-%d",
                        }
                    },
                    "_start_date": {"$literal": start_bson},
                    "_end_date": {
                        "$literal": end_bson - timedelta(days=1)
                    },
                }
            },
            {
                "$addFields": {
                    "_days": {
                        "$map": {
                            "input": {"$range": [0, day_count]},
                            "as": "offset",
                            "in": {
                                "$dateAdd": {
                                    "startDate": "$_start_date",
                                    "unit": "day",
                                    "amount": "$$offset",
                                }
                            },
                        }
                    }
                }
            },
            {"$unwind": "$_days"},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "code": "$emp_code",
                        "day": "$_days",
                        "joined": "$_joined_date",
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {
                                            "$eq": [
                                                "$date",
                                                {
                                                    "$dateToString": {
                                                        "format": "%Y-%m-%d",
                                                        "date": "$$day",
                                                    }
                                                },
                                            ]
                                        },
                                        {"$lte": ["$$joined", "$$day"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_attendance",
                }
            },
            {
                "$unwind": {
                    "path": "$_attendance",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$_days",
                    "headcount": {
                        "$sum": {
                            "$cond": [
                                {"$lte": ["$_joined_date", "$_days"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$_attendance.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$_attendance.half_day",
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$_attendance.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$addFields": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_id",
                        }
                    },
                    "is_working_day": {
                        "$lte": [{"$isoDayOfWeek": "$_id"}, 5]
                    },
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": [
                                            {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            {
                                                                "$divide": [
                                                                    "$present_count",
                                                                    "$headcount",
                                                                ]
                                                            },
                                                            10000,
                                                        ]
                                                    },
                                                    0.5,
                                                ]
                                            }
                                        ]
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"_id": 1},
                    "output": {
                        "_moving_avg": {
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
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "is_working_day": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$divide": [
                            {
                                "$floor": [
                                    {
                                        "$add": [
                                            {"$multiply": ["$_moving_avg", 10000]},
                                            0.5,
                                        ]
                                    }
                                ]
                            },
                            10000,
                        ]
                    },
                }
            },
            {"$sort": {"date": 1}},
        ]

        return db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
    raise HTTPException(
        status_code=404,
        detail={
            "message": "Unknown endpoint name",
            "available_endpoints": [
                "attendance_list",
                "employee_monthly",
                "department_summary",
                "late_leaderboard",
                "department_trend",
            ],
        },
    )




# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
