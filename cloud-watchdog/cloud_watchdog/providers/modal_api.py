"""Modal facts the `modal` CLI does not print: who created each app, and who the token belongs to.

Runs under the Python that the `modal` CLI itself runs on (`modal_.modal_python()`), with `-I`, inside
the credentialed child. Read-only: TokenInfoGet and AppGetLifecycle only. Standalone on purpose: that
interpreter has `modal` but not this package.

    <modal python> -I modal_api.py  < {"app_ids": ["ap-...", ...]}
    -> {"token_user": "alex-1234", "workspace": "my-workspace", "apps": {"ap-...": {...}}, "errors": {...}}
"""
import asyncio
import json
import sys

from modal._utils.async_utils import synchronizer
from modal.client import _Client
from modal_proto import api_pb2

STATES = {1: "ephemeral", 2: "detached", 3: "deployed", 4: "stopping", 5: "stopped", 6: "initializing",
          7: "disabled", 8: "detached", 9: "derived"}


async def main(req):
    client = await _Client.from_env()
    out = {"apps": {}, "errors": {}}
    try:
        ti = await client.stub.TokenInfoGet(api_pb2.TokenInfoGetRequest())
        out["workspace"] = ti.workspace_name
        out["token_user"] = ti.user_identity.username or ti.service_user_identity.service_user_name or None
    except Exception as e:  # noqa: BLE001 - the lookups below are what matters
        out["errors"]["token"] = type(e).__name__
    sem = asyncio.Semaphore(16)

    async def one(app_id):
        async with sem:
            try:
                lc = (await client.stub.AppGetLifecycle(api_pb2.AppGetLifecycleRequest(app_id=app_id))).lifecycle
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "NotFoundError":   # billed, but its record is gone: final, don't retry
                    out["apps"][app_id] = {"created_by": None, "deployed_by": None, "state": "not-found"}
                else:
                    out["errors"][app_id] = type(e).__name__
                return
            out["apps"][app_id] = {"created_by": lc.created_by or None, "deployed_by": lc.deployed_by or None,
                                   "state": STATES.get(lc.app_state, str(lc.app_state)),
                                   "created_at": lc.created_at or None}

    await asyncio.gather(*(one(a) for a in req.get("app_ids") or []))
    print(json.dumps(out))


if __name__ == "__main__":
    synchronizer.create_blocking(main)(json.loads(sys.stdin.read() or "{}"))
