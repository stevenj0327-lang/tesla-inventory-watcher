import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .inventory import BrowserInventory, InventoryError
from .state import State, idempotency_key


def send_email(event):
    if not event["vin"]:
        raise RuntimeError("Only vehicle-match emails are allowed")
    payload = {"from": os.environ["MAIL_FROM"], "to": [os.environ["ALERT_TO"]],
               "subject": event["subject"], "text": event["body"]}
    request = Request("https://api.resend.com/emails", data=json.dumps(payload).encode(),
                      headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
                               "Content-Type": "application/json",
                               "Idempotency-Key": idempotency_key(event["id"])})
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError("Email provider HTTP %s; check sender/account settings" % error.code) from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError("Email provider response unavailable; retry uses the same idempotency key") from None
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("Email provider did not return a message ID")
    return result["id"]


def flush(state, include_matches):
    for event in state.pending(include_matches):
        if not state.attempt(event["id"]):
            logging.error("Email retry window expired; manual review required")
            continue
        try:
            receipt = send_email(event)
            state.delivered(event["id"], receipt)
            logging.info("Email accepted by provider: %s", receipt)
        except Exception as error:
            state.mail_failed(event["id"], str(error))
            logging.error("Email not confirmed: %s", error)
            break


def cycle(state, source_factory=BrowserInventory, dry_run=False):
    ok = False
    try:
        with source_factory() as source:
            matches = source.scan()
        if dry_run:
            print(json.dumps({"status": "checked", "matches": list(matches.values())}, indent=2))
            return True
        state.success(matches)
        ok = True
        logging.info("Complete inventory scan: %s matching vehicles", len(matches))
    except InventoryError as error:
        if not dry_run:
            state.failure(str(error))
        logging.error("Inventory unknown: %s", error)
    except Exception as error:
        # Do not dump raw browser responses or environment values in cloud logs.
        reason = "Inventory check failed (%s)" % type(error).__name__
        if not dry_run:
            state.failure(reason)
        logging.error(reason)
    if not dry_run:
        flush(state, include_matches=ok)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Always-on Tesla inventory email watcher")
    parser.add_argument("--once", action="store_true", help="Run one complete scan")
    parser.add_argument("--dry-run", action="store_true", help="Read inventory without writing state or emailing")
    parser.add_argument("--status", action="store_true", help="Print check and mail health, without private settings")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.dry_run:
        if args.status:
            parser.error("--dry-run cannot be combined with --status")
        raise SystemExit(0 if cycle(None, dry_run=True) else 1)
    directory = Path(os.environ.get("DATA_DIR", "./data"))
    directory.mkdir(parents=True, exist_ok=True)
    # A persistent-disk lock prevents concurrent workers duplicating events.
    lock = open(directory / "worker.lock", "a")
    if not args.status:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = State(directory / "tracker.sqlite3")
    if args.status:
        print(json.dumps(state.summary(), indent=2))
        return
    if not args.dry_run:
        missing = [name for name in ("RESEND_API_KEY", "MAIL_FROM", "ALERT_TO") if not os.environ.get(name)]
        if missing:
            parser.error("Configure these secret environment settings before activation: " + ", ".join(missing))
    interval = int(os.environ.get("POLL_SECONDS", "120"))
    if not 60 <= interval <= 300:
        parser.error("POLL_SECONDS must be between 60 and 300")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        started = time.monotonic()
        ok = cycle(state, dry_run=args.dry_run)
        if args.once or args.dry_run:
            raise SystemExit(0 if ok else 1)
        elapsed = time.monotonic() - started
        if elapsed > interval:
            logging.warning("Scan exceeded requested polling interval (%.0fs)", elapsed)
        # Failures back off, explicitly sacrificing latency rather than hammering a blocked site.
        delay = interval if ok else min(900, interval * 2 ** min(state.get("failures", 1) - 1, 3))
        stop.wait(max(5, delay - elapsed))


if __name__ == "__main__":
    main()
