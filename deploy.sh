#!/bin/bash
# garmin-connector v1 -- build (if needed) and deploy or update the stack.
# Run from AWS CloudShell in this folder.
#
#   Usage:  ./deploy.sh <github-login> [github-id] [region]
#
# github-id is looked up automatically; pass it only if GitHub rate-limits the lookup
# (it's the "id" field at https://api.github.com/users/<login>).
set -euo pipefail

cd "$(dirname "$0")"

LOGIN="${1:?Usage: ./deploy.sh <github-login> [github-id] [region]}"
ID="${2:-}"
REGION="${3:-us-east-1}"
STACK="garmin-connector"

# Pin the allowlist to the account's numeric ID, which survives a login rename.
if [ -z "$ID" ]; then
  ID=$(curl -fsS "https://api.github.com/users/${LOGIN}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
fi
echo "Allowlisting GitHub account ${LOGIN} (id ${ID}) in ${REGION}"

[ -d package ] || ./build.sh
# Lambda Web Adapter executes run.sh directly; it must keep its exec bit.
chmod +x package/run.sh

sam deploy \
  --template-file template.yaml \
  --stack-name "$STACK" \
  --region "$REGION" \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides "AllowedGitHubLogin=${LOGIN}" "AllowedGitHubId=${ID}"

aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
