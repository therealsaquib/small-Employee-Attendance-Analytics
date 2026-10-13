"""Employee Attendance & Analytics API."""

import logging
import json
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
client = (
    MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
    if MONGO_URI
    else None
)
db = client[MONGO_DB] if client is not None and MONGO_DB else None

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMP_CODE_PATTERN = re.compile(r"^EMP\d{4,6}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.exception_handler(PyMongoError)
def mongo_error_handler(_, exc: PyMongoError):
    logger.exception("MongoDB operation failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def get_db():
    if db is None:
        raise HTTPException(503, "Database configuration is missing")
    return db


def create_indexes():
    database = get_db()
    database.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_code"
    )
    database.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    database.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    database.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_employee",
    )


@app.on_event("startup")
def startup():
    if db is None:
        logger.error("MONGO_URI and MONGO_DB must both be configured")
        return
    try:
        create_indexes()
    except PyMongoError:
        logger.exception("Could not create MongoDB indexes during startup")


def parse_date(value: str, field_name: str = "date") -> date:
    if not DATE_PATTERN.fullmatch(value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a real calendar date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
    except ValueError as exc:
        raise HTTPException(422, "month must be a real calendar month") from exc
    return start, end


def epoch_to_datetime(value: int) -> datetime:
    seconds = value // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


def datetime_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def server_now() -> datetime:
    return datetime.now(UTC)


def shift_clock(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute, tzinfo=IST)


def attendance_date(punch_in: datetime, employee: dict) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    shift_start = shift_clock(employee["shift_start"])
    shift_end = shift_clock(employee["shift_end"])
    if shift_end <= shift_start and local.timetz().replace(tzinfo=None) < shift_end.replace(tzinfo=None):
        day -= timedelta(days=1)
    return day


def shift_start_datetime(day: date, employee: dict) -> datetime:
    h, m = map(int, employee["shift_start"].split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def shift_end_datetime(day: date, employee: dict) -> datetime:
    h, m = map(int, employee["shift_end"].split(":"))
    end_day = day
    if shift_clock(employee["shift_end"]) <= shift_clock(employee["shift_start"]):
        end_day += timedelta(days=1)
    return datetime.combine(end_day, time(h, m), tzinfo=IST)


def compute_late_minutes(punch_in: Optional[datetime], day: date, employee: dict) -> int:
    if punch_in is None:
        return 0
    elapsed = int((punch_in - shift_start_datetime(day, employee)).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]):
    if punch_in is None or punch_out is None:
        return None
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: Optional[datetime], day: date, employee: dict) -> int:
    if punch_out is None:
        return 0
    elapsed = int((punch_out - shift_end_datetime(day, employee)).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def mongo_round_half_up(expression, places: int):
    scale = Decimal128(str(10**places))
    return {
        "$toDouble": {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": [{"$toDecimal": expression}, scale]},
                            Decimal128("0.5"),
                        ]
                    }
                },
                scale,
            ]
        }
    }


def api_time(value):
    if isinstance(value, datetime):
        return datetime_to_epoch(value)
    return value


def api_changes(changes: dict) -> dict:
    converted = {}
    for field, pair in changes.items():
        converted[field] = {
            "from": api_time(pair["from"]),
            "to": api_time(pair["to"]),
        }
    return converted


def serialize_employee(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch(document["created_at"]),
    }


def serialize_attendance(document: dict) -> dict:
    history = document.get("history") or []
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch(document.get("punch_in")),
        "punch_out": datetime_to_epoch(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": [
            {
                "at": datetime_to_epoch(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": api_changes(entry.get("changes", {})),
            }
            for entry in history
        ],
    }


def decimal_days(value):
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def date_is_weekday_expression(field="$date"):
    return {"$in": [{"$dayOfWeek": field}, [2, 3, 4, 5, 6]]}


def month_employee_pipeline(emp_code: str, month_start: date, month_end: date):
    start_text = month_start.isoformat()
    end_text = month_end.isoformat()
    start_date = datetime.combine(month_start, time.min, tzinfo=UTC)
    end_date = datetime.combine(month_end, time.min, tzinfo=UTC)
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "pipeline": [
                    {"$match": {"emp_code": emp_code, "date": {"$gte": start_text, "$lte": end_text}}},
                    {
                        "$addFields": {
                            "_calendar_date": {
                                "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
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
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                date_is_weekday_expression("$_calendar_date"),
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                        }
                    },
                ],
                "as": "_stats",
            }
        },
        {
            "$addFields": {
                "_stats": {"$ifNull": [{"$arrayElemAt": ["$_stats", 0]}, {}]},
                "_joined_date": {"$dateFromString": {"dateString": "$joined_on", "format": "%Y-%m-%d"}},
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$cond": [
                        {"$gt": ["$_joined_date", end_date]},
                        0,
                        {
                            "$let": {
                                "vars": {
                                    "period_start": {
                                        "$cond": [
                                            {"$gt": ["$_joined_date", start_date]},
                                            "$_joined_date",
                                            start_date,
                                        ]
                                    }
                                },
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "span": {
                                                "$dateDiff": {
                                                    "startDate": "$$period_start",
                                                    "endDate": end_date,
                                                    "unit": "day",
                                                }
                                            }
                                        },
                                        "in": {
                                            "$size": {
                                                "$filter": {
                                                    "input": {"$range": [0, {"$add": ["$$span", 1]}]},
                                                    "as": "offset",
                                                    "cond": {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateAdd": {
                                                                        "startDate": "$$period_start",
                                                                        "unit": "day",
                                                                        "amount": "$$offset",
                                                                    }
                                                                }
                                                            },
                                                            [2, 3, 4, 5, 6],
                                                        ]
                                                    },
                                                }
                                            }
                                        },
                                    }
                                },
                            }
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "working_days": 1,
                "present_days": {"$ifNull": ["$_stats.present_days", 0]},
                "leave_days": {"$ifNull": ["$_stats.leave_days", 0]},
                "late_count": {"$ifNull": ["$_stats.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$_stats.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$_stats.total_overtime_minutes", 0]},
            }
        },
    ]


def department_summary_pipeline(month_start: date, month_end: date, department: Optional[str]):
    start_text, end_text = month_start.isoformat(), month_end.isoformat()
    match = {"joined_on": {"$lte": end_text}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_text]},
                                    {"$lte": ["$date", end_text]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_calendar_date": {
                                "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
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
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                date_is_weekday_expression("$_calendar_date"),
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        {"$toDecimal": "$work_hours"},
                                        Decimal128("0"),
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "_stats",
            }
        },
        {"$addFields": {"_stats": {"$ifNull": [{"$arrayElemAt": ["$_stats", 0]}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$_stats.present_days", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$_stats.work_hours_sum", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$_stats.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$_stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$_stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$_stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$_stats.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        mongo_round_half_up(
                            {"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2
                        ),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month_start: date, month_end: date, department: Optional[str], limit: int):
    lookup_pipeline = [{"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}}]
    if department is not None:
        lookup_pipeline.append({"$match": {"department": department}})
    return [
        {
            "$match": {
                "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"employee_code": "$_id"},
                "pipeline": lookup_pipeline,
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def department_trend_pipeline(department: str, date_from: date, date_to: date):
    day_count = (date_to - date_from).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "day_index": {"$range": [0, day_count]},
                "range_start": {
                    "$dateFromString": {
                        "dateString": date_from.isoformat(),
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {"$unwind": "$day_index"},
        {
            "$densify": {
                "field": "day_index",
                "range": {"step": 1, "bounds": [0, day_count]},
            }
        },
        {
            "$set": {
                "_date": {
                    "$dateAdd": {
                        "startDate": "$range_start",
                        "unit": "day",
                        "amount": "$day_index",
                    }
                },
                "date": {
                    "$dateToString": {
                        "date": {
                            "$dateAdd": {
                                "startDate": "$range_start",
                                "unit": "day",
                                "amount": "$day_index",
                            }
                        },
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$set": {
                "_eligible": {
                    "$filter": {
                        "input": "$employees",
                        "as": "employee",
                        "cond": {"$lte": ["$$employee.joined_on", "$date"]},
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"attendance_day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$attendance_day"]}}},
                ],
                "as": "_day_logs",
            }
        },
        {
            "$set": {
                "_logs": {
                    "$filter": {
                        "input": "$_day_logs",
                        "as": "log",
                        "cond": {
                            "$in": [
                                "$$log.emp_code",
                                {
                                    "$map": {
                                        "input": "$employees",
                                        "as": "employee",
                                        "in": "$$employee.emp_code",
                                    }
                                },
                            ]
                        },
                    }
                },
                "is_working_day": date_is_weekday_expression("$_date"),
                "headcount": {"$size": "$_eligible"},
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_round_half_up(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day_index": 1},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": {"$toDecimal": "$attendance_rate"},
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
                "moving_avg_7d": mongo_round_half_up("$_moving_avg_raw", 4),
            }
        },
        {"$sort": {"date": 1}},
    ]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("invalid email")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value, "joined_on")
        return value

    @field_validator("shift_end")
    @classmethod
    def shifts_differ(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def time_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("punch times cannot be null")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def status_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("status cannot be null")
        return value


@app.get("/health")
def health():
    if db is None:
        raise HTTPException(503, "Database configuration is missing")
    try:
        db.command("ping")
    except PyMongoError as exc:
        logger.warning("MongoDB readiness check failed: %s", exc)
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    database = get_db()
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    document = body.model_dump()
    document["created_at"] = server_now()
    try:
        database.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    query = {"department": department} if department is not None else {}
    total = database.employees.count_documents(query)
    cursor = (
        database.employees.find(query)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    database = get_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = body.punched_at if body.punched_at is not None else int(server_now().timestamp() * 1000)
    punched_at = punched_at // 1000 * 1000
    punch_in_time = epoch_to_datetime(punched_at)
    attendance_day = attendance_date(punch_in_time, employee)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_time, attendance_day, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    database = get_db()
    employee = database.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = body.punched_at if body.punched_at is not None else int(server_now().timestamp() * 1000)
    punched_at = punched_at // 1000 * 1000
    punch_out_time = epoch_to_datetime(punched_at)
    record = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_time}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        future_record = database.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": punch_out_time}},
            sort=[("punch_in", ASCENDING)],
        )
        if future_record is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_time = record.get("punch_in")
    if punch_in_time is None:
        raise HTTPException(404, "no punch-in record found")
    elapsed = (punch_out_time - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    record_day = parse_date(record["date"])
    update_filter = {
        "emp_code": body.emp_code,
        "date": record["date"],
        "punch_in": punch_in_time,
        "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
    }
    update = {
        "punch_out": punch_out_time,
        "work_hours": compute_work_hours(punch_in_time, punch_out_time),
        "overtime_minutes": compute_overtime(punch_out_time, record_day, employee),
    }
    update["half_day"] = update["work_hours"] < 4.50
    result = database.attendance_logs.update_one(update_filter, {"$set": update})
    if result.modified_count != 1:
        raise HTTPException(409, "record was punched out concurrently")
    updated = database.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": record["date"]}
    )
    return serialize_attendance(updated)


def attendance_query(emp_code, date_from, date_to, status):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            bounds["$gte"] = date_from
        if date_to is not None:
            bounds["$lte"] = date_to
        query["date"] = bounds
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


def attendance_list_cursor(database, query, page, page_size):
    options = attendance_list_options(query, page, page_size)
    return (
        database.attendance_logs.find(options["filter"], options["projection"])
        .hint(options["hint"])
        .sort(options["sort"])
        .skip(options["skip"])
        .limit(options["limit"])
    )


def attendance_list_options(query, page, page_size):
    return {
        "filter": query,
        "projection": {"_id": 0},
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": (
            "ix_attendance_status_date_employee"
            if "status" in query
            else "ix_attendance_date_employee"
        ),
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    query = attendance_query(emp_code, date_from, date_to, status)
    total = database.attendance_logs.count_documents(query)
    records = attendance_list_cursor(database, query, page, page_size)
    return {
        "items": [serialize_attendance(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    database = get_db()
    record_day = parse_date(date)
    employee = database.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    existing = database.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if existing is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    old_status = existing["status"]
    status = body.status if "status" in supplied else old_status
    punch_in_time = (
        epoch_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else existing.get("punch_in")
    )
    punch_out_time = (
        epoch_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else existing.get("punch_out")
    )

    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        punch_in_time = None
        punch_out_time = None
    elif punch_in_time is None:
        raise HTTPException(422, "presence status requires punch_in")

    if punch_in_time is not None and attendance_date(punch_in_time, employee) != record_day:
        raise HTTPException(422, "punch_in must belong to the attendance date")
    if punch_out_time is not None:
        if punch_in_time is None:
            raise HTTPException(422, "punch_out requires punch_in")
        elapsed = (punch_out_time - punch_in_time).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if status in ("ABSENT", "LEAVE"):
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        work_hours = compute_work_hours(punch_in_time, punch_out_time)
        derived = {
            "work_hours": work_hours,
            "late_minutes": compute_late_minutes(punch_in_time, record_day, employee),
            "overtime_minutes": compute_overtime(punch_out_time, record_day, employee),
            "half_day": work_hours < 4.50 if work_hours is not None else False,
        }

    before = {
        "status": old_status,
        "punch_in": existing.get("punch_in"),
        "punch_out": existing.get("punch_out"),
        "work_hours": existing.get("work_hours"),
        "late_minutes": existing.get("late_minutes", 0),
        "overtime_minutes": existing.get("overtime_minutes", 0),
        "half_day": existing.get("half_day", False),
    }
    after = {
        "status": status,
        "punch_in": punch_in_time,
        "punch_out": punch_out_time,
        **derived,
    }

    def equal_value(field, left, right):
        if field in ("punch_in", "punch_out"):
            return datetime_to_epoch(left) == datetime_to_epoch(right)
        if field == "work_hours":
            return left is None and right is None or (
                left is not None and right is not None and Decimal(str(left)) == Decimal(str(right))
            )
        return left == right

    changes = {
        field: {"from": before[field], "to": after[field]}
        for field in before
        if not equal_value(field, before[field], after[field])
    }
    if not changes:
        raise HTTPException(422, "request does not change the record")

    history = existing.get("history", [])
    history_entry = {
        "at": server_now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    compare = {"emp_code": emp_code, "date": date, "status": old_status}
    for field in ("punch_in", "punch_out"):
        if field in existing:
            compare[field] = existing[field]
        else:
            compare[field] = {"$exists": False}
    if "history" in existing:
        compare["history"] = {"$size": len(history)}
    else:
        compare["history"] = {"$exists": False}
    update = {"$set": after, "$push": {"history": history_entry}}
    result = database.attendance_logs.update_one(compare, update)
    if result.modified_count != 1:
        raise HTTPException(409, "record was corrected concurrently")
    return serialize_attendance(
        database.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    )


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    database = get_db()
    month_start, month_end = parse_month(month)
    employee = database.employees.find_one({"emp_code": emp_code}, {"_id": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    result = next(
        iter(
            database.employees.aggregate(
                month_employee_pipeline(emp_code, month_start, month_end)
            )
        ),
        None,
    )
    if result is None:
        raise HTTPException(404, "employee not found")
    working_days = result["working_days"]
    present_days = decimal_days(result["present_days"])
    attendance_pct = (
        round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 4)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result["leave_days"],
        "late_count": result["late_count"],
        "total_late_minutes": result["total_late_minutes"],
        "total_overtime_minutes": result["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    database = get_db()
    month_start, month_end = parse_month(month)
    rows = list(
        database.employees.aggregate(
            department_summary_pipeline(month_start, month_end, department)
        )
    )
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    database = get_db()
    month_start, month_end = parse_month(month)
    rows = list(
        database.attendance_logs.aggregate(
            late_leaderboard_pipeline(month_start, month_end, department, limit)
        )
    )
    for row in rows:
        row["total_late_minutes"] = int(row["total_late_minutes"])
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    database = get_db()
    date_from = parse_date(from_, "from")
    date_to = parse_date(to, "to")
    if date_to < date_from or (date_to - date_from).days + 1 > 92:
        raise HTTPException(422, "trend range must be ordered and at most 92 days")
    if database.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    rows = list(
        database.employees.aggregate(
            department_trend_pipeline(department, date_from, date_to)
        )
    )
    return {"department": department, "items": rows}


def aggregate_explain(collection, pipeline):
    database = get_db()
    return database.command(
        "explain",
        {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    database = get_db()
    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status)
        options = attendance_list_options(query, page, page_size)
        explain = database.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": options["filter"],
                "projection": options["projection"],
                "sort": dict(options["sort"]),
                "skip": options["skip"],
                "limit": options["limit"],
                "hint": options["hint"],
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend"):
        if endpoint != "department_trend" and month is None:
            raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            month_start, month_end = parse_month(month)
            pipeline = month_employee_pipeline(emp_code, month_start, month_end)
            collection = "employees"
        elif endpoint == "department_summary":
            month_start, month_end = parse_month(month)
            pipeline = department_summary_pipeline(month_start, month_end, department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            month_start, month_end = parse_month(month)
            pipeline = late_leaderboard_pipeline(month_start, month_end, department, limit)
            collection = "attendance_logs"
        else:
            if department is None or from_ is None or to is None:
                raise HTTPException(422, "department, from, and to are required")
            date_from_value = parse_date(from_, "from")
            date_to_value = parse_date(to, "to")
            if date_to_value < date_from_value or (date_to_value - date_from_value).days + 1 > 92:
                raise HTTPException(422, "trend range must be ordered and at most 92 days")
            pipeline = department_trend_pipeline(department, date_from_value, date_to_value)
            collection = "employees"
        explain = aggregate_explain(collection, pipeline)
    else:
        raise HTTPException(422, "unsupported endpoint")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explain)),
    }
