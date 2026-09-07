"""Independent, bounded evidence review before runtime memory updates."""
from __future__ import annotations

import json
import re
import time

from .budget import check_deadline
from .files import digest, dumps, emit, redact
from .models import LessonReview


def learn_checked(memory, state, brain, lesson, allowed_ids, *, context=None):
    """Fail closed; save a review receipt before applying the existing deduplication."""
    ids = set(lesson["evidence_ids"])
    facts = memory.selected_facts(state["id"], ids & set(allowed_ids))
    feedback = {"key": lesson["key"], "accepted": False}
    if not ids or ids != {f["id"] for f in facts} or any(f["outcome"] != lesson["outcome"] for f in facts):
        memory.reject_lesson(lesson, state["id"], {**feedback, "reason": "missing_unseen_or_mismatched_evidence"})
        return None
    normalized = lambda text: " ".join(text.split())
    claim_hash = digest({"body": normalized(lesson["body"][:2500]),
                         "procedure": [normalized(x) for x in lesson["procedure"][:12]]})
    key = re.sub(r"[^\w.-]+", "-", lesson["key"].strip().lower())[:100]
    existing = [memory.db.execute("SELECT detail FROM confirmations WHERE lesson=? AND identity=?",
                (key, state["id"]+":"+f["identity"])).fetchone() for f in facts]
    if all(row and json.loads(row[0]).get("claim_hash") == claim_hash for row in existing):
        memory.reject_lesson(lesson, state["id"], {**feedback, "reason": "evidence_already_counted_for_this_lesson"})
        return None
    proposal = {"lesson": lesson, "evidence": facts, "context": context or {}}
    review_id = digest({"version": 1, "run": state["id"], **proposal})
    row = memory.db.execute("SELECT result FROM learning_reviews WHERE id=? AND run=?",
                            (review_id, state["id"])).fetchone()
    old_deadline = getattr(brain, "deadline_at", None)
    if row:
        review = json.loads(row[0])
    else:
        try:
            check_deadline(old_deadline)
            brain.deadline_at = min(old_deadline, time.time()+60) if old_deadline is not None else time.time()+60
            review = LessonReview.model_validate(brain.verify_lesson(redact(proposal))).model_dump()
            usage = dict(brain.usage)
            state["llm_seconds"] = state.get("llm_seconds", 0)+usage.get("seconds", 0)
            emit("brain_usage", local_id=state["id"], phase="verify_lesson", **usage)
            if review["sufficient"] and (not review["evidence_ids"] or not set(review["evidence_ids"]) <= ids):
                raise ValueError("Reviewer cited missing or unrelated evidence")
            with memory.db:
                memory.db.execute("INSERT OR IGNORE INTO learning_reviews VALUES (?,?,?)",
                                  (review_id, state["id"], dumps(redact(review))))
            memory.event(state["id"], "lesson_review", {"review_id": review_id, "proposal": proposal, "review": review})
        except Exception as error:
            memory.reject_lesson(lesson, state["id"], {**feedback, "reason": "verification_unavailable", "detail": str(error)[:1500]})
            memory.event(state["id"], "lesson_review_error", memory.learning_feedback)
            return None
        finally:
            brain.deadline_at = old_deadline
    if not review["sufficient"]:
        memory.reject_lesson(lesson, state["id"], {**feedback, "reason": "insufficient_evidence", "review": review, "review_id": review_id})
        return None
    # Only facts explicitly endorsed by the reviewer can count as confirmations.
    checked = {**lesson, "evidence_ids": review["evidence_ids"]}
    key = memory.learn(checked, state["id"], allowed_ids, review={"id": review_id, "result": review})
    memory.learning_feedback.update(review_id=review_id, review=review)
    return key
