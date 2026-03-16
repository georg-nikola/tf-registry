# tf-registry

A self-hosted [Terraform Module Registry](https://developer.hashicorp.com/terraform/internals/module-registry-protocol) — upload, version, and browse reusable Terraform modules. Fully compatible with the `terraform` CLI so modules can be sourced directly by address.

## Features

- **Terraform CLI compatible** — implements the registry protocol; use `source = "your-domain.com/namespace/module/provider"`
- **Upload & versioning** — publish `.tar.gz` archives with semver versions via the UI or API
- **Browse & search** — filter by namespace, provider, or keyword; view README and usage snippets per module
- **API key management** — generate, list, and revoke keys through the UI; bootstrap key via K8s secret
- **Security** — rate limiting, WAF rules, read-only root filesystem, non-root containers, NetworkPolicy on PostgreSQL

## Stack

| Layer | Technology |
|---|---|
| Frontend | nginx (static HTML/CSS/JS, no framework) |
| Backend | FastAPI + SQLAlchemy (async) + asyncpg |
| Database | PostgreSQL 16 |
| Storage | Persistent volume (`.tar.gz` archives) |
| Ingress | Traefik IngressRoute |
| GitOps | ArgoCD + ArgoCD Image Updater |
| Tunnel | Cloudflare Tunnel |

## Quick start

### 1. Create secrets

```bash
./scripts/create-k8s-secrets.sh
# Prompts for DB password; generates API key automatically
# API key is saved to .api_key (gitignored)
```

### 2. Deploy via ArgoCD

```bash
kubectl apply \
  -f helm/application-postgresql.yaml \
  -f helm/application-tf-registry-api.yaml \
  -f helm/application-tf-registry.yaml
```

### 3. Add Cloudflare tunnel entry

Add your registry hostname to your `cloudflared` ConfigMap ingress rules:

```yaml
- hostname: tf-registry.your-domain.com
  service: http://traefik.traefik.svc.cluster.local:80
```

### 4. Open the registry

Navigate to your domain. On first visit, go to **Keys** and enter your bootstrap API key (from `.api_key`) to generate a named key for day-to-day use.

## Using with Terraform

```hcl
# Configure the registry host
terraform {
  required_version = ">= 1.0"
}

# Reference a module
module "vpc" {
  source  = "tf-registry.your-domain.com/myorg/vpc/aws"
  version = "1.2.0"
}
```

Terraform will use `/.well-known/terraform.json` for service discovery automatically.

## API

All write operations require `Authorization: Bearer <key>`.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/.well-known/terraform.json` | — | Service discovery |
| `GET` | `/v1/modules` | — | List modules (`?q=`, `?namespace=`, `?provider=`, `?offset=`, `?limit=`) |
| `GET` | `/v1/modules/{ns}/{name}/{provider}/versions` | — | List versions |
| `GET` | `/v1/modules/{ns}/{name}/{provider}` | — | Latest version info |
| `GET` | `/v1/modules/{ns}/{name}/{provider}/{version}` | — | Specific version info |
| `GET` | `/v1/modules/{ns}/{name}/{provider}/{version}/download` | — | Terraform download (returns `X-Terraform-Get` header) |
| `GET` | `/v1/modules/{ns}/{name}/{provider}/{version}/archive` | — | Download `.tar.gz` |
| `POST` | `/v1/modules/{ns}/{name}/{provider}/{version}` | ✓ | Upload module (multipart `file`) |
| `DELETE` | `/v1/modules/{ns}/{name}/{provider}/{version}` | ✓ | Delete version |
| `GET` | `/api/keys` | ✓ | List API keys |
| `POST` | `/api/keys` | ✓ | Generate new key (`{"name": "..."}`) — full key shown once |
| `DELETE` | `/api/keys/{id}` | ✓ | Revoke key |
| `GET` | `/api/health` | — | Health check |

## Uploading a module

```bash
# Create archive from a module directory
tar -czf my-vpc-1.0.0.tar.gz -C ./my-vpc .

# Upload via curl
curl -X POST \
  -H "Authorization: Bearer <your-api-key>" \
  -F "file=@my-vpc-1.0.0.tar.gz" \
  "https://tf-registry.your-domain.com/v1/modules/myorg/vpc/aws/1.0.0"
```

Or use the **Upload** page in the UI.

## Development

### Running locally

```bash
# Start backend + database
docker compose up -d

# Backend available at http://localhost:8000
# Frontend (nginx) at http://localhost:8080
```

### Smoke tests

```bash
pip install requests playwright
python -m playwright install chromium

# Against production
python tests/test_tf_registry.py

# Against local instance
python tests/test_tf_registry.py --url http://localhost:8080

# API tests only (no browser)
python tests/test_tf_registry.py --skip-frontend
```

The test suite covers 51 assertions: health, service discovery, auth rejection, full module lifecycle (upload → list → download → delete), search/filter, key management (create → use → revoke), edge cases, and Playwright frontend tests.

## Repo structure

```
tf-registry/
├── backend/
│   ├── main.py          # FastAPI app + all endpoints
│   ├── models.py        # SQLAlchemy models (Module, ApiKey)
│   ├── database.py      # Async engine + session
│   ├── storage.py       # Archive save/read/delete + README extraction
│   └── Dockerfile
├── frontend/
│   ├── index.html       # Browse/search page
│   ├── module.html      # Module detail + versions
│   ├── upload.html      # Upload form
│   ├── keys.html        # API key management
│   ├── app.js           # All frontend logic
│   ├── style.css        # Dark theme
│   └── Dockerfile       # nginx
├── helm/
│   ├── tf-registry/         # Frontend Helm chart
│   ├── tf-registry-api/     # Backend Helm chart (with PVC)
│   ├── postgresql/          # PostgreSQL StatefulSet chart
│   ├── application-*.yaml   # ArgoCD Application manifests
├── tests/
│   └── test_tf_registry.py  # Smoke tests
├── scripts/
│   └── create-k8s-secrets.sh
└── .github/workflows/
    ├── docker-publish.yml   # Build + push on semver tag
    └── security-scan.yml    # Gitleaks, Bandit, pip-audit, Trivy, Kubescape
```

## K8s secrets

Two secrets are required:

```bash
# postgresql-credentials
kubectl create secret generic postgresql-credentials \
  --from-literal=password=<db-password>

# tf-registry-api-secrets
kubectl create secret generic tf-registry-api-secrets \
  --from-literal=DATABASE_URL="postgresql+asyncpg://tfregistry:<password>@tf-registry-postgresql.default.svc.cluster.local:5432/tfregistry" \
  --from-literal=API_KEY=<bootstrap-api-key>
```

Or use `./scripts/create-k8s-secrets.sh` which handles generation and prompts.
