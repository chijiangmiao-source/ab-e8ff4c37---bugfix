"""Saga engine unit tests: ordering, compensation, idempotency, recovery."""
from __future__ import annotations

import pytest

from app.models import SwitchRequest, ValveSpec
from app.saga import PayloadConflict, ValveConflict

from conftest import make_request


def test_happy_path_completes(engine):
    eng, store, devices, _ = engine
    status = eng.submit(make_request("op-ok", n=4))[0]

    assert status.phase == "COMPLETED"
    assert status.success and status.terminal
    assert {v.valve_id: v.current_opening for v in status.valves} == {
        f"V{i:02d}": 60 + i for i in range(1, 5)
    }
    # Intent + one durable action row per valve persisted.
    rec = store.get("op-ok")
    assert rec is not None and len(rec.actions) == 4
    assert all(a.forward_status == "SUCCESS" for a in rec.actions)


def test_forward_failure_triggers_reverse_compensation(engine):
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V03"], compensate=[])

    status = eng.submit(make_request("op-fail", n=4))[0]

    assert status.phase == "COMPENSATED"
    assert not status.success
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V01"].forward == "SUCCESS"
    assert by_id["V02"].forward == "SUCCESS"
    assert by_id["V03"].forward == "FAILED"
    assert by_id["V04"].forward == "SKIPPED"
    # The two changed valves restored to their original openings.
    assert by_id["V01"].current_opening == 1   # initial was 0+1
    assert by_id["V02"].current_opening == 2
    # V03/V04 never moved.
    assert by_id["V03"].current_opening == 3
    assert by_id["V04"].current_opening == 4

    # Compensation happened in REVERSE success order: V02 then V01.
    comp = [a for a in devices.executed_actions("op-fail")
            if a.phase == "COMPENSATE"]
    assert [a.valve_id for a in comp] == ["V02", "V01"]
    assert all(a.opening == {"V01": 1, "V02": 2}[a.valve_id] for a in comp)


def test_idempotent_replay_returns_same_result_and_does_not_re_act(engine):
    eng, store, devices, _ = engine
    req = make_request("op-replay", n=3)
    first = eng.submit(req)[0]
    actions_before = devices.executed_actions("op-replay")
    openings_before = {v.valve_id: v.opening for v in devices.list_valves()}

    second, created = eng.submit(req)

    assert created is False
    assert second.phase == first.phase == "COMPLETED"
    assert devices.executed_actions("op-replay") == actions_before
    assert {v.valve_id: v.opening for v in devices.list_valves()} == openings_before


def test_same_id_different_payload_conflicts_and_touches_nothing(engine):
    eng, store, devices, _ = engine
    eng.submit(make_request("op-x", n=3, target=60))
    actions_before = devices.executed_actions()

    changed = SwitchRequest(
        operation_id="op-x",
        valves=[
            ValveSpec(valve_id=f"V{i:02d}", initial_opening=i, target_opening=90)
            for i in range(1, 4)
        ],
    )
    with pytest.raises(PayloadConflict):
        eng.submit(changed)

    # No new device activity whatsoever.
    assert devices.executed_actions() == actions_before
    # Original result still intact.
    assert eng.status("op-x").phase == "COMPLETED"


def test_compensation_failure_leaves_resumable_state(engine):
    eng, store, devices, _ = engine
    # V02 must be restored (reverse order: V02 first) but its restore fails.
    devices.set_failures(forward=["V03"], compensate=["V02"])

    status = eng.submit(make_request("op-cf", n=4))[0]

    assert status.phase == "COMPENSATION_FAILED"
    assert status.terminal and status.resumable
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V02"].compensate == "FAILED"
    assert by_id["V01"].compensate == "PENDING"  # not reached, still to restore
    assert by_id["V02"].current_opening == 62    # still sitting at target
    assert "network failure" in (by_id["V02"].error or "")

    # Operator clears the network fault; resume continues from V02, then V01.
    devices.set_failures(forward=["V03"], compensate=[])
    resumed = eng.resume("op-cf")
    assert resumed.phase == "COMPENSATED"
    by_id = {v.valve_id: v for v in resumed.valves}
    assert by_id["V02"].current_opening == 2
    assert by_id["V01"].current_opening == 1
    assert by_id["V02"].compensate == "SUCCESS"
    assert by_id["V01"].compensate == "SUCCESS"


def test_device_commit_before_receipt_is_recognized_on_resume(engine):
    """The exact restart gap: device already committed FORWARD for V02,
    but the application crashed before storing the receipt. Recovery must
    recognize it via (op, phase) dedupe, not move the valve again."""
    eng, store, devices, _ = engine
    req = make_request("op-gap", n=3)

    # Build durable intent; V01 done+receipted; then V02 commits on the
    # device and the console dies before the receipt lands.
    from app.store import ActionRecord
    rec = store.create_intent(
        "op-gap",
        {"payload_key": "[]"},
        [ActionRecord(valve_id=v.valve_id, idx=i,
                      initial_opening=v.initial_opening,
                      target_opening=v.target_opening)
         for i, v in enumerate(req.valves)],
    )
    devices.execute("op-gap", "V01", claimed_initial=1, new_opening=61,
                    phase="FORWARD")
    a01 = rec.actions[0]
    a01.forward_status = "SUCCESS"
    a01.forward_opening = 61
    store.mark_forward("op-gap", a01)
    # V02 commits on the device ... and no receipt is written.
    devices.execute("op-gap", "V02", claimed_initial=2, new_opening=62,
                    phase="FORWARD")

    # Fresh engine instance, same durable stores == console restart.
    restarted = type(eng)(store, devices)
    status = restarted.resume("op-gap")

    assert status.phase == "COMPLETED"
    persisted = {a.valve_id: a for a in store.get("op-gap").actions}
    assert persisted["V02"].forward_status == "SUCCESS"
    assert persisted["V02"].forward_deduped is True  # recognized, not re-driven
    # Exactly one FORWARD action per valve on the device.
    forward = [a for a in devices.executed_actions("op-gap")
               if a.phase == "FORWARD"]
    assert [(a.valve_id, a.opening) for a in forward] == [
        ("V01", 61), ("V02", 62), ("V03", 63)
    ]


def test_device_rejects_are_persisted_and_safe_to_retry(engine):
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V02"], compensate=[])
    status = eng.submit(make_request("op-rej", n=2))[0]
    assert status.phase == "COMPENSATED"
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V02"].forward == "FAILED"
    assert "rejected" in by_id["V02"].error

    # While still broken, replay must not claim success.
    again, _ = eng.submit(make_request("op-rej", n=2))
    assert again.phase == "COMPENSATED"


@pytest.mark.parametrize("n", [1, 9])
def test_valve_count_bounds(engine, n):
    eng, _, _, _ = engine
    with pytest.raises(ValueError):
        eng.submit(make_request("op-bounds", n=n))


def _compensation_failed_switch(engine, op="op-guard"):
    """Drive a switch into COMPENSATION_FAILED with V01/V02 still at their
    targets (V03 rejected forward, V02's restore hit a network failure)."""
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V03"], compensate=["V02"])
    status = eng.submit(make_request(op, n=4))[0]
    assert status.phase == "COMPENSATION_FAILED"
    return eng, store, devices


def test_unfinished_compensation_blocks_shared_valves_and_touches_nothing(engine):
    eng, store, devices = _compensation_failed_switch(engine)
    actions_before = devices.executed_actions()
    openings_before = {v.valve_id: v.opening for v in devices.list_valves()}

    # New operation id, current field openings registered, other targets.
    follow_up = SwitchRequest(
        operation_id="op-follow",
        valves=[
            ValveSpec(valve_id="V01", initial_opening=61, target_opening=20),
            ValveSpec(valve_id="V02", initial_opening=62, target_opening=30),
        ],
    )
    with pytest.raises(ValveConflict) as excinfo:
        eng.submit(follow_up)

    assert excinfo.value.conflicts == {"op-guard": ["V01", "V02"]}
    # Nothing was registered and no device was touched.
    assert store.get("op-follow") is None
    assert devices.executed_actions() == actions_before
    assert {v.valve_id: v.opening for v in devices.list_valves()} == openings_before


def test_unfinished_compensation_still_allows_unshared_valves(engine):
    eng, store, devices = _compensation_failed_switch(engine)

    other = SwitchRequest(
        operation_id="op-other",
        valves=[
            ValveSpec(valve_id="V07", initial_opening=7, target_opening=77),
            ValveSpec(valve_id="V08", initial_opening=8, target_opening=88),
        ],
    )
    status, created = eng.submit(other)
    assert created and status.phase == "COMPLETED"
    assert {v.valve_id: v.opening for v in devices.list_valves()
            if v.valve_id in ("V07", "V08")} == {"V07": 77, "V08": 88}


def test_valve_rejected_by_forward_is_not_protected(engine):
    """V03 never moved (its FORWARD was rejected), so a new switch may take
    it even while V01/V02 remain protected."""
    eng, store, devices = _compensation_failed_switch(engine)

    devices.set_failures(forward=[], compensate=["V02"])
    req = SwitchRequest(
        operation_id="op-v03",
        valves=[
            ValveSpec(valve_id="V03", initial_opening=3, target_opening=33),
            ValveSpec(valve_id="V04", initial_opening=4, target_opening=44),
        ],
    )
    status, _ = eng.submit(req)
    assert status.phase == "COMPLETED"


def test_blocked_switch_submits_once_compensation_finishes(engine):
    eng, store, devices = _compensation_failed_switch(engine)
    follow_up = SwitchRequest(
        operation_id="op-follow",
        valves=[
            ValveSpec(valve_id="V01", initial_opening=61, target_opening=20),
            ValveSpec(valve_id="V02", initial_opening=62, target_opening=30),
        ],
    )
    with pytest.raises(ValveConflict):
        eng.submit(follow_up)

    # Fault cleared; the old switch finishes its rollback ...
    devices.set_failures(forward=["V03"], compensate=[])
    assert eng.resume("op-guard").phase == "COMPENSATED"

    # ... and now the previously blocked switch is accepted and completes.
    status, created = eng.submit(follow_up)
    assert created and status.phase == "COMPLETED"
    assert {v.valve_id: v.current_opening for v in status.valves} == {
        "V01": 20, "V02": 30,
    }
    cur = {v.valve_id: v.opening for v in devices.list_valves()}
    assert cur["V01"] == 20 and cur["V02"] == 30


def test_valve_protection_survives_engine_restart(engine):
    eng, store, devices = _compensation_failed_switch(engine)

    # Fresh engine over the same durable stores == service restart.
    restarted = type(eng)(store, devices)
    assert restarted.protected_valves() == {"op-guard": ["V01", "V02"]}

    follow_up = SwitchRequest(
        operation_id="op-follow",
        valves=[
            ValveSpec(valve_id="V01", initial_opening=61, target_opening=20),
            ValveSpec(valve_id="V02", initial_opening=62, target_opening=30),
        ],
    )
    with pytest.raises(ValveConflict):
        restarted.submit(follow_up)
    assert store.get("op-follow") is None
