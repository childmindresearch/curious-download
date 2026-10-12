"""Upload data dictionaries into an existing applet.

The backend has no partial edits: `PUT /applets/{id}` replaces every activity, item, and flow.
So the plan starts from the whole applet as the admin Builder loads it, changes only the
activities named in the dictionary, and sends everything else back unchanged (encryption,
theme, flows, other activities, and item settings a dictionary doesn't describe).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .client import CuriousClient
from .dictionary import _merged_order, describe_item_change
from .itembuilder import (
    ActivityRows,
    CellError,
    ItemRow,
    Problem,
    apply_scores,
    build_item,
    check_activity,
    plan_scores,
    resolve_logic,
)

# Roles allowed to save an applet (backend Role.editors()).
EDIT_ROLES = {"owner", "manager", "editor"}
PLAN_COLUMNS = ["activity", "item", "action", "details"]
_COMPARED = (
    "name",
    "responseType",
    "question",
    "responseValues",
    "config",
    "isHidden",
    "conditionalLogic",
    "allowEdit",
)


class StaleAppletError(Exception):
    """The applet changed on the server after the plan was made."""


@dataclass
class ItemChange:
    activity: str
    item: str
    action: str  # added, changed, unchanged, removed, kept, created (activity), renamed (activity), reordered
    details: str = ""


@dataclass
class UploadPlan:
    applet: dict  # as fetched
    body: dict  # what PUT will send
    changes: list[ItemChange] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    new_activities: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity != "error"]

    @property
    def has_changes(self) -> bool:
        return any(c.action != "unchanged" and c.action != "kept" for c in self.changes)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for change in self.changes:
            result[change.action] = result.get(change.action, 0) + 1
        return result


def update_body(applet: dict) -> dict:
    """The PUT body for an applet as fetched: activities get their `key`, flow items point at activity keys."""
    body = copy.deepcopy(applet)
    for activity in body.get("activities") or []:
        activity["key"] = activity.get("id") or activity.get("key") or str(uuid.uuid4())
    for flow in body.get("activityFlows") or []:
        flow["items"] = [
            {"id": item.get("id"), "activityKey": item.get("activityKey") or item.get("activityId")}
            for item in flow.get("items") or []
        ]
    return body


def new_activity(name: str) -> dict:
    """What the admin Builder sends for a new activity (getNewActivity). No id, so the backend makes one."""
    return {
        "key": str(uuid.uuid4()),
        "name": name,
        "description": {"en": ""},
        "splashScreen": "",
        "image": "",
        "showAllAtOnce": False,
        "isSkippable": False,
        "isReviewable": False,
        "responseIsEditable": True,
        "isHidden": False,
        "autoAssign": True,
        "reportIncludedItemName": "",
        "scoresAndReports": {"generateReport": False, "reports": [], "showScoreSummary": False},
        "subscaleSetting": None,
        "items": [],
    }


def _same(old: dict, new: dict) -> bool:
    return all(old.get(key) == new.get(key) for key in _COMPARED)


ExtrasDecision = Callable[[str, list[str]], str]  # (activity name, item names) -> "keep" or "remove"


def _plan_activity(
    plan: UploadPlan, spec: ActivityRows, target: dict, old_items: list[dict], extras: ExtrasDecision
) -> None:
    old_by_id = {item.get("id"): item for item in old_items}
    old_by_name = {item.get("name"): item for item in old_items}
    used: set[str] = set()
    built: list[tuple[dict, ItemRow, dict | None]] = []
    for row in spec.items:
        existing = old_by_id.get(row.cells.get("item_id") or "") or old_by_name.get(row.name)
        if existing is not None and existing.get("id") in used:
            existing = None
        try:
            item, warnings = build_item(row, existing, target)
        except CellError as e:
            plan.problems.append(Problem(spec.name, row.name, str(e), where=row.where))
            continue
        for warning in warnings:
            plan.problems.append(Problem(spec.name, row.name, warning, "warning", row.where))
        if existing is not None:
            used.add(existing.get("id"))
        built.append((item, row, existing))

    leftovers = [item for item in old_items if item.get("id") not in used]
    # Score rows are read against the activity as it is now, before its items change.
    scores = plan_scores(spec.score_rows, target) if spec.score_rows else None
    if scores is not None:
        plan.problems += scores.problems
    missing = [i.get("name") for i in leftovers] + (scores.leftovers if scores else [])
    keep = bool(missing) and extras(target.get("name") or spec.name, missing) == "keep"
    for item in leftovers:
        plan.changes.append(
            ItemChange(spec.name, item.get("name"), "kept" if keep else "removed", "not in the dictionary")
        )

    # Items from the file in file order; kept ones stay after the item they followed before.
    def key(item: dict, position: int) -> str:
        return item.get("id") or f"new-{position}"

    entries = [(key(item, i), item, row) for i, (item, row, _) in enumerate(built)]
    entries += [(item.get("id"), item, None) for item in leftovers if keep]
    old_keys = [item.get("id") for item in old_items if item.get("id") in {k for k, _, _ in entries}]
    order = _merged_order([old_keys, [k for k, _, row in entries if row is not None]])
    entries.sort(key=lambda entry: order[entry[0]])
    items = [item for _, item, _ in entries]
    rows = {index: row for index, (_, _, row) in enumerate(entries) if row is not None}
    plan.problems += resolve_logic(items, rows, old_items, target)
    target["items"] = items

    for item, row, existing in built:
        if existing is None:
            plan.changes.append(ItemChange(spec.name, item.get("name"), "added", item.get("responseType")))
        elif _same(existing, item):
            plan.changes.append(ItemChange(spec.name, item.get("name"), "unchanged"))
        else:
            plan.changes.append(
                ItemChange(spec.name, item.get("name"), "changed", describe_item_change(existing, item))
            )
    if scores is not None:
        for name, action, details in apply_scores(scores, target, keep):
            plan.changes.append(ItemChange(spec.name, name, action, details))
    still_there = [i.get("id") for i in items if i.get("id") in old_by_id]
    if still_there != [i.get("id") for i in old_items if i.get("id") in set(still_there)]:
        plan.changes.append(ItemChange(spec.name, "", "reordered", "items are in a new order"))
    plan.problems += check_activity(target)


def plan_upload(applet: dict, activities: list[ActivityRows], extras: ExtrasDecision) -> UploadPlan:
    """Work out the new applet. Nothing is sent; `extras` decides about items the file doesn't list."""
    body = update_body(applet)
    plan = UploadPlan(applet=applet, body=body)
    by_id = {a.get("id"): a for a in body["activities"] if a.get("id")}
    by_name = {(a.get("name") or "").lower(): a for a in body["activities"]}
    claimed: set[int] = set()
    for spec in activities:
        target = by_id.get(spec.activity_id) or by_name.get(spec.name.lower())
        if target is not None and id(target) in claimed:
            plan.problems.append(Problem(spec.name, "", "two activities in the files match the same applet activity"))
            continue
        if target is None:
            target = new_activity(spec.name)
            body["activities"].append(target)
            plan.new_activities.append(spec.name)
            plan.changes.append(ItemChange(spec.name, "", "created", "new activity"))
            old_items: list[dict] = []
        else:
            old_items = copy.deepcopy(target.get("items") or [])
            if target.get("name") != spec.name:
                taken = {(a.get("name") or "").lower() for a in body["activities"] if a is not target}
                if spec.name.lower() in taken:
                    plan.problems.append(Problem(spec.name, "", "another activity already has this name"))
                plan.changes.append(ItemChange(spec.name, "", "renamed", f"was {target.get('name')}"))
                target["name"] = spec.name
        claimed.add(id(target))
        _plan_activity(plan, spec, target, old_items, extras)
    return plan


def apply_plan(client: CuriousClient, owner_id: str, plan: UploadPlan) -> dict:
    """Save the plan, refusing if the applet changed since it was fetched. Returns the applet as saved."""
    applet_id = plan.applet["id"]
    current = client.applet_full(owner_id, applet_id)
    if current.get("version") != plan.applet.get("version"):
        raise StaleAppletError(
            f"The applet changed on the server (version {plan.applet.get('version')} → {current.get('version')}) "
            "since the changes were prepared. Nothing was saved; run the upload again."
        )
    client.update_applet(applet_id, plan.body)
    return client.applet_full(owner_id, applet_id)


def verify(plan: UploadPlan, after: dict) -> list[str]:
    """Differences between what was sent and what the server now has (item names and types per activity)."""
    saved = {a.get("name"): a for a in after.get("activities") or []}
    problems = []
    for activity in plan.body["activities"]:
        expected = [(i.get("name"), i.get("responseType")) for i in activity.get("items") or []]
        found = saved.get(activity.get("name"))
        actual = [(i.get("name"), i.get("responseType")) for i in (found or {}).get("items") or []]
        if expected != actual:
            problems.append(f"{activity.get('name')}: the saved items differ from the ones sent")
    return problems


def restore_body(backup: dict, current: dict) -> tuple[dict, list[str]]:
    """The PUT body that puts back an applet saved by an upload (applet_before.json), plus warnings."""
    if backup.get("id") != current.get("id"):
        raise ValueError("The backup is of a different applet.")
    if backup.get("encryption") != current.get("encryption"):
        raise ValueError("The applet's encryption changed since the backup, so restoring it could break answers.")
    notes = []
    now = {a.get("id") for a in current.get("activities") or []}
    then = {a.get("id") for a in backup.get("activities") or []}
    for activity in current.get("activities") or []:
        if activity.get("id") not in then:
            notes.append(f"activity {activity.get('name')!r} will be removed, with its schedule")
    for activity in backup.get("activities") or []:
        if activity.get("id") not in now:
            notes.append(f"activity {activity.get('name')!r} comes back, but its old schedule doesn't")
    return update_body(backup), notes
