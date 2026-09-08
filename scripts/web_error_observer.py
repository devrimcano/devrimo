"""Forward bounded frontend infrastructure failures from journald to PostHog.

Runs independently of Next.js and its build files. Never forwards raw journal
messages, request content, cookies, or arbitrary exception values.
"""

import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen


def classify(message):
    if "client reference manifest" in message and "does not exist" in message:
        return "missing_client_manifest", "Next.js client reference manifest is missing"
    if "Failed to load static file for page" in message and "ENOENT" in message:
        return "missing_error_page", "Next.js static error page is missing"
    if "Could not find a production build" in message:
        return "missing_production_build", "Next.js production build is missing"
    if "Main process exited" in message or "Failed to start" in message:
        return "frontend_process_failed", "Frontend service process failed"
    return None


def payload(record, category, description, key):
    timestamp = datetime.fromtimestamp(int(record["__REALTIME_TIMESTAMP"]) / 1_000_000, UTC).isoformat()
    return {
        "api_key": key,
        "event": "$exception",
        "distinct_id": "service:devrimo-web",
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, record["__CURSOR"])),
        "timestamp": timestamp,
        "properties": {
            "$exception_list": [{"type": "FrontendInfrastructureError", "value": description}],
            "$exception_fingerprint": category,
            "$process_person_profile": False,
            "service": "devrimo-web-journal",
            "environment": "production",
            "source": "systemd_journal",
            "error_category": category,
        },
    }


def send(event, host):
    if not host.startswith("https://"):
        raise RuntimeError("PostHog ingestion must use HTTPS")
    request = Request(host.rstrip("/") + "/capture/", data=json.dumps(event).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read())
        if result.get("status") not in (1, "Ok", "ok"):
            raise RuntimeError(f"PostHog ingestion status: {str(result.get('status'))[:40]}")


def main():
    key = os.environ["POSTHOG_API_KEY"]
    host = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com")
    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/devrimo-web-observer")) / "cursor"
    command = ["journalctl", "-u", "devrimo-web.service", "--follow", "--output=json", "--no-pager"]
    command += ["--after-cursor", state.read_text().strip()] if state.exists() else ["--since", "-1h"]
    recent = {}
    journal = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    try:
        for line in journal.stdout:
            record = json.loads(line)
            failure = classify(record.get("MESSAGE", ""))
            if failure:
                category, description = failure
                now = time.monotonic()
                if now - recent.get(category, -60) >= 60:
                    # A failed send leaves this cursor unacknowledged. Systemd
                    # restarts and replays it; deterministic UUID deduplicates it.
                    send(payload(record, category, description, key), host)
                    recent[category] = now
                    print(json.dumps({"event": "frontend_service_error_forwarded", "category": category}), flush=True)
            temporary = state.with_suffix(".tmp")
            temporary.write_text(record["__CURSOR"])
            temporary.replace(state)
        raise RuntimeError("Frontend journal stream ended")
    finally:
        journal.terminate()
        try:
            journal.wait(timeout=5)
        except subprocess.TimeoutExpired:
            journal.kill()
            journal.wait()


if __name__ == "__main__":
    main()
