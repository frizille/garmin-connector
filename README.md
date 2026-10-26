# garmin-connector

A private connector that lets Claude read your Garmin data on the web, on your phone, and in the desktop app. It runs in your own AWS account on Garmin's official [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api), and only your GitHub account can sign in. It can read your data but can't change anything in Garmin Connect.

```
Claude (web / phone / desktop)
        │  OAuth 2.1 (GitHub sign-in) + MCP over HTTPS
        ▼
API Gateway (HTTP API, throttled)
        ▼
Lambda (Python 3.13, arm64) ──► Garmin Personal API
   │                │
   ▼                ▼
Secrets Manager   DynamoDB
(keys)            (sign-in sessions, encrypted)
```

It reuses the 23 read-only tools from [garmin-personal-mcp](https://pypi.org/project/garmin-personal-mcp/): activities with splits, daily summaries, sleep, HRV, heart rate, stress, Body Battery, weight, and HR zones. This repo adds what a hosted connector needs: GitHub sign-in, a one-account allowlist, and secrets kept in AWS.

Setup takes about 20 minutes, all in a browser. Running it costs about $0.40 a month.

## What you need

- A Garmin Connect account and watch
- An AWS account
- A GitHub account (for signing in)
- A Claude plan that supports custom connectors

## 1. Create a Garmin Personal API key

In Garmin Connect on the web, open **Settings → Personal API** and create a key. Pick an expiration (up to a year). Garmin shows the key once, so keep the tab open until step 4. Don't paste it anywhere else.

## 2. Deploy

1. Sign in to the AWS console, pick a region (the scripts default to `us-east-1`), and open **CloudShell** from the top bar.
2. Clone and deploy:

   ```bash
   git clone https://github.com/frizille/garmin-connector.git
   cd garmin-connector
   ./deploy.sh <your-github-username>
   ```

   For another region, run `./deploy.sh <your-github-username> "" <region>`.

The script builds the Lambda package, looks up your numeric GitHub ID, deploys the stack, and prints a table of outputs. Keep it on screen.

## 3. Create the GitHub sign-in app

1. Go to **github.com/settings/developers → OAuth Apps → New OAuth App**.
2. Fill in:
   - **Homepage URL:** the `HomepageUrl` output
   - **Authorization callback URL:** the `GitHubCallbackUrl` output
3. Register it, then **Generate a new client secret**. Copy the Client ID and the secret.

## 4. Fill in the secret

1. In the AWS console, open **Secrets Manager → garmin-connector/app → Retrieve secret value → Edit**.
2. Replace the three `REPLACE_ME` values: `garmin_api_key`, `github_client_id`, and `github_client_secret`.
3. Leave `master_key` alone and save.

Typing the values into the console keeps them out of your shell history.

## 5. Reload and check

```bash
./reload.sh
```

It should end with `{"status":"ok","version":"garmin-connector v1"}`. If it says `not_configured`, a value in the secret still reads `REPLACE_ME`.

## 6. Add it to Claude

1. In claude.ai, open **Settings → Connectors → Add custom connector**.
2. Paste the `ConnectorUrl` output (it ends in `/mcp`). Leave the OAuth client fields empty.
3. Choose **Connect**, approve the consent screen, and sign in with GitHub.

Connectors belong to your Claude account, so it also appears in the Claude mobile app. To test it, ask: "What runs did I do this week?"

## Security

- **One account.** Every MCP request re-checks the GitHub account by username *and* numeric ID, so a renamed and re-registered username can't get in. Anyone else gets a 401.
- **Least privilege.** GitHub sign-in asks only for `read:user`. The Lambda's IAM role can read only its own secret and table.
- **No secrets in code or config.** The Garmin key and GitHub credentials live only in Secrets Manager. CloudFormation generates a master key, and the app derives separate keys from it for token signing and storage encryption.
- **Encrypted sign-in records** in DynamoDB, on top of DynamoDB's own encryption at rest.
- **Fails closed.** If the secret can't be read or still holds placeholders, every request gets a generic 503.
- **No Garmin data is stored.** Each request goes to Garmin and back.
- **Throttled.** API Gateway caps traffic at 10 requests a second, so nobody can run up your bill.
- **Kill switch.** Revoke the app in GitHub (Settings → Applications) and access stops on the next request.

## Upkeep

- **Garmin key expiring:** Garmin emails a week ahead. Create a new key and paste it into the secret. It takes effect within 10 minutes, or immediately after Garmin rejects the old one.
- **GitHub app changed:** update the secret, then run `./reload.sh`.
- **Logs:** CloudWatch, in the log group starting with `garmin-connector-ConnectorLogs`. Kept 30 days.
- **Remove everything:** `sam delete --stack-name garmin-connector`, then delete the GitHub OAuth app and the Garmin key.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 503, or Claude says the connector isn't configured | A value in the secret still reads `REPLACE_ME`, or you skipped `./reload.sh` |
| GitHub sign-in works, then every request fails with 401 | You signed in with a GitHub account other than the one you deployed with |
| "Garmin rejected the Personal API key" | The key expired or was deleted; create a new one and update the secret |
| `{"no_data": ...}` | Garmin has nothing for that date yet, such as today's HRV before you've slept |

## Limitations

- **Read-only.** The Personal API can't push workouts to your watch.
- **No Garmin analytics.** Training status, training readiness, VO2 max, and race predictions aren't in the Personal API. Claude works from the raw data.
- **HRV needs a supported watch** (the original Forerunner 945 doesn't record it).
- **One person per deployment.** A Personal API key reaches only its owner's data.

## Repo layout

| Path | What it is |
|---|---|
| `template.yaml` | AWS SAM template: API Gateway, Lambda, Secrets Manager, DynamoDB, logs |
| `src/app.py` | The server: GitHub OAuth, allowlist, Secrets Manager, Garmin tools |
| `src/requirements.txt` | Pinned dependencies |
| `build.sh` | Builds the Lambda package (Linux arm64 wheels; no Docker needed) |
| `deploy.sh` / `reload.sh` | Deploy or update the stack; restart after a secret change |
| `tests/` | Local tests; no AWS, GitHub, or Garmin account needed |

Run the tests:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r src/requirements.txt pytest httpx
pytest -q tests
```

## License

MIT. Not affiliated with or endorsed by Garmin. Garmin and Garmin Connect are trademarks of Garmin Ltd.
