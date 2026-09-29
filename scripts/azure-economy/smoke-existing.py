"""Verify the live API after resizing, using isolated demo runs only."""

import argparse
import json
import time
import uuid
from datetime import datetime
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-demo", action="store_true", required=True,
                        help="Consent to create two small test events in the public demo.")
    parser.parse_args()
    base = "https://eventharbor.irfanburakozer.com/api/v1"

    def request(path, *, method="GET", body=None, key=None):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        operation = Request(base + path, method=method, headers=headers,
                            data=None if body is None else json.dumps(body).encode())
        with urlopen(operation, timeout=30) as response:
            return json.load(response)

    endpoints = request("/endpoints?limit=100")["items"]
    endpoint = next((item for item in endpoints if item["enabled"] and
                     item["url"] == "http://eventharbor-receiver-prod/webhooks/orders"), None)
    if endpoint is None:
        raise RuntimeError("Expected existing demo receiver is missing; no endpoint was created.")

    for preset, scenario, expected, expected_status in (
        ("success", "permanent_rejection", [400], "dead_lettered"),
        ("rate_limit_then_recover", "rate_limit_recovery", [429, 429, 200], "delivered"),
    ):
        run_id = "resize-check-" + uuid.uuid4().hex
        request(f"/demo/receiver-lab?run_id={run_id}", method="PUT", body={"preset": preset})
        data = {
            "run_id": run_id, "scenario": scenario, "purpose": "resize-verification",
            "order_id": run_id, "amount_cents": 100, "currency": "USD",
        }
        if scenario != "permanent_rejection":
            data["customer_id"] = "demo-resize-check"
        accepted = request("/events", method="POST", key=run_id, body={
            "endpoint_id": endpoint["id"], "type": "order.paid", "data": data,
        })
        deadline = time.monotonic() + 60
        while True:
            event = request(f"/events/{accepted['event_id']}")
            delivery = next(item for item in event["deliveries"] if item["id"] == accepted["delivery_id"])
            if delivery["status"] in ("delivered", "dead_lettered"):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"{preset}: delivery did not finish within 60 seconds")
            time.sleep(1)
        if delivery["status"] != expected_status:
            raise RuntimeError(f"{scenario}: unexpected delivery status {delivery['status']}")
        receiver = request(f"/demo/receiver-lab?run_id={run_id}")
        records = [item for item in receiver["requests"] if item["event_id"] == accepted["event_id"]]
        codes = [item["response_status_code"] for item in records]
        if codes != expected:
            raise RuntimeError(f"{preset}: expected {expected}, received {codes}")
        if len(records) == 3:
            times = [datetime.fromisoformat(item["received_at"].replace("Z", "+00:00")) for item in records]
            gaps = [(times[index + 1] - times[index]).total_seconds() for index in range(2)]
            if any(gap < 5 for gap in gaps):
                raise RuntimeError(f"Retry-After was not respected: {gaps}")
            print(f"PASS {scenario}: {codes}; retry gaps {gaps}; event {accepted['event_id']}", flush=True)
        else:
            print(f"PASS {scenario}: {codes}; event {accepted['event_id']}", flush=True)


if __name__ == "__main__":
    main()
