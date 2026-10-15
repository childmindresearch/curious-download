"""Run `curious-app` against the fake study server, to try the Dashboard tab without a real Curious.

    .venv/bin/python -m tests.demo_server            # then open http://127.0.0.1:8457/?token=demo
    .venv/bin/python -m tests.demo_server --port 9000 --token secret

Log in with researcher@example.org / account-pw. The workspace "Lab" has two applets: "EMA Study",
a scheduled daily/weekly study with an individual schedule and a flow, and "Intake", a one-shot
applet whose schedule the account may not read. Nothing leaves this computer.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.webapp.server import create_app

from . import fake_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8457)
    parser.add_argument("--token", default="demo")
    args = parser.parse_args()
    # Keep the demo away from the real settings and saved studies.
    cli.SETTINGS_PATH = Path(tempfile.mkdtemp(prefix="curious-demo-")) / "settings.json"
    fake = fake_dashboard.FakeStudyServer()
    app = create_app(token=args.token, client_factory=lambda url: CuriousClient(url, transport=fake.transport()))
    print(f"\nDemo Curious app with fake data:  http://127.0.0.1:{args.port}/?token={args.token}")
    print(f"Log in with {fake_dashboard.EMAIL} / {fake_dashboard.PASSWORD}; any server address works.\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
