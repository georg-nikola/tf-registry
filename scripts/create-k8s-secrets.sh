#!/bin/bash
# Creates Kubernetes secrets for tf-registry-api and postgresql.
# Run this once before deploying. Values are NOT stored in git.
#
# Usage:
#   ./scripts/create-k8s-secrets.sh
#
# Required inputs (will prompt if not set as env vars):
#   DB_PASSWORD  - PostgreSQL password for tfregistry user
#   USERNAME     - Admin username for the registry UI
#   PASSWORD     - Admin password for the registry UI

set -e

CONTEXT="admin@home-talos-k8s-cluster"
NAMESPACE="default"

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

DB_URL="postgresql+asyncpg://tfregistry:${DB_PASSWORD}@tf-registry-postgresql.${NAMESPACE}.svc.cluster.local:5432/tfregistry"

echo ""
echo "Creating secret: postgresql-credentials"
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic postgresql-credentials \
  --from-literal=password="$DB_PASSWORD" \
  --from-literal=postgres-password="$DB_PASSWORD" \
  --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

echo "Creating secret: tf-registry-api-secrets"
kubectl --context "$CONTEXT" -n "$NAMESPACE" create secret generic tf-registry-api-secrets \
  --from-literal=DATABASE_URL="$DB_URL" \
  --from-literal=USERNAME="$USERNAME" \
  --from-literal=PASSWORD="$PASSWORD" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --dry-run=client -o yaml | kubectl --context "$CONTEXT" apply -f -

echo ""
echo "Secrets created successfully."
echo "Next steps:"
echo "  1. Apply ArgoCD apps: kubectl --context $CONTEXT apply -f helm/application-*.yaml"
echo "  2. Or deploy directly:"
echo "     helm upgrade --install tf-registry-postgresql helm/postgresql --kube-context $CONTEXT -n $NAMESPACE"
echo "     helm upgrade --install tf-registry-api helm/tf-registry-api --kube-context $CONTEXT -n $NAMESPACE"
echo "     helm upgrade --install tf-registry helm/tf-registry --kube-context $CONTEXT -n $NAMESPACE"
