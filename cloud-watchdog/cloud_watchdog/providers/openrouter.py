"""OpenRouter: per-token, nothing "left running". Read the key's spend counters for the daily-spend rule."""
import os

from . import http_json

HAS_RESOURCES = False


def collect(acct, opts):
    d = http_json("https://openrouter.ai/api/v1/key", bearer=os.environ["OPENROUTER_API_KEY"])["data"]
    return {"resources": [], "metrics": {"day_spend_usd": round(float(d["usage_daily"]), 2),
                                         "month_spend_usd": round(float(d["usage_monthly"]), 2)}}

