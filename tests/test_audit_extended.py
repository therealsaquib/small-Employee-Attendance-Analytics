"""Focused black-box checks for the attendance API; requires a live throwaway test DB."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re

import httpx
import pytest
from pymongo import MongoClient


BASE_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.environ["MONGO_DB"]
mongo = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = mongo[MONGO_DB]
employees = db.employees
attendance = db.attendance_logs
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="session", autouse=True)
def clear_audit_documents():
    attendance.delete_many({"emp_code": re.compile(r"^EMP81")})
    employees.delete_many({"emp_code": re.compile(r"^EMP81")})
    yield
    attendance.delete_many({"emp_code": re.compile(r"^EMP81")})
    employees.delete_many({"emp_code": re.compile(r"^EMP81")})
    mongo.close()


def api(method, path, payload=None):
    response = httpx.request(
        method,
        BASE_URL + path,
        json=payload,
        timeout=30,
    )
    assert response.status_code < 500, f"{method} {path} unexpectedly returned {response.status_code}: {response.text}"
    return response.status_code, response.json()


def epoch_ist(value):
    local = datetime.fromisoformat(value).replace(tzinfo=IST)
    return int(local.timestamp() * 1000)


def employee_doc(code, department="Audit", joined="2026-07-01", start="09:30", end="18:30"):
    return {
        "emp_code": code,
        "name": f"Person {code}",
        "email": f"{code.lower()}@example.com",
        "department": department,
        "shift_start": start,
        "shift_end": end,
        "joined_on": joined,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def create_employee(code, **overrides):
    body = {
        "emp_code": code,
        "name": f"Person {code}",
        "email": f"{code.lower()}@example.com",
        "department": "Audit",
        "joined_on": "2026-01-01",
        **overrides,
    }
    return api("POST", "/employees", body)


def log_doc(code, day, status="PRESENT", *, late=0, overtime=0, half=False, hours=None, punch_in=None, punch_out=None, history=True):
    document = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
    }
    if half is not None:
        document["half_day"] = half
    if history:
        document["history"] = []
    return document


def assert_no_ids(value):
    if isinstance(value, dict):
        assert "_id" not in value and "id" not in value
        for nested in value.values():
            assert_no_ids(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_ids(nested)


def test_employee_validation_and_pagination_contract():
    assert api("GET", "/health")[0] == 200

    valid = {
        "emp_code": "EMP8101",
        "name": "Audit Employee",
        "email": "audit@example.com",
        "department": "Audit",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    for change in (
        {"emp_code": "EMP81"},
        {"email": "not-an-email"},
        {"shift_start": "24:00"},
        {"shift_start": "09:30", "shift_end": "09:30"},
        {"joined_on": "2026-02-30"},
    ):
        assert api("POST", "/employees", {**valid, **change})[0] == 422

    status, created = api("POST", "/employees", valid)
    assert status == 201
    assert isinstance(created["created_at"], int)
    stored_employee = employees.find_one({"emp_code": "EMP8101"})
    assert isinstance(stored_employee["created_at"], datetime)
    assert set(created) == {
        "emp_code", "name", "email", "department", "shift_start",
        "shift_end", "joined_on", "created_at",
    }
    assert_no_ids(created)
    assert api("POST", "/employees", valid)[0] == 409

    for params in ("?page=0", "?page_size=101"):
        assert api("GET", "/employees" + params)[0] == 422
    page = api("GET", "/employees?department=Audit&page=1&page_size=1")[1]
    assert page["total"] >= 1
    assert page["items"][0]["emp_code"] == "EMP8101"
    assert_no_ids(page)


def test_epoch_validation_default_status_and_punchout_errors():
    assert create_employee("EMP8102")[0] == 201
    bad_epochs = [1783312500, 1783312500000.0, "1783312500000", True, -1, 4_102_444_800_001, 99_999_999_999]
    for value in bad_epochs:
        assert api("POST", "/attendance/punch-in", {"emp_code": "EMP8102", "punched_at": value})[0] == 422

    status, punched = api("POST", "/attendance/punch-in", {"emp_code": "EMP8102"})
    assert status == 201
    assert punched["status"] == "PRESENT"
    assert abs(punched["punch_in"] - int(datetime.now(UTC).timestamp() * 1000)) < 120_000
    assert api("POST", "/attendance/punch-in", {"emp_code": "EMP9998"})[0] == 404
    assert api("POST", "/attendance/punch-in", {"emp_code": "EMP8102", "status": "ABSENT"})[0] == 422
    assert api("POST", "/attendance/punch-in", {"emp_code": "EMP8102", "status": "LEAVE"})[0] == 422

    assert api("POST", "/attendance/punch-out", {"emp_code": "EMP9998"})[0] == 404
    assert api("POST", "/attendance/punch-out", {"emp_code": "EMP8102", "punched_at": punched["punch_in"]})[0] == 422
    assert api(
        "POST",
        "/attendance/punch-out",
        {"emp_code": "EMP8102", "punched_at": punched["punch_in"] + 86_400_001},
    )[0] == 200
    assert create_employee("EMP8112")[0] == 201
    assert api("POST", "/attendance/punch-in", {
        "emp_code": "EMP8112", "punched_at": punched["punch_in"],
    })[0] == 201
    assert api(
        "POST",
        "/attendance/punch-out",
        {"emp_code": "EMP8112", "punched_at": punched["punch_in"] + 86_401_000},
    )[0] == 422


def test_r1_r2_r3_r4_r5_boundaries_and_bson_shape():
    assert create_employee("EMP8103")[0] == 201
    cases = [
        ("2026-07-06T09:29:00", 0, 0),
        ("2026-07-07T09:40:00", 0, 0),
        ("2026-07-08T09:40:00", 0, 900),
        ("2026-07-09T09:40:01", 10, 900),
        ("2026-07-10T10:15:59", 45, 0),
    ]
    for clock, expected, fractional_ms in cases:
        millis = epoch_ist(clock) + fractional_ms
        status, row = api("POST", "/attendance/punch-in", {
            "emp_code": "EMP8103",
            "punched_at": millis,
        })
        assert status == 201 and row["late_minutes"] == expected
        if fractional_ms:
            assert row["punch_in"] == millis // 1000 * 1000
            stored = attendance.find_one({"emp_code": "EMP8103", "date": row["date"]})
            assert stored["punch_in"].microsecond == 0
            assert isinstance(stored["date"], str)

    assert create_employee("EMP8104", shift_start="22:00", shift_end="06:00")[0] == 201
    evening = api("POST", "/attendance/punch-in", {
        "emp_code": "EMP8104", "punched_at": epoch_ist("2026-07-10T21:55:00"),
    })[1]
    assert create_employee("EMP8109", shift_start="22:00", shift_end="06:00")[0] == 201
    morning = api("POST", "/attendance/punch-in", {
        "emp_code": "EMP8109", "punched_at": epoch_ist("2026-07-11T02:00:00"),
    })[1]
    assert evening["date"] == "2026-07-10"
    assert morning["date"] == "2026-07-10"
    overnight_out = api("POST", "/attendance/punch-out", {
        "emp_code": "EMP8109", "punched_at": epoch_ist("2026-07-11T06:40:00"),
    })[1]
    assert overnight_out["overtime_minutes"] == 40

    assert create_employee("EMP8105")[0] == 201
    overtime_cases = [("2026-07-12T18:59:00", 0), ("2026-07-13T19:00:00", 30), ("2026-07-14T19:10:00", 40)]
    for index, (clock, expected) in enumerate(overtime_cases):
        day = (date(2026, 7, 12) + timedelta(days=index)).isoformat()
        started = api("POST", "/attendance/punch-in", {
            "emp_code": "EMP8105", "punched_at": epoch_ist(f"{day}T09:30:00"),
        })[1]
        ended = api("POST", "/attendance/punch-out", {
            "emp_code": "EMP8105", "punched_at": epoch_ist(clock),
        })[1]
        assert ended["overtime_minutes"] == expected
        assert ended["punch_in"] == started["punch_in"]

    assert create_employee("EMP8106")[0] == 201
    durations = [
        4 * 3600 + 18,
        4 * 3600 + 54,
        4 * 3600 + 29 * 60 + 24,
        4 * 3600 + 30 * 60,
        5 * 3600 + 18,
        5 * 3600 + 54,
        6 * 3600 + 90,
    ]
    for index, seconds in enumerate(durations):
        day = (date(2026, 8, 1) + timedelta(days=index)).isoformat()
        started = api("POST", "/attendance/punch-in", {
            "emp_code": "EMP8106", "punched_at": epoch_ist(f"{day}T09:30:00"),
        })[1]
        ended = api("POST", "/attendance/punch-out", {
            "emp_code": "EMP8106",
            "punched_at": started["punch_in"] + seconds * 1000,
        })[1]
        expected = float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        assert ended["work_hours"] == expected

    assert create_employee("EMP8107")[0] == 201
    for index, seconds in enumerate((4 * 3600 + 29 * 60 + 24, 4 * 3600 + 30 * 60, 4 * 3600 + 29 * 60 + 46)):
        day = (date(2026, 8, 10) + timedelta(days=index)).isoformat()
        started = api("POST", "/attendance/punch-in", {
            "emp_code": "EMP8107", "punched_at": epoch_ist(f"{day}T09:30:00"),
        })[1]
        ended = api("POST", "/attendance/punch-out", {
            "emp_code": "EMP8107", "punched_at": started["punch_in"] + seconds * 1000,
        })[1]
        assert ended["half_day"] is (ended["work_hours"] < 4.50)
    assert attendance.find_one({"emp_code": "EMP8106"})["punch_out"].tzinfo is not None

    assert create_employee("EMP8108")[0] == 201
    utc_before = int(datetime(2026, 7, 10, 18, 29, tzinfo=UTC).timestamp() * 1000)
    utc_after = int(datetime(2026, 7, 10, 18, 30, tzinfo=UTC).timestamp() * 1000)
    before_date = api("POST", "/attendance/punch-in", {
        "emp_code": "EMP8108", "punched_at": utc_before,
    })[1]["date"]
    after_date = api("POST", "/attendance/punch-in", {
        "emp_code": "EMP8108", "punched_at": utc_after,
    })[1]["date"]
    assert (before_date, after_date) == ("2026-07-10", "2026-07-11")


def test_attendance_filters_legacy_patch_and_history():
    employee = employee_doc("EMP8110")
    employees.insert_one(employee)
    employees.insert_one(employee_doc("EMP8111"))
    original_in = datetime(2030, 7, 6, 4, 30, tzinfo=UTC)
    original_out = datetime(2030, 7, 6, 13, 10, tzinfo=UTC)
    legacy = log_doc(
        "EMP8110", "2030-07-06", punch_in=original_in, punch_out=original_out,
        late=30, overtime=40, hours=8.67, history=False, half=None,
    )
    attendance.insert_one(legacy)
    attendance.insert_many([
        log_doc("EMP8111", "2030-07-06", "ABSENT"),
        log_doc("EMP8111", "2030-07-07"),
    ])

    invalid_queries = (
        "?page=0",
        "?page_size=101",
        "?date_from=2026-02-30",
        "?date_from=2026-07-09&date_to=2026-07-01",
        "?status=UNKNOWN",
    )
    for query in invalid_queries:
        assert api("GET", "/attendance" + query)[0] == 422

    page = api("GET", "/attendance?emp_code=EMP8110&date_from=2030-07-06&date_to=2030-07-06&status=PRESENT")[1]
    assert page["total"] == 1
    row = page["items"][0]
    assert row["history"] == [] and row["half_day"] is False
    assert_no_ids(page)
    ordered = api("GET", "/attendance?date_from=2030-07-06&date_to=2030-07-07&page_size=10")[1]["items"]
    assert [(record["date"], record["emp_code"]) for record in ordered] == [
        ("2030-07-07", "EMP8111"),
        ("2030-07-06", "EMP8110"),
        ("2030-07-06", "EMP8111"),
    ]

    request = {
        "punch_in": epoch_ist("2030-07-06T09:30:00"),
        "punch_out": epoch_ist("2030-07-06T13:59:24"),
        "reason": "Correct punch times",
        "regularized_by": "qa",
        "work_hours": 999,
        "late_minutes": 999,
        "overtime_minutes": 999,
        "half_day": False,
    }
    status, corrected = api("PATCH", "/attendance/EMP8110/2030-07-06", request)
    assert status == 200
    assert corrected["late_minutes"] == 0
    assert corrected["work_hours"] == 4.49
    assert corrected["overtime_minutes"] == 0
    assert corrected["half_day"] is True
    changes = corrected["history"][0]["changes"]
    assert set(changes) == {"punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"}
    assert changes["punch_in"]["from"] == int(original_in.timestamp() * 1000)
    assert changes["punch_in"]["to"] == request["punch_in"]
    assert isinstance(corrected["history"][0]["at"], int)
    assert_no_ids(corrected)

    second = api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "status": "WFH", "reason": "Status correction", "regularized_by": "qa",
    })[1]
    assert len(second["history"]) == 2
    assert set(second["history"][1]["changes"]) == {"status"}
    assert second["history"][0] == corrected["history"][0]
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "reason": "No actual change", "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "status": "ABSENT",
        "punch_in": epoch_ist("2030-07-06T09:30:00"),
        "punch_out": epoch_ist("2030-07-06T13:30:00"),
        "reason": "Invalid absent punches",
        "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "status": "LEAVE",
        "punch_in": epoch_ist("2030-07-06T09:30:00"),
        "punch_out": epoch_ist("2030-07-06T13:30:00"),
        "reason": "Invalid leave punches",
        "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "punch_in": epoch_ist("2030-07-07T09:30:00"),
        "reason": "Punch-in on another date",
        "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "punch_out": epoch_ist("2030-07-06T09:00:00"),
        "reason": "Punch-out before punch-in",
        "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8111/2030-07-06", {
        "status": "PRESENT",
        "reason": "Present without punch-in",
        "regularized_by": "qa",
    })[0] == 422
    assert api("PATCH", "/attendance/EMP8110/2026-07-30", {
        "reason": "Missing record", "regularized_by": "qa",
    })[0] == 404
    assert api("PATCH", "/attendance/EMP9998/2026-07-06", {
        "reason": "Missing employee", "regularized_by": "qa",
    })[0] == 404
    assert api("PATCH", "/attendance/EMP8110/2030-07-06", {
        "status": "LEAVE", "punch_out": epoch_ist("2030-07-06T18:30:00"),
        "reason": "Invalid leave", "regularized_by": "qa",
    })[0] == 422


def test_analytics_known_dataset_and_rounding():
    employees.insert_many([
        employee_doc("EMP8120", "Alpha", "2026-07-01"),
        employee_doc("EMP8121", "Alpha", "2026-07-15"),
        employee_doc("EMP8122", "Beta", "2026-07-01", "22:00", "06:00"),
        employee_doc("EMP8123", "Alpha", "2026-07-01"),
        employee_doc("EMP8124", "Alpha", "2026-07-01"),
        employee_doc("EMP8125", "Alpha", "2026-08-01"),
    ])
    logs = [
        log_doc("EMP8121", "2026-07-15", late=40, half=True, hours=4.49,
                punch_in=datetime(2026, 7, 15, 4, 20, tzinfo=UTC),
                punch_out=datetime(2026, 7, 15, 8, 49, 24, tzinfo=UTC)),
        log_doc("EMP8121", "2026-07-18", late=60, overtime=40, hours=9.0,
                punch_in=datetime(2026, 7, 18, 4, 30, tzinfo=UTC),
                punch_out=datetime(2026, 7, 18, 13, 30, tzinfo=UTC)),
        log_doc("EMP8121", "2026-07-20", "LEAVE"),
        log_doc("EMP8121", "2026-07-21", "ON_DUTY", hours=8.0,
                punch_in=datetime(2026, 7, 21, 4, 0, tzinfo=UTC),
                punch_out=datetime(2026, 7, 21, 12, 0, tzinfo=UTC)),
        log_doc("EMP8122", "2026-07-16", late=25, overtime=30, hours=8.0,
                punch_in=datetime(2026, 7, 16, 17, 30, tzinfo=UTC),
                punch_out=datetime(2026, 7, 17, 0, 30, tzinfo=UTC)),
        log_doc("EMP8123", "2026-07-15", late=30, hours=10.0),
        log_doc("EMP8123", "2026-07-16", late=30, hours=8.0),
        log_doc("EMP8124", "2026-07-15", late=60, hours=6.0),
        log_doc("EMP8199", "2026-07-15", late=999, overtime=999),
    ]
    attendance.insert_many(logs)

    monthly = api("GET", "/analytics/employees/EMP8121/monthly?month=2026-07")[1]
    assert_no_ids(monthly)
    python_weekdays = sum(
        1 for offset in range(17)
        if (date(2026, 7, 15) + timedelta(days=offset)).weekday() < 5
    )
    assert python_weekdays == 13
    assert monthly == {
        "emp_code": "EMP8121", "month": "2026-07", "working_days": 13,
        "present_days": 1.5, "leave_days": 1, "late_count": 2,
        "total_late_minutes": 100, "total_overtime_minutes": 40,
        "attendance_pct": float((Decimal("1.5") / Decimal(13) * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
    }
    zero_logs = api("GET", "/analytics/employees/EMP8120/monthly?month=2026-07")[1]
    assert zero_logs["working_days"] == 23 and zero_logs["present_days"] == 0
    assert zero_logs["attendance_pct"] == 0.0
    assert api("GET", "/analytics/employees/EMP8121/monthly?month=2026-06")[1]["attendance_pct"] is None
    assert api("GET", "/analytics/employees/EMP8198/monthly?month=2026-07")[0] == 404

    summary = api("GET", "/analytics/departments/summary?month=2026-07")[1]["items"]
    assert_no_ids(summary)
    assert [item["department"] for item in summary] == sorted(item["department"] for item in summary)
    alpha = next(item for item in summary if item["department"] == "Alpha")
    alpha_hours = [4.49, 9.0, 8.0, 10.0, 8.0, 6.0]
    expected_avg = float((sum(Decimal(str(item)) for item in alpha_hours) / len(alpha_hours)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    ))
    assert alpha["headcount"] == 4
    assert alpha["present_days"] == 4.5
    assert alpha["avg_work_hours"] == expected_avg == 7.58
    assert alpha["late_count"] == 5 and alpha["total_late_minutes"] == 220
    assert alpha["leave_count"] == 1 and alpha["on_duty_count"] == 1
    assert api("GET", "/analytics/departments/summary?month=2026-07&department=Alpha")[1]["items"] == [alpha]
    assert not any(item["department"] == "No such department" for item in summary)

    board = api("GET", "/analytics/leaderboard/late?month=2026-07&limit=2&department=Alpha")[1]["items"]
    assert_no_ids(board)
    assert [(item["rank"], item["emp_code"]) for item in board] == [
        (1, "EMP8121"), (2, "EMP8123"), (2, "EMP8124"),
    ]
    assert api("GET", "/analytics/leaderboard/late?month=2026-07&department=Beta")[1]["items"][0]["emp_code"] == "EMP8122"

    trend_response = api(
        "GET", "/analytics/departments/Alpha/trend?from=2026-07-15&to=2026-07-21",
    )[1]["items"]
    assert_no_ids(trend_response)
    assert len(trend_response) == 7
    assert [item["date"] for item in trend_response] == [
        f"2026-07-{day:02}" for day in range(15, 22)
    ]
    assert [item["headcount"] for item in trend_response] == [4] * 7
    assert trend_response[0]["present_count"] == 2.5
    assert trend_response[0]["attendance_rate"] == 0.625
    assert trend_response[1]["attendance_rate"] == 0.25
    assert trend_response[2]["attendance_rate"] == 0.0
    assert trend_response[3]["attendance_rate"] is None
    assert trend_response[3]["present_count"] == 1
    assert trend_response[4]["attendance_rate"] is None
    assert trend_response[5]["attendance_rate"] == 0.0
    assert trend_response[6]["attendance_rate"] == 0.25
    rates = [Decimal("0.625"), Decimal("0.25"), Decimal("0"), Decimal("0"), Decimal("0.25")]
    expected_moving = float((sum(rates) / len(rates)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    assert trend_response[-1]["moving_avg_7d"] == expected_moving == 0.225
    assert api("GET", "/analytics/departments/Unknown/trend?from=2026-07-15&to=2026-07-16")[0] == 404
    assert api("GET", "/analytics/departments/Alpha/trend?from=2026-07-16&to=2026-07-15")[0] == 422
    assert api("GET", "/analytics/departments/Alpha/trend?from=2026-07-01&to=2026-10-01")[0] == 422

    # Analytics must follow stored derived values, even when the value disagrees with the punch times.
    attendance.update_one({"emp_code": "EMP8121", "date": "2026-07-15"}, {"$set": {"late_minutes": 77}})
    assert api("GET", "/analytics/employees/EMP8121/monthly?month=2026-07")[1]["total_late_minutes"] == 137


def test_trend_92_day_inclusive_limit_and_no_500():
    assert create_employee("EMP8130", department="Range")[0] == 201
    assert len(api("GET", "/analytics/departments/Range/trend?from=2026-01-01&to=2026-04-02")[1]["items"]) == 92
    assert api("GET", "/analytics/departments/Range/trend?from=2026-01-01&to=2026-04-03")[0] == 422


def run_parallel(count, fn):
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda _: fn(), range(count)))


def test_20_thread_write_races_repeated_ten_times():
    assert create_employee("EMP8140")[0] == 201
    for day in range(1, 11):
        payload = {"emp_code": "EMP8140", "punched_at": epoch_ist(f"2026-08-{day:02}T09:30:00")}
        results = run_parallel(20, lambda: api("POST", "/attendance/punch-in", payload)[0])
        assert results.count(201) == 1 and results.count(409) == 19

        out_payload = {"emp_code": "EMP8140", "punched_at": payload["punched_at"] + 9 * 60 * 60 * 1000}
        results = run_parallel(20, lambda: api("POST", "/attendance/punch-out", out_payload)[0])
        assert results.count(200) == 1 and results.count(409) == 19

    assert create_employee("EMP8141")[0] == 201
    for day in range(11, 21):
        code = f"EMP814{day - 9:02}"
        payload = {
            "emp_code": code,
            "name": "Concurrent duplicate",
            "email": f"{code.lower()}@example.com",
            "department": "Audit",
            "joined_on": "2026-01-01",
        }
        results = run_parallel(20, lambda: api("POST", "/employees", payload)[0])
        assert results.count(201) == 1 and results.count(409) == 19

    for day in range(21, 31):
        punch_day = date(2026, 8, day)
        punch_in = datetime.combine(punch_day, time(4, 0), tzinfo=UTC)
        code = "EMP8141"
        attendance.insert_one(log_doc(
            code, punch_day.isoformat(), punch_in=punch_in, history=True,
        ))
        current = "PRESENT"
        body = {
            "status": "WFH",
            "reason": "Concurrent correction",
            "regularized_by": "qa",
        }
        url = f"/attendance/{code}/{punch_day.isoformat()}"
        results = run_parallel(20, lambda: api("PATCH", url, body)[0])
        assert all(value in (200, 409, 422) for value in results), results
        stored = attendance.find_one({"emp_code": code, "date": punch_day.isoformat()})
        assert len(stored["history"]) == results.count(200)

    for day in range(1, 11):
        punch_day = date(2026, 9, day)
        code = "EMP8141"
        punch_in = datetime.combine(punch_day, time(4, 0), tzinfo=UTC)
        attendance.insert_one(log_doc(code, punch_day.isoformat(), punch_in=punch_in))
        path = f"/attendance/{code}/{punch_day.isoformat()}"
        patch = {"status": "WFH", "reason": "Concurrent patch", "regularized_by": "qa"}
        punchout = {"emp_code": code, "punched_at": int(punch_in.timestamp() * 1000) + 9 * 60 * 60 * 1000}
        with ThreadPoolExecutor(max_workers=2) as pool:
            patch_future = pool.submit(api, "PATCH", path, patch)
            punchout_future = pool.submit(api, "POST", "/attendance/punch-out", punchout)
            patch_status = patch_future.result()[0]
            punchout_status = punchout_future.result()[0]
        assert patch_status in (200, 409) and punchout_status in (200, 409)
        stored = attendance.find_one({"emp_code": code, "date": punch_day.isoformat()})
        assert len(stored.get("history", [])) == (patch_status == 200)
