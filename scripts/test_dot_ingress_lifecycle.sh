#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/home"
swiftc -parse-as-library "$root/Sources/DBond/DotIngressController.swift" \
  "$root/scripts/fixtures/dot_ingress/main.swift" -o "$scratch/check-ingress"
HOME="$scratch/home" CFFIXED_USER_HOME="$scratch/home" DBOND_INGRESS_FIXTURE_DIR="$scratch" "$scratch/check-ingress"
