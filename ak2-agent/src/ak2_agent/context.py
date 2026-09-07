"""Compact structural diagnostics for arbitrary world observations."""
from __future__ import annotations

import re
import json

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:")
REFERENCE = "__ak2_context_reference__"


def wire_size(value):
    # Conservative even when the SDK escapes Unicode on the wire.
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


class ContextView:
    """Bounded views of one immutable call input; pointers never access files."""
    def __init__(self, value):
        self.value = value

    def descriptor(self, value, pointer):
        return {"context_pointer": pointer, "type": type(value).__name__,
                "length": len(value) if isinstance(value, (dict, list, str)) else None,
                "complete": False}

    def decision_preview(self):
        """Allocate by runtime role, never by a world's domain or equal field shares."""
        budgets = {"knowledge": 110000, "observation": 90000, "evidence": 45000,
                   "recalled_memory": 45000, "recent_events": 30000}
        result = {}
        for key, value in self.value.items():
            pointer = "/" + key.replace("~", "~0").replace("/", "~1")
            budget = min(budgets.get(key, 15000), max(1000, 360000-wire_size(result)))
            if key == "knowledge" and isinstance(value, dict):
                files = {}
                # Keep tool instructions whole whenever they fit; references are optional.
                for name, body in sorted(value.items(), key=lambda x: x[0].startswith("references/")):
                    path = pointer + "/" + name.replace("~", "~0").replace("/", "~1")
                    left = max(300, budget-wire_size(files)-500)
                    files[name] = (self.descriptor(body, path) if name.startswith("references/")
                                   else self.preview(body, path, left))
                result[key] = files if wire_size(files) <= budget else self.preview(value, pointer, budget)
            else:
                result[key] = self.preview(value, pointer, budget)
        return result if wire_size(result) <= 380000 else self.preview(budget=90000)

    def preview(self, value=None, pointer="", budget=90000):
        if value is None and pointer == "":
            value = self.value
        if wire_size(value) <= budget:
            return value
        result = self.descriptor(value, pointer)
        if isinstance(value, (dict, list)):
            items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
            shown = {}
            for key, child in items[:30]:
                path = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
                allowance = max(200, min(12000, (budget - 1000) // max(1, min(30, len(items)))))
                shown[str(key)] = self.preview(child, path, allowance)
                if wire_size({**result, "preview": shown}) > budget:
                    shown.pop(str(key))
                    break
            result["preview"] = shown
        elif isinstance(value, str):
            result["preview"] = value[:max(0, min(500, (budget-500)//12))]
        return result

    def read(self, pointer, offset=0):
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise ValueError("Expected JSON Pointer into the current context")
        if type(offset) is not int or offset < 0:
            raise ValueError("Context offset must be a nonnegative integer")
        value = self.value
        for token in pointer.split("/")[1:] if pointer else []:
            if re.search(r"~(?![01])", token):
                raise ValueError("Invalid JSON Pointer escape")
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise ValueError("Invalid array index")
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise ValueError("Cannot traverse scalar context data")
        if isinstance(value, str):
            content = value[offset:offset+16000]
            end = offset + len(content)
        elif isinstance(value, (dict, list)):
            items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
            content = []
            for key, child in items[offset:offset+10]:
                path = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
                content.append({"key": key, "value": self.preview(child, path, 4000)})
            end = offset + len(content)
        else:
            content, end = value, 0
        length = len(value) if isinstance(value, (dict, list, str)) else 0
        return {"pointer": pointer, "offset": offset, "content": content,
                "next_offset": end if end < length else None, "length": length}


def deduplicate_context(value, minimum=512):
    """Lossless references to earlier, exactly identical JSON values."""
    def reserved(node):
        if isinstance(node, dict):
            return REFERENCE in node or any(reserved(v) for v in node.values())
        return isinstance(node, list) and any(reserved(v) for v in node)
    if reserved(value):
        return value  # Never interpret a world-supplied marker as our own reference.
    seen = {}

    def visit(node, path):
        if isinstance(node, (dict, list, str)):
            canonical = json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(canonical) >= minimum:
                if canonical in seen:
                    return {REFERENCE: seen[canonical]}
                seen[canonical] = path
        if isinstance(node, dict):
            return {k: visit(v, path + "/" + k.replace("~", "~0").replace("/", "~1")) for k, v in node.items()}
        if isinstance(node, list):
            return [visit(v, path + "/" + str(i)) for i, v in enumerate(node)]
        return node
    return visit(value, "")


def context_history(events):
    """Keep prior measurements and action receipts without repeating full snapshots."""
    result = []
    for event in events:
        if event["kind"] != "observation":
            result.append(event)
            continue
        observation = event["data"]
        result.append({**event, "data": {
            **{key: observation[key] for key in
               ("metrics", "done", "success", "pending", "error", "delivery", "external_id")
               if key in observation},
            "outline": observation_outline(observation),
            "snapshot_omitted": True}})
    return result


def observation_outline(value):
    collections, clocks = [], []

    def visit(node, path, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                child = path + "." + key
                if isinstance(item, str) and DATE.match(item):
                    clocks.append({"path": child, "value": item})
                elif isinstance(item, list):
                    record = {"path": child, "count": len(item)}
                    dates = {}
                    for row in item:
                        if isinstance(row, dict):
                            for field, text in row.items():
                                if isinstance(text, str) and DATE.match(text):
                                    dates.setdefault(field, []).append(text)
                    if dates:
                        record["date_ranges"] = {field: {"first": min(xs), "last": max(xs)} for field, xs in dates.items()}
                    # Preserve the source's own paging metadata without assigning domain semantics.
                    continuation = {k: v for k, v in node.items() if isinstance(v, (str, int, bool)) and
                                    any(part in k.lower() for part in ("cursor", "has_more", "page", "limit", "total"))}
                    if continuation:
                        record["pagination"] = continuation
                    collections.append(record)
                else:
                    visit(item, child, depth+1)
    visit(value, "observation")
    return {"date_fields": clocks[:40], "collections": collections[:40]}
