# GitHub Actions Setup Guide

This guide sets up GitHub Actions + repo protections so that:

- `master` requires pull requests (no direct pushes).
- PRs must pass the bot's `validate` mode, or they can't merge.
- PRs that pass validate are automatically merged into `master`.
- pushes to `master` run the bot in `apply` mode.
- `apply` is skipped automatically if required Discord secrets aren't configured.

---

## 0) Set variables for this session

At the start of your terminal session, set these once:

```bash
export OWNER="YOUR_OWNER"
export REPO="YOUR_REPO"
```

Example:

```bash
export OWNER="Gate-Builders"
export REPO="Rules"
```

All commands below assume these environment variables are set.

---

## 1) Authenticate GitHub CLI

```bash
gh auth login
gh auth status
```

---

## 2) Allow workflows to auto-merge PRs

The auto-merge workflow uses `GITHUB_TOKEN` to merge PRs. Set workflow token permissions to `write`:

```bash
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${OWNER}/${REPO}/actions/permissions/workflow" \
  --input - <<'JSON'
{
  "default_workflow_permissions": "write",
  "can_approve_pull_request_reviews": false
}
JSON
```

---

## 3) Protect the `master` branch (require PR + require validate check)

This enables branch protection so:

* PRs are required
* status check `Discord Rules Validate / validate` must pass
* no approving reviews required (approval count = 0)
* admins are enforced too

```bash
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${OWNER}/${REPO}/branches/master/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Discord Rules Validate / validate"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null
}
JSON
```

### If the status check name differs

The context string must match exactly what GitHub shows in PR checks.

To confirm the name:

1. Open a PR
2. Go to the **Checks** tab
3. Copy the check name (example: `Discord Rules Validate / validate`)

Then re-run the protection command with the correct value.

---

## 5) Add Discord secrets (to enable `apply` on pushes to master)

The apply workflow runs only if these three repo secrets exist and are non-empty:

* `DISCORD_BOT_TOKEN`
* `DISCORD_GUILD`
* `DISCORD_GUILD_RULES_CHANNEL`

### Set secrets interactively (recommended)

```bash
gh secret set DISCORD_BOT_TOKEN --repo "${OWNER}/${REPO}"
gh secret set DISCORD_GUILD --repo "${OWNER}/${REPO}"
gh secret set DISCORD_GUILD_RULES_CHANNEL --repo "${OWNER}/${REPO}"
```

### Set numeric secrets non-interactively

```bash
gh secret set DISCORD_GUILD --repo "${OWNER}/${REPO}" --body "1334193811366346752"
gh secret set DISCORD_GUILD_RULES_CHANNEL --repo "${OWNER}/${REPO}" --body "1449370764926128200"
```

For the token, prefer stdin:

```bash
# Put the token in a file token.txt first, then:
gh secret set DISCORD_BOT_TOKEN --repo "${OWNER}/${REPO}" < token.txt
```

Verify secret names are present:

```bash
gh secret list --repo "${OWNER}/${REPO}"
```

> GitHub will not show secret values back to you.

---

## 5) What to expect from the workflows

### Pull requests to `master`

* `Discord Rules Validate` runs automatically
* If validate fails, the PR cannot merge (branch protection blocks it)
* If validate succeeds, `Discord Rules Auto-merge` merges it (squash merge)

> For safety, the auto-merge workflow typically skips fork PRs.

### Pushes to `master`

* `Discord Rules Apply` runs
* It gates on secrets:

  * if missing → logs "Skipping apply" and exits successfully
  * if present → installs dependencies and runs the bot in `apply` mode

---

## 7) Quick test checklist

1. Create a branch and change a `Rules.<n>.*.md` file
2. Open a PR into `master`
3. Confirm validate runs and passes
4. Confirm the PR merges automatically
5. Confirm apply runs (or is skipped if secrets aren't configured)

---

## Sensible repository configuration

Use branch protection for master. This blocks direct writes to master while still allowing collaborators to push to their own branches and open PRs (also set enforce_admins: true so admins can't bypass).

```bash
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${OWNER}/${REPO}/branches/master/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Discord Rules Validate / validate"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null
}
JSON
```

## Troubleshooting

### Apply didn't run

* Check secrets exist:

  ```bash
  gh secret list --repo "${OWNER}/${REPO}"
  ```
* Check apply workflow logs for "Discord secrets are missing. Skipping apply".

### Auto-merge didn't merge

Could be:

* PR is a draft
* PR is from a fork (often skipped)
