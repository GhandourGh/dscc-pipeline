#!/usr/bin/env bash
# =============================================================================
# Foundation of the event-driven ML pipeline, for an Azure for Students account.
# Run by the OWNER of the integration subscription (role assignments need Owner).
#
#   cp infra/.env.example infra/.env && edit it
#   ./infra/deploy.sh check        # allowed regions + Flex regions, register providers
#   ./infra/deploy.sh foundation   # all resources + identities + roles + budget + alert
#   (push to main -> GitHub Actions deploys API, functions and dashboard)
#   ./infra/deploy.sh eventgrid    # AFTER the first functions deployment
#   ./infra/deploy.sh outputs      # URLs and values to put in GitHub
#   ./infra/deploy.sh cleanup      # after the defence: delete everything + proofs
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source ./.env

RG=rg-projet-$EQ
ST=st${EQ}pipe                 # globally unique, lowercase, no hyphen
COSMOS=cosmos-$EQ
ENV=cae-$EQ
API=api-$EQ
FUNC=func-$EQ
ID=id-github-$EQ
LAW=log-$EQ
AI=appi-$EQ
AG=ag-$EQ
IMAGE=ghcr.io/$(echo "$GH_REPO" | tr "[:upper:]" "[:lower:]")/api:1.0.0
SUB=$(az account show --query id -o tsv)

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

check() {
  log "Subscription"; az account show --query "{name:name, id:id, user:user.name}" -o table
  log "1. Allowed regions (policy)"
  az policy assignment list --disable-scope-strict-match \
    --query "[?contains(displayName,'region') || contains(displayName,'location')].parameters" -o json
  log "2. Flex Consumption regions"
  az functionapp list-flexconsumption-locations -o table
  log "3. Registering providers"
  for p in Microsoft.Web Microsoft.App Microsoft.EventGrid Microsoft.DocumentDB \
           Microsoft.OperationalInsights Microsoft.Insights Microsoft.ManagedIdentity \
           Microsoft.Storage Microsoft.Consumption; do
    az provider register --namespace "$p" -o none; echo "  $p"; done
}

foundation() {
  log "Resource group (tagged)"
  az group create -n "$RG" -l "$LOC" --tags projet=dscc equipe="$EQ" -o none

  # Cosmos first: most often refused for capacity -> validates the region early.
  log "Cosmos DB Free Tier (CLI, not portal)"
  az cosmosdb show -n "$COSMOS" -g "$RG" -o none 2>/dev/null || \
  az cosmosdb create -n "$COSMOS" -g "$RG" --enable-free-tier true \
    --locations regionName="$LOC" failoverPriority=0 isZoneRedundant=False \
    --disable-key-based-metadata-write-access true -o none
  az cosmosdb sql database create -a "$COSMOS" -g "$RG" -n pipeline --throughput 400 -o none 2>/dev/null || true
  az cosmosdb sql container create -a "$COSMOS" -g "$RG" -d pipeline -n inferences \
    --partition-key-path /fileName -o none 2>/dev/null || true

  log "Storage: containers, versioning, queues, static website"
  az storage account create -n "$ST" -g "$RG" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false -o none
  az storage account blob-service-properties update -n "$ST" -g "$RG" --enable-versioning true -o none
  for c in input output rejected models; do
    az storage container create --account-name "$ST" -n "$c" --auth-mode key -o none; done
  for q in inference-requests inference-requests-poison; do
    az storage queue create --account-name "$ST" -n "$q" --auth-mode key -o none; done
  az storage blob service-properties update --account-name "$ST" --static-website \
    --index-document index.html --auth-mode key -o none
  az storage blob upload --account-name "$ST" -c models -f ../model/model-1.0.0.joblib \
    -n model-1.0.0.joblib --overwrite --auth-mode key -o none

  log "Monitoring FIRST: capped workspace (0.2 GB/day) + workspace-based App Insights"
  az monitor log-analytics workspace create -g "$RG" -n "$LAW" -l "$LOC" --quota 0.2 -o none
  LAW_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query id -o tsv)
  az monitor app-insights component create --app "$AI" -g "$RG" -l "$LOC" --workspace "$LAW_ID" -o none
  AI_CS=$(az monitor app-insights component show --app "$AI" -g "$RG" --query connectionString -o tsv)

  log "Container Apps: ONE environment, API scales 0..2"
  az containerapp env show -n "$ENV" -g "$RG" -o none 2>/dev/null || \
  az containerapp env create -n "$ENV" -g "$RG" -l "$LOC" \
    --logs-workspace-id "$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query customerId -o tsv)" \
    --logs-workspace-key "$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LAW" --query primarySharedKey -o tsv)" \
    -o none
  ENV_ID=$(az containerapp env show -n "$ENV" -g "$RG" --query id -o tsv)
  cat > /tmp/containerapp-$EQ.yaml <<YAML
location: $LOC
name: $API
properties:
  managedEnvironmentId: $ENV_ID
  configuration:
    activeRevisionsMode: Single
    ingress: { external: true, targetPort: 8000, transport: auto }
    secrets:
      - name: appinsights-cs
        value: "$AI_CS"
  template:
    containers:
      - name: api
        image: $IMAGE
        resources: { cpu: 0.25, memory: 0.5Gi }
        env:
          - { name: APPLICATIONINSIGHTS_CONNECTION_STRING, secretRef: appinsights-cs }
          - { name: OTEL_SERVICE_NAME, value: ml-api }
        probes:
          - type: Liveness
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 30
          - type: Readiness
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 3
            periodSeconds: 10
    scale:
      minReplicas: 0
      maxReplicas: 2
      rules:
        - name: http
          http: { metadata: { concurrentRequests: "20" } }
YAML
  az containerapp show -n "$API" -g "$RG" -o none 2>/dev/null \
    && az containerapp update -n "$API" -g "$RG" --yaml /tmp/containerapp-$EQ.yaml -o none \
    || az containerapp create -n "$API" -g "$RG" --yaml /tmp/containerapp-$EQ.yaml -o none
  rm -f /tmp/containerapp-$EQ.yaml   # contains the App Insights connection string
  API_FQDN=$(az containerapp show -n "$API" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)

  log "Function App: Flex Consumption, Python 3.12, 512 MB"
  az functionapp show -n "$FUNC" -g "$RG" -o none 2>/dev/null || \
  az functionapp create -n "$FUNC" -g "$RG" --storage-account "$ST" --app-insights "$AI" \
    --flexconsumption-location "$LOC" --runtime python --runtime-version 3.12 \
    --instance-memory 512 --assign-identity '[system]' -o none
  PID=$(az functionapp identity show -n "$FUNC" -g "$RG" --query principalId -o tsv)
  ST_ID=$(az storage account show -n "$ST" -g "$RG" --query id -o tsv)

  log "Role assignments for the Function App identity (made ONCE by the owner)"
  for r in "Storage Blob Data Owner" "Storage Queue Data Contributor" "Storage Account Contributor"; do
    az role assignment create --role "$r" --assignee-object-id "$PID" \
      --assignee-principal-type ServicePrincipal --scope "$ST_ID" -o none; done
  az cosmosdb sql role assignment create -a "$COSMOS" -g "$RG" --scope / \
    --principal-id "$PID" --role-definition-id 00000000-0000-0000-0000-000000000002 -o none 2>/dev/null || true

  log "Function App settings: identity-based connections, no keys"
  COSMOS_EP=$(az cosmosdb show -n "$COSMOS" -g "$RG" --query documentEndpoint -o tsv)
  az functionapp config appsettings set -n "$FUNC" -g "$RG" -o none --settings \
    AzureWebJobsStorage__accountName="$ST" \
    STORAGE_ACCOUNT_NAME="$ST" \
    COSMOS_ENDPOINT="$COSMOS_EP" COSMOS_DATABASE=pipeline COSMOS_CONTAINER=inferences \
    API_URL="https://$API_FQDN" API_TIMEOUT_S=10 MAX_FILE_BYTES=1048576 MAX_ROWS=1000 \
    PYTHON_ENABLE_OPENTELEMETRY=true
  az functionapp config appsettings delete -n "$FUNC" -g "$RG" --setting-names AzureWebJobsStorage -o none || true
  az functionapp deployment config set -n "$FUNC" -g "$RG" \
    --deployment-storage-auth-type SystemAssignedIdentity -o none || true
  az functionapp config appsettings delete -n "$FUNC" -g "$RG" \
    --setting-names DEPLOYMENT_STORAGE_CONNECTION_STRING -o none 2>/dev/null || true

  log "CORS: /api/recent only callable from the dashboard origin"
  WEB=$(az storage account show -n "$ST" -g "$RG" --query primaryEndpoints.web -o tsv)
  az functionapp cors add -n "$FUNC" -g "$RG" --allowed-origins "${WEB%/}" -o none

  github_identity
  budget_and_alerts
  outputs
}

github_identity() {
  log "User-assigned managed identity for GitHub Actions (OIDC, no secret)"
  az identity create -n "$ID" -g "$RG" -l "$LOC" -o none
  # Public repo: subject = environment:production. Private repo: use
  #   repo:$GH_REPO:ref:refs/heads/main  (and workflow_dispatch instead of reviewers)
  az identity federated-credential create -n github-production --identity-name "$ID" -g "$RG" \
    --issuer https://token.actions.githubusercontent.com \
    --subject "repo:$GH_REPO:environment:production" \
    --audiences api://AzureADTokenExchange -o none 2>/dev/null || true
  az role assignment create --role Contributor \
    --assignee-object-id "$(az identity show -n "$ID" -g "$RG" --query principalId -o tsv)" \
    --assignee-principal-type ServicePrincipal \
    --scope "$(az group show -n "$RG" --query id -o tsv)" -o none
}

budget_and_alerts() {
  log "Action group (email)"
  az monitor action-group create -n "$AG" -g "$RG" --short-name "${EQ:0:12}" \
    --action email owner "$ALERT_EMAIL" -o none
  AG_ID=$(az monitor action-group show -n "$AG" -g "$RG" --query id -o tsv)

  log "Budget \$25 on the resource group, alerts at 20/50/80 %"
  START=$(date -u +%Y-%m-01)
  END=$(date -u -v+6m +%Y-%m-01 2>/dev/null || date -u -d "+6 months" +%Y-%m-01)
  az rest --method put \
    --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Consumption/budgets/budget-$EQ?api-version=2023-05-01" \
    --body "{\"properties\":{\"category\":\"Cost\",\"amount\":25,\"timeGrain\":\"Monthly\",
      \"timePeriod\":{\"startDate\":\"${START}T00:00:00Z\",\"endDate\":\"${END}T00:00:00Z\"},
      \"notifications\":{
        \"p20\":{\"enabled\":true,\"operator\":\"GreaterThan\",\"threshold\":20,\"contactEmails\":[\"$ALERT_EMAIL\"]},
        \"p50\":{\"enabled\":true,\"operator\":\"GreaterThan\",\"threshold\":50,\"contactEmails\":[\"$ALERT_EMAIL\"]},
        \"p80\":{\"enabled\":true,\"operator\":\"GreaterThan\",\"threshold\":80,\"contactEmails\":[\"$ALERT_EMAIL\"]}}}}" -o none

  log "Log alert: pipeline errors in the last 15 min (evaluated every 15 min)"
  AI_ID=$(az monitor app-insights component show --app "$AI" -g "$RG" --query id -o tsv)
  az monitor scheduled-query create -n "alert-pipeline-errors-$EQ" -g "$RG" --scopes "$AI_ID" \
    --description "Worker failures / API 5xx above threshold" \
    --condition "count 'Errors' > 3" \
    --condition-query Errors="union (requests | where success == false), (exceptions) | where timestamp > ago(15m)" \
    --evaluation-frequency 15m --window-size 15m --severity 2 \
    --action-groups "$AG_ID" -o none
}

eventgrid() {
  log "Event Grid: BlobCreated on input/ -> dispatcher (function must be deployed first)"
  az eventgrid event-subscription create -n blob-to-dispatcher \
    --source-resource-id "$(az storage account show -n "$ST" -g "$RG" --query id -o tsv)" \
    --endpoint-type azurefunction \
    --endpoint "$(az functionapp show -n "$FUNC" -g "$RG" --query id -o tsv)/functions/dispatcher" \
    --included-event-types Microsoft.Storage.BlobCreated \
    --subject-begins-with /blobServices/default/containers/input/ \
    --max-delivery-attempts 10 --event-ttl 60 -o none
  echo "Event Grid subscription created."
}

outputs() {
  log "Values for GitHub"
  echo "Secrets (identifiers only, not credentials):"
  echo "  AZURE_CLIENT_ID       = $(az identity show -n "$ID" -g "$RG" --query clientId -o tsv)"
  echo "  AZURE_TENANT_ID       = $(az account show --query tenantId -o tsv)"
  echo "  AZURE_SUBSCRIPTION_ID = $SUB"
  echo "Variables:"
  echo "  AZ_RESOURCE_GROUP=$RG  AZ_API_APP=$API  AZ_FUNC_APP=$FUNC  AZ_STORAGE_ACCOUNT=$ST"
  log "Endpoints"
  echo "  API        https://$(az containerapp show -n "$API" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)"
  echo "  Functions  https://$(az functionapp show -n "$FUNC" -g "$RG" --query defaultHostName -o tsv 2>/dev/null)/api/recent"
  echo "  Dashboard  $(az storage account show -n "$ST" -g "$RG" --query primaryEndpoints.web -o tsv)"
}

cleanup() {
  log "Deleting $RG (everything, including the budget and alerts)"
  az group delete -n "$RG" --yes
  echo "az group exists -n $RG -> $(az group exists -n "$RG")"
  az group list -o table
  echo "Check above: no DefaultResourceGroup-* / NetworkWatcherRG left over."
}

help() { sed -n "2,14p" "$0"; }
"${1:-help}"
