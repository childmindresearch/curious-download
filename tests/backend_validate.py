"""Run by test_backend_schema.py with a Python that has the backend's dependencies.

Reads a JSON list of PUT /applets/{id} bodies on stdin and validates each with the backend's own
AppletUpdate model (field types, item configs, skip logic, scores, subscales). Prints one line per body.
"""

import json
import sys

sys.path.insert(0, sys.argv[1])  # the backend's src folder
from apps.applets.domain.applet_create_update import AppletUpdate

for index, body in enumerate(json.load(sys.stdin)):
    try:
        AppletUpdate.model_validate(body)
    except Exception as e:  # noqa: BLE001 - report every kind of rejection
        print(f"{index} FAIL {type(e).__name__}: {e}")
    else:
        print(f"{index} OK")
