# Free cloud option: GitHub Actions + GitHub notifications

Deployed and enabled, but inventory access is BLOCKED as of October 13, 2026. The cloud browser starts; Tesla returns HTTP 403 for inventory requests. No complete scan has succeeded and no match alerts have been sent. Scheduled attempts remain enabled, but this is not a working inventory monitor until last_success is verified. No Render service, email provider, custom domain, API key, artifact upload, cache, or paid runner is required. Standard GitHub-hosted runners are free in public repositories under GitHub's current plan rules. The job refuses to run in a private repository and remains disabled until MONITOR_ENABLED=true is set.

The schedule requests checks at minutes 2, 7, 12, and so on. GitHub can delay or drop scheduled runs; this cannot promise a maximum five-minute delay. GitHub can disable schedules after 60 days without repository activity. Check the Actions page and saved last_success to verify monitoring; a successful workflow status is not proof of successful inventory access. Tesla may block cloud runners; activation requires a successful cloud inventory check.

Only newly confirmed vehicle matches create GitHub issues, assigned to and mentioning the repository owner. These are visible in GitHub notifications. Install GitHub Mobile, sign into the same account, and enable mention/assignment push notifications for phone alerts. GitHub notifications may also email according to the user's existing settings. Infrastructure-level workflow failures can independently trigger GitHub's workflow notifications; turn those off for this workflow/account if only match alerts are wanted. The program sends no empty-result, setup, test, error, or recovery notifications.

State is saved on the monitor-state branch using the workflow's temporary repository token. Availability episodes are saved before posting and existing bot-created issues are checked before retries. Only complete successful inventory checks count toward two-scan disappearance/reappearance detection. Keep the state branch and match issues to preserve duplicate prevention. Creating an issue and saving state are separate operations, so receipt lookup reconciles interrupted submissions on the next successful scan.

## Public disclosure requiring approval

The user approved publication. The public repository exposes its source and Git history, the configured ZIP 91766, vehicle preferences, GitHub identity, and future inventory alert/state records. The uploaded files do not include .env.local, monitor state, phone number, recipient email, credentials, or the surrounding Clearo project. Git commit metadata can include the account's configured commit email. Review it before publishing. Do not enable or publish without the user's explicit approval.

## Activation

1. Approve the public repository disclosure after reviewing its contents and history.
2. Upload tracker/github_runner.py, this document, and .github/workflows/inventory.yml.
3. Make the reviewed repository public and set repository Actions variable MONITOR_ENABLED=true.
4. Manually run once and inspect logs/state for a complete inventory check. Do not send a test notification.
5. Verify a scheduled check and configure GitHub Mobile push settings; only then describe it as active. Retire the local Codex monitor after this succeeds.

Sources: https://docs.github.com/en/actions/concepts/billing-and-usage and https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule and https://docs.github.com/en/subscriptions-and-notifications/concepts/about-notifications
