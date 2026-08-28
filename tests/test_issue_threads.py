"""Multi-issue session invariants.

A session holds several independent problems, and exactly one of them is the
one the customer is talking about right now. Getting that wrong is not a
cosmetic bug: the agent keeps answering fluently, on the wrong machine, and
every downstream fact — retrieval scope, warranty verdict, part number — is
scoped to the thread it picked.
"""

from __future__ import annotations

import uuid

import pytest

from services.api.app.agent import state
from services.api.app.db import execute, query_one
from services.api.app.tools import catalog


@pytest.fixture()
def session_with_two_threads():
    """Two open threads on two different machines, oldest touched first."""
    session_id = str(uuid.uuid4())
    execute(
        "INSERT INTO sessions (id, customer_id, status) VALUES (%s, NULL, 'active')",
        (session_id,),
    )
    first = state.create_issue(
        session_id, title="velodrome grinding",
        symptom_summary="grinding noise from the flywheel",
        model_id="FF-BB-VELODROME-300-S")
    second = state.create_issue(
        session_id, title="circuit screen blank",
        symptom_summary="screen keeps going blank",
        model_id="FF-BB-PELOTON-X-100-PRO")

    # Thread 2 was worked most recently, exactly as it would be after the
    # customer raised it mid-diagnosis and answered a question on it.
    state.save_issue(second)

    yield session_id, first, second

    execute("DELETE FROM issue_threads WHERE session_id = %s", (session_id,))
    execute("DELETE FROM sessions WHERE id = %s", (session_id,))


def test_active_thread_is_the_most_recently_worked(session_with_two_threads):
    session_id, _first, second = session_with_two_threads
    assert state.load_active_issue_id(session_id) == second.id


def test_switching_back_survives_the_next_turn(session_with_two_threads):
    """The regression this file exists for.

    `switch_issue` used to return the new active_issue_id in graph state and
    stop there. Graph state does not outlive the turn: the next customer
    message re-derives the active thread from the database, found thread 2 more
    recently updated, and answered on the wrong machine — one turn after
    agreeing to go back to thread 1.
    """
    session_id, first, _second = session_with_two_threads

    state.touch_issue(first.id)

    assert state.load_active_issue_id(session_id) == first.id, (
        "resuming a thread must make it the active one for the turn that follows"
    )


def test_terminal_threads_are_never_resumed_as_active(session_with_two_threads):
    session_id, first, second = session_with_two_threads
    second.status = "resolved"
    state.save_issue(second)
    assert state.load_active_issue_id(session_id) == first.id


# ---------------------------------------------------------------------------
# Part selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("asked", "expected"), [
    ("can I just order a new pedal set for the Velodrome", "Pedal Set"),
    ("grinding noise from the flywheel", "Flywheel Bearing Set"),
    ("the drive belt is slipping under load", "Drive Belt"),
])
def test_a_named_part_beats_a_long_sentence(asked, expected):
    """Trigram similarity against a whole sentence collapses as the sentence
    grows, so a customer naming the exact part used to score near zero and the
    thread's symptom won instead — which is how you ship someone a display they
    never asked for."""
    parts = catalog.find_parts_for_symptom(
        model_id="FF-BB-VELODROME-300-S", symptom=asked)
    assert parts, f"no part matched {asked!r}"
    assert parts[0]["name"] == expected


def test_part_matching_never_crosses_models():
    parts = catalog.find_parts_for_symptom(
        model_id="FF-BB-VELODROME-300-S", symptom="pedal set")
    assert parts
    for p in parts:
        assert p["part_number"].startswith("FF-BB-VELODROME-300-S-")
