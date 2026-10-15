"""Run `curious-app` against the fake study server, to try the Dashboard tab without a real Curious.

    .venv/bin/python -m tests.demo_server            # then open http://127.0.0.1:8457/?token=demo
    .venv/bin/python -m tests.demo_server --port 9000 --token secret

Log in with researcher@example.org / account-pw. The workspace "Lab" has two applets: "EMA Study",
a scheduled daily/weekly study with an individual schedule and a flow, and "Intake", a one-shot
applet whose schedule the account may not read. Nothing leaves this computer.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import httpx
import uvicorn

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.webapp.server import create_app

from . import fake_dashboard

BANNER = (
    f"Demo with fake data — nothing here is real. Log in with {fake_dashboard.EMAIL} and the password "
    f"{fake_dashboard.PASSWORD}; the server address is ignored."
)


class DemoServer(fake_dashboard.FakeStudyServer):
    """The fake study server, but a wrong login says it is the demo instead of mimicking Curious."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if request.url.path == "/auth/login" and response.status_code == 401:
            body = json.loads(request.content) if request.content else {}
            message = (
                f"This is the demo with fake data; your Curious login does not work here ({body.get('email', '')!s}). "
                f"Use {fake_dashboard.EMAIL} / {fake_dashboard.PASSWORD}, or run `curious-app` for the real server."
            )
            return httpx.Response(401, json={"result": [{"message": message}]})
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8457)
    parser.add_argument("--token", default="demo")
    args = parser.parse_args()
    # Keep the demo away from the real settings and saved studies.
    cli.SETTINGS_PATH = Path(tempfile.mkdtemp(prefix="curious-demo-")) / "settings.json"
    fake = DemoServer()
    app = create_app(
        token=args.token, client_factory=lambda url: CuriousClient(url, transport=fake.transport()), banner=BANNER
    )
    print(f"\nDemo Curious app with fake data:  http://127.0.0.1:{args.port}/?token={args.token}")
    print(f"Log in with {fake_dashboard.EMAIL} / {fake_dashboard.PASSWORD}; any server address works.\n", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
