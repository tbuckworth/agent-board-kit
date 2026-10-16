"""OpenRouter: per-token, nothing "left running". Read the key's spend counters for the daily-spend rule."""
import os

from . import http_json


def collect(acct, opts):
    d = (http_json("https://openrouter.ai/api/v1/key",
                   headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]}) or {}).get("data") or {}
    return {"resources": [], "metrics": {"day_spend_usd": round(float(d.get("usage_daily") or 0), 2),
                                         "month_spend_usd": round(float(d.get("usage_monthly") or 0), 2)}}


def stop(acct, native_id):
    return False, "nothing to stop"
