# Employee Attendance & Analytics API

FastAPI service backed by MongoDB 6.0+ (MongoDB 7 is recommended). Configure `MONGO_URI` and `MONGO_DB` in the
process environment. A local `.env` is optional; environment variables take precedence. The app creates its indexes
idempotently at startup.

```powershell
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

For the supplied MongoDB Atlas cluster, set the URI in your local PowerShell session instead
(replace `<db_password>` with the database user's URL-encoded password):

```powershell
$env:MONGO_URI = "mongodb+srv://<username>:<db_password>@<cluster-host>/?appName=<app-name>"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Or on macOS/Linux, export the two variables and run the same `uvicorn` command. Check readiness at `GET /health`.
No endpoint returns MongoDB `_id`; attendance is addressed by employee code and IST attendance date.

## Assumptions and rule choices

1. When `punched_at` is omitted, the server's current UTC instant is used; all supplied epoch milliseconds are truncated
   to whole seconds before storage or calculations.
2. A shift with `shift_end <= shift_start` is overnight. A punch-in before its IST shift end belongs to the prior
   attendance date, and the scheduled end is the following day.
3. Month/date ranges are inclusive calendar ranges. Working days are weekdays only; weekends still contribute to
   late/overtime metrics, and department headcount includes employees joined by the period's final day even with no logs.

Analytics read persisted derived attendance values, with missing legacy values treated as documented defaults. Use a
dedicated test database for any integration or load testing.
