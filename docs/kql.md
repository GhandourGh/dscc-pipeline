# KQL queries

Run them in **Application Insights → Logs** (resource `appi-<team>`). Screenshots go in
`docs/screenshots/kql-1.png`, `kql-2.png`, `kql-3.png`.

## 1. Inferences per hour

```kusto
customMetrics
| where timestamp > ago(24h)
| where name == "inferences"
| summarize rows_scored = sum(valueSum), calls = sum(valueCount) by bin(timestamp, 1h)
| order by timestamp asc
| render columnchart
```

## 2. The 5 slowest files (upload → result stored)

```kusto
traces
| where timestamp > ago(7d)
| where message == "file_processed"
| extend file = tostring(customDimensions.fileName),
         e2e_ms = todouble(customDimensions.endToEndMs),
         api_ms = todouble(customDimensions.apiCallMs),
         model_ms = todouble(customDimensions.modelLatencyMs)
| top 5 by e2e_ms desc
| project timestamp, file, e2e_ms, api_ms, model_ms
```

## 3. HTTP status codes and errors

```kusto
requests
| where timestamp > ago(24h)
| summarize count() by cloud_RoleName, name, resultCode
| order by cloud_RoleName asc, count_ desc
```

Errors only, by type:

```kusto
union
  (requests | where success == false | project timestamp, kind = "failed request", detail = strcat(name, " ", resultCode)),
  (exceptions | project timestamp, kind = "exception", detail = outerMessage)
| where timestamp > ago(24h)
| summarize count() by kind, detail
| order by count_ desc
```

## Extra: p95 model latency (custom metric)

```kusto
customMetrics
| where timestamp > ago(24h) and name == "model_latency_ms"
| summarize avg_ms = sum(valueSum) / sum(valueCount), max_ms = max(valueMax) by bin(timestamp, 15m)
```

## Alert rule (deployed by `infra/deploy.sh`)

`alert-pipeline-errors-<team>` is a log alert evaluated every 15 minutes:

```kusto
union (requests | where success == false), (exceptions)
| where timestamp > ago(15m)
```

It fires when the count is above 3. **To trigger it on purpose**, upload `samples/poison_values.csv`.
The worker fails 5 times (5 exceptions plus 5 API 422s), the message moves to
`inference-requests-poison`, and the alert email arrives within about 15 minutes.
