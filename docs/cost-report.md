# Cost report

Scope: resource group `rg-projet-<team>`, tag `projet=dscc`. Envelope: **$25 maximum**.

## 1. Initial estimate (15 days)

| Service | Configuration | Free grant used | Estimate |
|---|---|---|---|
| Functions | Flex Consumption, 512 MB, 3 functions | 250k executions, 100k GB-s / month | $0.00 |
| Container Apps | 0.25 vCPU / 0.5 GiB, 0 to 2 replicas | 180k vCPU-s, 360k GiB-s, 2M requests | $0.00 |
| Event Grid | system topic, BlobCreated on `input/` | 100k operations / month | $0.00 |
| Storage | Standard LRS hot: blobs, 2 queues, `$web` | (none) | < $0.20 |
| Cosmos DB | Free Tier, 1 database at 400 RU/s | 1,000 RU/s + 25 GB | $0.00 |
| Log Analytics + App Insights | daily cap 0.2 GB | 5 GB / month | $0.00 |
| Log alert | 1 rule, every 15 min | (none) | < $0.50 |
| Budget, action group | email | free | $0.00 |
| GitHub Actions + GHCR | public repository | free | $0.00 |
| **Total** | | | **≈ $0 to $1** |

We chose not to use ACR (≈ $0.17/day); the image is in GHCR instead.

## 2. Actual cost on D15

> _To fill in on D15:_ Cost Management → Cost analysis → filter **Tag: projet = dscc**,
> range = project dates. Screenshot: `docs/screenshots/cost-analysis-d15.png`.

| | Amount |
|---|---|
| Estimated | ≈ $0 to $1 |
| Actual (D15) | $ ___ |
| Gap | $ ___ |

**Explaining the gap** (fill in): e.g. log alert billed per rule-month, storage transactions
from load tests, Cosmos DB above the free RU/s during the burst test.

## 3. Budget

`budget-<team>`: $25 per month on the resource group, email alerts at 20, 50 and 80 %.
Screenshot: `docs/screenshots/budget.png`.

## 4. Proof of deletion (after the defence)

```bash
./infra/deploy.sh cleanup
az group exists -n rg-projet-<team>      # -> false
az group list -o table                   # no DefaultResourceGroup-* left
```

Screenshots: `docs/screenshots/group-exists-false.png`, `docs/screenshots/group-list.png`.
