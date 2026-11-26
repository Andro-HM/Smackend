"""Independent asyncio client for checking the Locust measurements."""

import asyncio
import json
from pathlib import Path
import random
import sys
from time import perf_counter
from uuid import uuid4

import httpx

from live_checks import AUTH, ORIGIN, PASSWORD, csrf


async def main(seconds, count, output, offered_actions=0):
    samples = []
    started = perf_counter()

    async def user(index):
        await asyncio.sleep(index / 5)
        rng = random.Random(index)
        async with httpx.AsyncClient(base_url=ORIGIN, verify=False, trust_env=False, timeout=8) as client:
            action_delay = 0.0

            async def call(name, method, path, **kwargs):
                sent = perf_counter()
                try:
                    response = await client.request(method, path, **kwargs)
                except httpx.HTTPError:
                    samples.append([perf_counter() - started, name, (perf_counter() - sent) * 1000, False, {}, action_delay])
                    return None
                samples.append([
                    perf_counter() - started, name, (perf_counter() - sent) * 1000,
                    response.is_success, json.loads(response.headers.get("X-Phase0-Profile", "{}")), action_delay,
                ])
                return response

            await call("bootstrap", "GET", "/")
            signup = await call("signup", "POST", AUTH + "signup", headers=csrf(client), json={
                "username": "audit" + uuid4().hex[:16], "password": PASSWORD,
            })
            if signup is None or not signup.is_success:
                return
            due = started + count / 5 + 3 + index / offered_actions if offered_actions else None
            while (perf_counter() if due is None else due) - started < seconds:
                if due is not None:
                    await asyncio.sleep(max(0, due - perf_counter()))
                    action_delay = max(0, perf_counter() - due) * 1000
                    if perf_counter() - started >= seconds:
                        samples.append([perf_counter() - started, "missed_action", 0, False, {}, action_delay])
                        due += count / offered_actions
                        continue
                choice = rng.randrange(10)
                if choice < 6:
                    await call("list", "GET", "/api/notes/")
                elif choice < 8:
                    record = await call("create", "POST", "/api/notes/", headers=csrf(client), json={"title": "Synthetic"})
                    if record is not None and record.is_success:
                        await call("delete", "DELETE", f"/api/notes/{record.json()['id']}", headers=csrf(client))
                elif choice == 8:
                    await call("provider", "GET", "/api/provider")
                else:
                    await call("session", "GET", AUTH + "session")
                if due is None:
                    await asyncio.sleep(rng.uniform(0.25, 0.75))
                else:
                    due += count / offered_actions

    await asyncio.gather(*(user(index) for index in range(count)))
    Path(output).write_text(json.dumps(samples), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], int(sys.argv[4])))
