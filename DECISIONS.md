# DECISIONS.md — draft

1. **Indexes.** A unique employee-code index prevents duplicate employees. The department/code and joined-date indexes support lists, trends, and headcount. Attendance uses unique employee/date for one record per day, date/code for lists and monthly scans, and status/date/code for status-filtered lists. I skipped separate indexes for every optional filter because they add write cost without serving a required query.
2. **Punch-in race.** Both requests may read the employee and derive the same attendance date. Both attempt insertion. MongoDB's unique employee/date index accepts one and rejects the other; the duplicate-key error becomes HTTP 409.
3. **Ties.** MongoDB competition rank shares the cutoff rank. The API returns every employee whose rank is within the limit, sorted by total late minutes and then employee code.
4. **Headcount.** The summary starts with employees joined by month-end, then looks up each person's logs. An employee with no matching logs still contributes to department headcount.
5. **Scale.** At one hundred times the data, I would benchmark database capacity and the existing indexed pipelines first. If measurements justified it, I would consider sharding or a daily summary collection while keeping raw logs as the audit source.
