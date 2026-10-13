# Tesla inventory email tracker

A standalone cloud worker that checks Tesla inventory every two minutes and emails new matching vehicles. Once deployed, it runs independently of the computer used to create it.

**Current status: code and local tests complete; cloud deployment and real email delivery are not activated or verified.** There is no deployment URL or running cloud worker yet.

## Search rules

| Vehicle | Configuration |
| --- | --- |
| Model Y | Premium Rear-Wheel Drive; black or grey paint; 19-inch Crossflow wheels; black interior; five seats |
| Cybertruck | Premium All-Wheel Drive; 20-inch Core wheels; standard dark interior; no paid options |
| Both | New, non-demo inventory; no optional charges except requested Model Y paint; no price ceiling |
| Area | Union of Tesla's 200-mile search around registration ZIP 91766 and All Deliverable to 91766 |

The code checks exact trim names and equipment prices, not just a model-name keyword. It requires every listed option to have known pricing. Missing data produces an error instead of a false match. Other trims, demo vehicles, upgraded wheels, white Model Y interiors, seven seats, and paid add-ons are excluded. Names/default equipment can change; this strict policy needs reviewing if Tesla changes the configuration catalog. These assumptions should be compared against a current US matching listing before activation.

## How it works

1. Open Tesla's public inventory in an ordinary headless browser.
2. Read the website's same-origin inventory JSON for four model/location combinations. Queries have no price filters. All pages must be read successfully before a scan is considered complete.
3. Validate configurations and compare identifiers with SQLite history on a persistent disk.
4. Queue one email per availability episode. Notify on the first scan's matches and vehicles returning after two successful absent scans. Unchanged inventory stays quiet.
5. Send through Resend with a persistent idempotency key. A crash or short mail-provider outage can retry without intentionally duplicating a message. Provider acceptance is recorded separately from actual inbox delivery.

This is change detection by polling, not a Tesla push subscription. No supported public inventory-update webhook was established during research. Two minutes is the normal polling interval, not a maximum alert delay: inventory freshness, scan time, network failures, and email delivery add latency. Blocked requests back off up to 15 minutes. Errors and recovery are recorded only in logs/state. Only confirmed vehicle matches generate emails; there are no empty-result, outage, recovery, or setup emails. If the email provider itself fails, the problem appears in cloud logs/state and cannot be reported through that broken provider.

The worker never purchases or reserves a vehicle and does not log into a Tesla account. It does not bypass CAPTCHAs or access blocks, use stealth modifications, or rotate proxies. A blocked cloud IP requires an allowed data source or hosting arrangement, not treating the block as empty stock.

## Deployment package

The included Dockerfile uses Python and Playwright. `render.yaml` defines one persistent background worker in Oregon with the `1c-2g` compute plan and a 1 GB disk. It exposes no public web endpoint. Automatic deploys are disabled. Hosting is paid; confirm the actual monthly amount in Render before creating the service. Nothing in this package purchases hosting.

Render is a deployment option, not an established working host for Tesla: Chromium's sandbox and Tesla access must pass the cloud smoke test. If the host cannot support the sandbox, use a compatible Docker/VPS host. Do not silently disable it. A continuously running container with a persistent `/data` volume can also run this package elsewhere.

Configure these settings in the cloud host's environment/secret settings:

| Setting | Value |
| --- | --- |
| `ALERT_TO` | The recipient chosen in this chat; recorded locally in the ignored `.env.local` file |
| `MAIL_FROM` | A sender authorized by the Resend account |
| `RESEND_API_KEY` | A sending key, entered directly in the host's secret settings |
| `POLL_SECONDS` | `120` |
| `DATA_DIR` | `/data` |

Do not use a Gmail address as `MAIL_FROM` unless your provider explicitly authorizes it. Resend generally requires an authorized sender/domain; its onboarding sender has recipient restrictions. Do not paste API keys into chat, commit them, or include them in a deployment archive. `.env.local` is deliberately excluded from the archive. The app reads environment variables; it does not automatically load `.env` files.

### Activate

1. Sign into a cloud host and an email-provider account. This package uses Render and Resend; neither is connected to the code yet.
2. Put this directory's contents in a private Git repository. Import `render.yaml` as a Render Blueprint, review the billing quote, and enter the three mail settings as secrets. Do not import the surrounding Clearo project or synced `sources/` files.
3. In the deployed service environment, run a read-only inventory smoke test:

   ```sh
   python -m tracker.worker --dry-run
   ```

   Expect a JSON result with `status: checked`. Zero matches is valid only if all four API responses succeeded. A blocked browser, schema error, or HTTP 403 is a failed smoke test. Confirm a real candidate's options with Tesla before relying on match notifications.
4. Validate the authorized sender and API access in Resend without sending a setup email. The first actual email will be for a matching vehicle. Confirm that message in provider delivery logs when a match occurs; provider acceptance alone is not proof of inbox delivery.
5. Run `python -m tracker.worker` as the one continuous service process. The Docker entrypoint does this automatically. Verify at least two successful checks approximately two minutes apart, then verify a restart preserves history. Avoid running multiple independent copies with separate disks.
6. Only after cloud checks and email configuration are verified, disable the local Codex five-minute automation to avoid duplicate systems. It was not changed by this build.

Only match alerts are authorized. Do not send test emails to the user. Cloud worker startup requires mail configuration and fails explicitly when it is missing. Render may start the worker immediately after deployment; do not call it operational until the checks above succeed.

### Local verification (no credentials, no network)

```sh
python3 -m unittest discover -s tests -v
```

Tests use clearly synthetic vehicles and fake email responses. They cover accepted/rejected equipment, incomplete data, pagination, duplicates across restarts, confirmed reappearance, outage/recovery, stale alerts, retry timing, and idempotency. They do not prove Tesla cloud access or actual email delivery.

### Operation

```sh
python -m tracker.worker --status
```

Status includes last attempt, last complete success, matching count, and inventory/mail errors. It omits recipient addresses and credentials. Cloud logs record successful scans and failed checks. Watch `last_success`, not only whether the process is running. Back up the persistent disk; loss of history can cause fresh alerts for old vehicles. Stop the cloud service to stop alerts. If an email has uncertain acceptance for more than 23 hours, it moves to `needs_review` rather than being resent after the provider's 24-hour idempotency window.

## Validation and known limits

- Tesla's website returned HTTP 200 inventory JSON during browser inspection on October 13, 2026. The observed payload contained no exact results. The browser's automatically applied price range was present, so this inspection is not a complete availability baseline.
- A separate direct HTTP request returned 403. Headless access from the eventual cloud host remains unverified.
- The empty-response shape was observed live. Nonempty equipment fields were checked against a public developer example and tested with synthetic US configurations; no real matching US listing was available for full configuration validation during this build.
- Docker is not installed in the current execution environment, so the container was not built or executed here. The Python logic tests run independently of Docker.
- An email-provider key, authorized sender, and a cloud-host account/deployment are still required. No email or SMS has been sent by this code.
- No strict five-minute freshness or delivery guarantee is possible with this implementation.

## References

- [Tesla Model Y inventory](https://www.tesla.com/inventory/new/my?zip=91766&range=200)
- [Tesla Cybertruck inventory](https://www.tesla.com/inventory/new/ct?zip=91766&range=200)
- [Public inventory response example](https://gist.github.com/myhalici/2241ae06ca4f8069290cd63c3b114542)
- [Render background workers](https://render.com/docs/background-workers)
- [Render compute plans](https://render.com/docs/compute-plans)
- [Render persistent disks](https://render.com/docs/disks)
- [Render pricing](https://render.com/pricing)
- [Playwright Python Docker guidance](https://playwright.dev/python/docs/docker)
- [Resend email API and idempotency](https://resend.com/docs/api-reference/emails/send-email)
