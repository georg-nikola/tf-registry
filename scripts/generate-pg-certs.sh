#!/bin/bash
# Generates self-signed TLS certificates for PostgreSQL SSL.
# The generated certs are placed in a local directory and used by
# create-k8s-secrets.sh to create the postgresql-tls Kubernetes secret.
#
# Usage:
#   ./scripts/generate-pg-certs.sh [output-dir]
#
# Default output directory: ./pg-certs
# Generated files: ca.crt, ca.key, server.crt, server.key

set -euo pipefail

CERT_DIR="${1:-./pg-certs}"
DAYS_VALID=3650  # 10 years for a self-signed cert
CN="postgresql.default.svc.cluster.local"

if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
    echo "Certificates already exist in $CERT_DIR — skipping generation."
    echo "Delete $CERT_DIR to regenerate."
    exit 0
fi

mkdir -p "$CERT_DIR"

echo "==> Generating CA key and certificate..."
openssl genrsa -out "$CERT_DIR/ca.key" 4096
openssl req -new -x509 -days "$DAYS_VALID" \
    -key "$CERT_DIR/ca.key" \
    -out "$CERT_DIR/ca.crt" \
    -subj "/CN=PostgreSQL CA/O=tf-registry"

echo "==> Generating server key and CSR..."
openssl genrsa -out "$CERT_DIR/server.key" 4096
openssl req -new \
    -key "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.csr" \
    -subj "/CN=$CN"

echo "==> Signing server certificate with CA..."
openssl x509 -req -days "$DAYS_VALID" \
    -in "$CERT_DIR/server.csr" \
    -CA "$CERT_DIR/ca.crt" \
    -CAkey "$CERT_DIR/ca.key" \
    -CAcreateserial \
    -out "$CERT_DIR/server.crt"

# PostgreSQL requires the key file to be mode 600 and owned by the postgres
# user (uid 999) inside the container. The mode is set here for local use;
# the Kubernetes Secret + defaultMode handles the in-cluster permission.
chmod 600 "$CERT_DIR/server.key" "$CERT_DIR/ca.key"
chmod 644 "$CERT_DIR/server.crt" "$CERT_DIR/ca.crt"

# Clean up intermediate files
rm -f "$CERT_DIR/server.csr" "$CERT_DIR/ca.srl"

echo ""
echo "Certificates generated in $CERT_DIR:"
ls -la "$CERT_DIR"
echo ""
echo "Next: run ./scripts/create-k8s-secrets.sh to create the K8s TLS secret."
