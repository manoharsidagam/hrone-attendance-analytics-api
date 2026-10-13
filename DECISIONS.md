# DECISIONS.md

### 1. Indexes
I created a unique index on `employees.emp_code` to enforce employee-code uniqueness, and a compound index on `department` and `emp_code` for department-based employee lookups. On `attendance_logs`, I created a unique compound index on `emp_code` and `date` to prevent duplicate daily attendance records. I also added indexes on employee/date, date/status, and employee/punch-in fields to support attendance filtering and sorting. I would verify whether the two employee/date indexes are both necessary, since redundant indexes increase storage and write costs.

### 2. Punch-in race
Two identical punch-in requests may arrive before either request sees the other's record. The unique compound index ensures that only one record can be inserted for the same employee and date. The successful request returns `201`, while the request that encounters a duplicate-key error returns `409 Conflict`.

### 3. Leaderboard ties

I use MongoDB's `$rank` window function to implement competition ranking, so equal total late minutes receive the same rank (for example, 1, 2, 2, 4). The department filter is applied before ranking so employees are ranked only against others in the selected department. I apply the limit after ranking using `rank <= limit`, which preserves all employees tied at the cutoff. Therefore, the response can contain more rows than the requested limit. Finally, I sort by total late minutes descending and employee code ascending to make the output deterministic.

### 4. Headcount

Department headcount is calculated using employees whose `joined_on` date is on or before the end of the requested reporting period. Employees who have not joined by that date are excluded. Employees who joined during the period are included only from their joining date onward in the daily trend. This ensures that headcount includes eligible employees even when they have no attendance logs, rather than calculating headcount only from attendance records.

### 5. Handling 100x more data

If the dataset grows by 100 times, I would first inspect MongoDB execution statistics and query latency to identify the actual bottlenecks. I would review index usage and remove redundant indexes where safe, since extra indexes increase storage and write costs. I would also check whether aggregation pipelines filter records early and use indexes effectively. For expensive analytics, I would consider pre-aggregating frequently requested metrics if the business requirements allow slightly delayed updates. I would add load tests using realistic data volumes and monitor query execution time, memory usage, and database resource consumption before making further changes.