"""Run as root with backend/.venv/bin/python to provision the journal observer."""

import json
import os
from pathlib import Path

from dotenv import dotenv_values


def main():
    frontend = dotenv_values("/opt/devrimo/frontend/.env.local")
    key = frontend.get("NEXT_PUBLIC_POSTHOG_KEY")
    host = frontend.get("NEXT_PUBLIC_POSTHOG_HOST") or "https://eu.i.posthog.com"
    if not key or not host.startswith("https://"):
        raise RuntimeError("Frontend PostHog ingestion is not configured")
    path = Path("/etc/devrimo/web-observer.env")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as file:
        for name, value in {"POSTHOG_API_KEY": key, "POSTHOG_HOST": host}.items():
            if "\n" in value or "\r" in value:
                raise RuntimeError("Invalid multiline ingestion setting")
            file.write(f"{name}={json.dumps(value)}\n")
    temporary.replace(path)
    print("Observer environment provisioned with ingestion credentials only.")


if __name__ == "__main__":
    main()
