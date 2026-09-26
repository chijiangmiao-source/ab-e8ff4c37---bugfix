"""End-to-end crash/restart tests using a real uvicorn process.

The server process is killed with ``os._exit(77)`` at the precise instant
*after* the simulated device committed an action but *before* the
application stored its receipt (controlled by ``CRASH_AFTER``). A brand new
process is then started against the same data directory and must recognize
the executed action through the device dedupe log instead of moving the
valve twice.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.device import CRASH_EXIT_CODE

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"server never became healthy: {last}")


@contextmanager
def server(data_dir: Path, port: int, crash_after: str | None = None):
    env = os.environ.copy()
    env["VALVE_DB_DIR"] = str(data_dir)
    env["VALVE_ALLOW_RESET"] = "1"
    if crash_after:
        env["CRASH_AFTER"] = crash_after
    log = open(data_dir / f"server-{port}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if crash_after:
            # The process is expected to kill itself; wait for exit instead
            # of ordinary readiness.
            yield proc
        else:
            _wait_healthy(port)
            yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()


def _payload(op: str, n: int = 4) -> dict:
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, n + 1)
        ],
    }


def _wait_dead(proc: subprocess.Popen, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.1)
    proc.kill()
    raise AssertionError("crashed server did not exit")


def test_crash_after_forward_device_commit_recognized_on_restart(tmp_path):
    port = _free_port()
    body = _payload("op-crash-forward")

    # ---- first process: crash right after V02 FORWARD commits on device
    with server(tmp_path, port, crash_after="op-crash-forward:V02:FORWARD") as proc:
        _wait_healthy(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            # Submit; the server hard-exits before replying.
            with pytest.raises(httpx.HTTPError):
                api.post("/api/switches", json=body)
        assert _wait_dead(proc) == CRASH_EXIT_CODE

    # ---- console restarts: no crash marker, same durable data
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            mid = api.get("/api/switches/op-crash-forward")
            assert mid.status_code == 200
            s0 = mid.json()
            assert s0["phase"] == "EXECUTING"  # explicit, explained half-state
            by_id = {v["valve_id"]: v for v in s0["valves"]}
            assert by_id["V01"]["forward"] == "SUCCESS"
            assert by_id["V02"]["forward"] == "PENDING"  # receipt was lost

            # Re-submitting the SAME operation id resumes the switch.
            r = api.post("/api/switches", json=body)
            assert r.status_code == 200
            s = r.json()
            assert s["phase"] == "COMPLETED"
            by_id = {v["valve_id"]: v for v in s["valves"]}
            # V02 was recognized on the device, not driven a second time.
            assert by_id["V02"]["forward"] == "SUCCESS"

            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-crash-forward"},
            ).json()
            forward = [a for a in acts if a["phase"] == "FORWARD"]
            # Exactly one FORWARD action per valve — no double movement.
            assert [(a["valve_id"], a["opening"]) for a in forward] == [
                ("V01", 51), ("V02", 52), ("V03", 53), ("V04", 54)
            ]
            final = {v["valve_id"]: v["opening"]
                     for v in api.get("/api/devices/valves").json()}
            assert final == {"V01": 51, "V02": 52, "V03": 53, "V04": 54}


def test_crash_during_compensation_resumes_reverse_rollback(tmp_path):
    port = _free_port()
    body = _payload("op-crash-comp")

    with server(tmp_path, port,
                crash_after="op-crash-comp:V02:COMPENSATE") as proc:
        _wait_healthy(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": []})
            with pytest.raises(httpx.HTTPError):
                api.post("/api/switches", json=body)
        assert _wait_dead(proc) == CRASH_EXIT_CODE

    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            s0 = api.get("/api/switches/op-crash-comp").json()
            assert s0["phase"] == "COMPENSATING"

            # Same-id replay continues the reverse rollback: V02's device
            # commit is recognized (deduped), then V01 is restored.
            s = api.post("/api/switches", json=body).json()
            assert s["phase"] == "COMPENSATED"
            assert [v["current_opening"] for v in s["valves"]] == [1, 2, 3, 4]

            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-crash-comp"},
            ).json()
            comp = [(a["valve_id"], a["opening"])
                    for a in acts if a["phase"] == "COMPENSATE"]
            assert comp == [("V02", 2), ("V01", 1)]  # reverse order, once each


def _follow_up_payload() -> dict:
    """New operation id on the valves the old switch still owns, with the
    field openings registered as they stand after the old forward pass."""
    return {
        "operation_id": "op-follow-up",
        "valves": [
            {"valve_id": "V01", "initial_opening": 51, "target_opening": 20},
            {"valve_id": "V02", "initial_opening": 52, "target_opening": 30},
        ],
    }


def _valve_openings(api: httpx.Client) -> dict:
    return {v["valve_id"]: v["opening"]
            for v in api.get("/api/devices/valves").json()}


def test_unfinished_compensation_blocks_shared_valves_across_restart(tmp_path):
    """Compensation failure -> shared-valve submit conflicts (also after a
    restart) -> fault cleared -> restart -> old switch compensated -> the
    previously blocked switch submits and completes."""
    port = _free_port()
    old_body = _payload("op-old")
    follow_up = _follow_up_payload()

    # ---- first process: drive op-old into COMPENSATION_FAILED
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": ["V02"]})
            s = api.post("/api/switches", json=old_body).json()
            assert s["phase"] == "COMPENSATION_FAILED"
            by_id = {v["valve_id"]: v for v in s["valves"]}
            assert by_id["V02"]["compensate"] == "FAILED"
            assert by_id["V01"]["compensate"] == "PENDING"
            # V01/V02 still sit at op-old's targets.
            assert _valve_openings(api) == {
                "V01": 51, "V02": 52, "V03": 3,
            }

            # New operation id on the owned valves: explicit conflict, and
            # the field is not touched.
            r = api.post("/api/switches", json=follow_up)
            assert r.status_code == 409
            detail = r.json()["detail"]
            assert detail["error"] == "valve_conflict"
            assert detail["conflicts"] == [
                {"operation_id": "op-old", "valves": ["V01", "V02"]}
            ]
            assert _valve_openings(api) == {
                "V01": 51, "V02": 52, "V03": 3,
            }
            assert api.get("/api/switches/op-follow-up").status_code == 404

    # ---- restart with the fault still injected: protection survives
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            s0 = api.get("/api/switches/op-old").json()
            assert s0["phase"] == "COMPENSATION_FAILED"
            r = api.post("/api/switches", json=follow_up)
            assert r.status_code == 409
            assert _valve_openings(api) == {
                "V01": 51, "V02": 52, "V03": 3,
            }
            assert api.get("/api/switches/op-follow-up").status_code == 404

            # Fault cleared; the next boot's startup recovery will finish
            # the old switch's rollback.
            api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": []})

    # ---- restart after the fix: op-old is compensated, op-follow-up runs
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            s = api.get("/api/switches/op-old").json()
            assert s["phase"] == "COMPENSATED"
            assert [v["current_opening"] for v in s["valves"]] == [1, 2, 3, 4]
            assert _valve_openings(api) == {
                "V01": 1, "V02": 2, "V03": 3,
            }

            # The previously blocked switch is now accepted and completes.
            r = api.post("/api/switches", json=follow_up)
            assert r.status_code == 201, r.text
            s = r.json()
            assert s["phase"] == "COMPLETED"
            assert [v["current_opening"] for v in s["valves"]] == [20, 30]
            assert _valve_openings(api) == {
                "V01": 20, "V02": 30, "V03": 3,
            }

            # Device actions line up with the reported phases: op-old moved
            # each valve once forward and restored V02/V01 in reverse order;
            # op-follow-up moved V01/V02 exactly once.
            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-old"},
            ).json()
            comp = [(a["valve_id"], a["opening"])
                    for a in acts if a["phase"] == "COMPENSATE"]
            assert comp == [("V02", 2), ("V01", 1)]
            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-follow-up"},
            ).json()
            assert [(a["valve_id"], a["phase"], a["opening"])
                    for a in acts] == [("V01", "FORWARD", 20),
                                       ("V02", "FORWARD", 30)]
