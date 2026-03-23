#!/bin/bash
# Creates Kubernetes secrets for tf-registry-api and postgresql.
# Run this once before deploying. Values are NOT stored in git.
#
# Usage:
#   ./scripts/create-k8s-secrets.sh            # Apply plain secrets directly
#   ./scripts/create-k8s-secrets.sh --sealed    # Generate SealedSecret YAMLs (safe for git)
#
# Required inputs (will prompt if not set as env vars):
#   DB_PASSWORD  - PostgreSQL password for tfregistry user
#   USERNAME     - Admin username for the registry UI
#   PASSWORD     - Admin password for the registry UI
#
# Sealed Secrets migration path:
#   1. Deploy sealed-secrets controller to the cluster (see talos-configs ArgoCD app)
#   2. Install kubeseal: brew install kubeseal
#   3. Run this script with --sealed to generate SealedSecret YAML files
#   4. Commit the SealedSecret files to git (they are encrypted, safe to store)
#   5. Apply them: kubectl apply -f <sealed-secret-file>.yaml
#   6. The controller decrypts them into regular Secrets in-cluster

set -e

CONTEXT="admin@home-talos-k8s-cluster"
NAMESPACE="default"
SEALED=false
SEALED_OUTPUT_DIR="./sealed-secrets"

if [ "$1" = "--sealed" ]; then
    SEALED=true
    echo "Mode: Sealed Secrets (will generate SealedSecret YAML files)"
    echo "Prerequisites: kubeseal CLI installed, sealed-secrets controller running in cluster"
    if ! command -v kubeseal &> /dev/null; then
        echo "ERROR: kubeseal not found. Install with: brew install kubeseal"
        exit 1
    fi
    mkdir -p "$SEALED_OUTPUT_DIR"
    echo ""
fi

read_secret() {
  local var="$1"
  local prompt="$2"
  if [ -z "${!var}" ]; then
    read -rsp "$prompt: " value
    echo
    eval "$var='$value'"
  fi
}

read_secret DB_PASSWORD "PostgreSQL password for tfregistry user"
read_secret USERNAME    "Admin username (default: admin)"
read_secret PASSWORD    "Admin password"

if [ -z "$USERNAME" ]; then
  USERNAME="admin"
fi

JWT_SECRET=$(openssl rand -hex 32)

DB_URL="postgresql+asyncpg://tfregistry:${DB_PASSWORD}@tf-registry-postgresql.${NAMESPACE}.svc.cluster.local:5432/tfregistry?ssl=require"

# Helper: either apply secret directly or seal it
apply_or_seal() {
  local secret_name="$1"
  local secret_yaml

  secret_yaml=$(cat)

  if [ "$SEALED" = true ]; then
    local output_file="${SEALED_OUTPUT_DIR}/${secret_name}.yaml"
    echo "$secret_yaml" | kubeseal --format yaml > "$output_file"
    echo "Sealed secret written to: $output_file (safe to commit to git)"
  else
    echo "$secret_yaml" | kubectl --context "$CONTEXT" apply -f -
  fi
}

echo ""
echo "Creating secret: postgresql-credentials"
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic postgresql-credentials \
  --from-literal=password="$DB_PASSWORD" \
  --from-literal=postgres-password="$DB_PASSWORD" \
  --dry-run=client -o yaml | apply_or_seal "postgresql-credentials"

echo "Creating secret: tf-registry-api-secrets"
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic tf-registry-api-secrets \
  --from-literal=DATABASE_URL="$DB_URL" \
  --from-literal=USERNAME="$USERNAME" \
  --from-literal=PASSWORD="$PASSWORD" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --dry-run=client -o yaml | apply_or_seal "tf-registry-api-secrets"

echo ""
echo "Creating secret: postgresql-tls (TLS certificates for PostgreSQL SSL)"
CERT_DIR="./pg-certs"
if [ ! -f "$CERT_DIR/server.crt" ]; then
  echo "Generating PostgreSQL TLS certificates..."
  ./scripts/generate-pg-certs.sh "$CERT_DIR"
fi
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic postgresql-tls \
  --from-file=server.crt="$CERT_DIR/server.crt" \
  --from-file=server.key="$CERT_DIR/server.key" \
  --from-file=ca.crt="$CERT_DIR/ca.crt" \
  --dry-run=client -o yaml | apply_or_seal "postgresql-tls"

echo ""
if [ "$SEALED" = true ]; then
  echo "SealedSecret files generated in: $SEALED_OUTPUT_DIR/"
  echo "These files are safe to commit to git."
  echo ""
  echo "To apply them:"
  echo "  kubectl --context $CONTEXT apply -f $SEALED_OUTPUT_DIR/"
else
  echo "Secrets created successfully."
  echo "Next steps:"
  echo "  1. Apply ArgoCD apps: kubectl --context $CONTEXT apply -f helm/application-*.yaml"
  echo "  2. Or deploy directly:"
  echo "     helm upgrade --install tf-registry-postgresql helm/postgresql --kube-context $CONTEXT -n $NAMESPACE"
  echo "     helm upgrade --install tf-registry-api helm/tf-registry-api --kube-context $CONTEXT -n $NAMESPACE"
  echo "     helm upgrade --install tf-registry helm/tf-registry --kube-context $CONTEXT -n $NAMESPACE"
fi
