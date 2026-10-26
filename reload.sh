#!/bin/bash
# garmin-connector v1 -- make Lambda start fresh containers so they re-read the
# secret (needed after you change the GitHub values; a new Garmin key is picked
# up within 10 minutes on its own). Then checks /healthz.
#   Usage:  ./reload.sh [region]
set -euo pipefail

STACK="garmin-connector"
REGION="${1:-us-east-1}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

FN=$(out FunctionName)
HOME_URL=$(out HomepageUrl)

aws lambda update-function-configuration --region "$REGION" --function-name "$FN" \
  --description "garmin-connector v1 (reloaded $(date -u +%Y-%m-%dT%H:%M:%SZ))" > /dev/null
aws lambda wait function-updated --region "$REGION" --function-name "$FN"

echo "Health check:"
curl -fsS "${HOME_URL}/healthz"; echo
