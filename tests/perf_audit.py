"""Measure the seeded 100k-record API workload and check explain plans."""

from datetime import date
import json
import math
import os
import statistics
import time

import httpx
from pymongo import MongoClient


BASE_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB = os.environ["MONGO_DB"]
database = MongoClient(MONGO_URI, tz_aware=True)[MONGO_DB]

cases = {
    "employee_list": "/employees?page=1&page_size=100",
    "attendance_list": "/attendance?date_from=2025-01-01&date_to=2025-04-10&status=ABSENT&page_size=100",
    "employee_monthly": "/analytics/employees/EMP1000/monthly?month=2025-01",
    "department_summary": "/analytics/departments/summary?month=2025-01",
    "late_leaderboard": "/analytics/leaderboard/late?month=2025-01&limit=10",
    "department_trend": "/analytics/departments/Load/trend?from=2025-01-01&to=2025-01-07",
}
explain_cases = {
    "attendance_list": "/admin/explain/attendance_list?date_from=2025-01-01&date_to=2025-04-10&status=ABSENT&page_size=100",
    "employee_monthly": "/admin/explain/employee_monthly?emp_code=EMP1000&month=2025-01",
    "department_summary": "/admin/explain/department_summary?month=2025-01",
    "late_leaderboard": "/admin/explain/late_leaderboard?month=2025-01&limit=10",
    "department_trend": "/admin/explain/department_trend?department=Load&from=2025-01-01&to=2025-01-07",
}

attendance_count = database.attendance_logs.count_documents({"emp_code": {"$regex": "^EMP1"}})
employee_count = database.employees.count_documents({"emp_code": {"$regex": "^EMP1"}})
assert attendance_count >= 100_000, f"expected >=100,000 load rows, got {attendance_count}"
assert employee_count >= 500, f"expected >=500 load employees, got {employee_count}"
print(f"DATA employees={employee_count:,} attendance={attendance_count:,}")

for name, path in cases.items():
    samples = []
    for _ in range(12):
        started = time.perf_counter()
        response = httpx.get(BASE_URL + path, timeout=120)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, f"{name} returned {response.status_code}: {response.text}"
        samples.append(elapsed_ms)
    ordered = sorted(samples)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    print(f"TIMING {name:20s} p50={statistics.median(samples):8.2f}ms p95={p95:8.2f}ms max={max(samples):8.2f}ms")


def stages(value):
    if isinstance(value, dict):
        if isinstance(value.get("stage"), str):
            yield value["stage"]
        for nested in value.values():
            yield from stages(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from stages(nested)


for name, path in explain_cases.items():
    response = httpx.get(BASE_URL + path, timeout=120)
    assert response.status_code == 200, f"{name} explain returned {response.status_code}: {response.text}"
    payload = response.json()
    assert set(payload) == {"endpoint", "collection", "explain"}
    encoded = json.dumps(payload["explain"])
    assert "IXSCAN" in encoded, f"{name} explain did not show IXSCAN"
    assert "COLLSCAN" not in encoded, f"{name} explain showed COLLSCAN"
    print(f"EXPLAIN {name} IXSCAN=yes COLLSCAN=no stages={sorted(set(stages(payload['explain'])))}")


for name, path in {
    "employee_monthly": "/admin/explain/employee_monthly?month=2025-01",
    "department_summary": "/admin/explain/department_summary",
    "late_leaderboard": "/admin/explain/late_leaderboard",
    "department_trend": "/admin/explain/department_trend?department=Load",
}.items():
    response = httpx.get(BASE_URL + path, timeout=30)
    assert response.status_code == 422, f"{name} missing required params returned {response.status_code}"
    print(f"MISSING_PARAMS {name}=422")

attendance_explain = httpx.get(
    BASE_URL + explain_cases["attendance_list"], timeout=120,
).json()["explain"]
sort_stages = [stage for stage in stages(attendance_explain) if stage in {"SORT", "SORT_KEY_GENERATOR"}]
assert not sort_stages, f"attendance list uses in-memory sort stages: {sort_stages}"
print("ATTENDANCE_LIST in_memory_sort=no")
