#!/usr/bin/env python3
"""HTTP acceptance: compensation-failure protection across a service restart.

Drives the full reported chain against a compose-managed (restartable) web
service:

  1. an old switch stalls in COMPENSATION_FAILED (V03 rejects the forward
     action, V02's restoration hits a network failure);
  2. a new operation id naming the already-changed valves -> explicit 409,
     field openings untouched, no record left behind;
  3. a switch over disjoint valves still completes;
  4. the compensation fault is cleared and the service is restarted via the
     test hook (the container's restart policy brings it back on the same
     data volume);
  5. startup recovery finishes the old operation's reverse rollback;
  6. the previously blocked request now submits and completes, with target
     openings, phase and the device action log all consistent.

Exits 0 on success, 1 on any failure.
"""
from __future__ import annotations

import sys
import time

import httpx


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise SystemExit(1)


def wait_healthy(base_url: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def openings(api: httpx.Client) -> dict:
    return {v["valve_id"]: v["opening"]
            for v in api.get("/api/devices/valves").json()}


def main(base_url: str) -> int:
    print(f"restart-chain acceptance against {base_url}")
    old = {
        "operation_id": "chain-old",
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, 5)
        ],
    }
    blocked = {
        "operation_id": "chain-blocked",
        "valves": [
            {"valve_id": "V01", "initial_opening": 51, "target_opening": 71},
            {"valve_id": "V02", "initial_opening": 52, "target_opening": 72},
        ],
    }
    free = {
        "operation_id": "chain-free",
        "valves": [
            {"valve_id": "V07", "initial_opening": 7, "target_opening": 57},
            {"valve_id": "V08", "initial_opening": 8, "target_opening": 58},
        ],
    }

    with httpx.Client(base_url=base_url, timeout=10) as api:
        check("reset", api.post("/api/test/reset").status_code == 200)
        check("inject forward+compensate failures",
              api.post("/api/test/failures",
                       json={"forward": ["V03"],
                             "compensate": ["V02"]}).status_code == 200)

        r = api.post("/api/switches", json=old)
        check("old switch accepted", r.status_code == 201, r.text)
        check("old switch stuck in COMPENSATION_FAILED",
              r.json()["phase"] == "COMPENSATION_FAILED", r.json()["phase"])

        r = api.post("/api/switches", json=blocked)
        check("shared-valve switch -> 409 conflict",
              r.status_code == 409, f"{r.status_code} {r.text}")
        check("conflict names the unfinished operation",
              "chain-old" in r.json().get("detail", ""), r.text)
        cur = openings(api)
        check("conflict did not change field openings",
              cur.get("V01") == 51 and cur.get("V02") == 52, str(cur))
        check("blocked operation left no record",
              api.get("/api/switches/chain-blocked").status_code == 404)
        check("no device actions logged for blocked operation",
              api.get("/api/devices/executed-actions",
                      params={"operation_id": "chain-blocked"}).json() == [])

        r = api.post("/api/switches", json=free)
        check("disjoint switch still completes",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED",
              f"{r.status_code} {r.text}")

        # Fault cleared BEFORE the restart (persists in the device db).
        check("clear the compensation fault",
              api.post("/api/test/failures",
                       json={"forward": [],
                             "compensate": []}).status_code == 200)

        # Restart the service: the hook exits the process moments after
        # replying; the container restart policy brings it back.
        try:
            api.post("/api/test/restart")
        except httpx.HTTPError:
            pass  # response lost to the hard exit; the restart still counts

    time.sleep(1.5)  # let the old process actually exit
    check("service healthy again after restart", wait_healthy(base_url))

    with httpx.Client(base_url=base_url, timeout=10) as api:
        s = api.get("/api/switches/chain-old").json()
        check("old switch COMPENSATED after restart",
              s["phase"] == "COMPENSATED", s["phase"])
        cur = openings(api)
        check("old switch restored the initial openings",
              cur.get("V01") == 1 and cur.get("V02") == 2, str(cur))

        r = api.post("/api/switches", json=blocked)
        check("previously blocked switch -> 201",
              r.status_code == 201, f"{r.status_code} {r.text}")
        s = r.json()
        check("previously blocked switch COMPLETED",
              s["phase"] == "COMPLETED", s["phase"])
        check("previously blocked switch reached its targets",
              [v["current_opening"] for v in s["valves"]] == [71, 72],
              str([v["current_opening"] for v in s["valves"]]))

        acts = api.get("/api/devices/executed-actions",
                       params={"operation_id": "chain-blocked"}).json()
        check("device log of the blocked switch matches its targets",
              [(a["valve_id"], a["phase"], a["opening"]) for a in acts]
              == [("V01", "FORWARD", 71), ("V02", "FORWARD", 72)], str(acts))

        acts_old = api.get("/api/devices/executed-actions",
                           params={"operation_id": "chain-old"}).json()
        comp = [(a["valve_id"], a["opening"])
                for a in acts_old if a["phase"] == "COMPENSATE"]
        check("old rollback was reverse-order, once each",
              comp == [("V02", 2), ("V01", 1)], str(comp))

    print("restart-chain acceptance: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    sys.exit(main(base.rstrip("/")))
