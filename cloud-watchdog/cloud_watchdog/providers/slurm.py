"""Slurm: your jobs on paid partitions, over SSH (read-only `squeue --me`; `scancel` to stop).

Account settings:
  ssh_host            ssh alias of a node where squeue and scancel work (required)
  paid_partitions     glob patterns of partitions that cost money (required); other partitions are ignored
  partition_rates     {partition glob: USD per hour of one job there}; jobs without a rate show no cost
  month_cost_command  optional command on the same host that prints this month's spend
  month_cost_regex    regex with one group capturing the dollar amount from that output
"""
import fnmatch
import re

from .. import util
from . import clean

DEFAULT_COST_RE = r"\$\s*([\d,]+(?:\.\d+)?)"


def _elapsed(text):
    """Slurm %M: [D-]HH:MM:SS or MM:SS -> seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def _host(acct):
    host = acct.get("ssh_host")
    if not host:
        raise RuntimeError("set ssh_host for this Slurm account in ~/.config/cloud-watchdog/config.toml")
    return host


def _paid(acct, partition):
    pats = acct.get("paid_partitions") or []
    if not pats:
        raise RuntimeError("set paid_partitions (glob patterns) for this Slurm account")
    return any(fnmatch.fnmatch(partition, p) for p in pats)


def rate_for(acct, partition):
    rates = acct.get("partition_rates") or {}
    if partition in rates:
        return float(rates[partition])
    for pat, rate in rates.items():
        if fnmatch.fnmatch(partition, pat):
            return float(rate)
    return None


def parse(acct, jobs_txt, now):
    res = []
    for line in jobs_txt.strip().splitlines():
        f = line.strip().split("|")
        if len(f) < 5 or not _paid(acct, f[1]):
            continue
        jid, part, name, state, el = f[:5]
        if state not in ("RUNNING", "CONFIGURING", "COMPLETING"):
            continue
        secs = _elapsed(el) if re.match(r"[\d:-]+$", el) else 0
        rate = rate_for(acct, part)
        res.append(clean({"native_id": jid, "name": name, "kind": "job", "state": state, "partition": part,
                          "launched_at": now - secs, "rate_usd_h": rate, "rate_source": "partition list price",
                          "cost_usd": round(rate * secs / 3600, 2) if rate else None,
                          "cost_source": "estimate: price x elapsed"}))
    return res


def collect(acct, opts):
    now = opts["now"]
    host = _host(acct)
    _paid(acct, "")  # fail early and clearly when no paid partitions are configured
    remote = 'squeue --me -h -o "%i|%P|%j|%T|%M"'
    if acct.get("month_cost_command"):
        remote += "; echo ===; " + acct["month_cost_command"]
    code, out, err = util.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, remote], timeout=60)
    if code != 0:
        raise RuntimeError("ssh %s failed (%s): %s" % (host, code, err.strip()[-200:]))
    jobs_txt, _, cost_txt = out.partition("===")
    metrics = {}
    if acct.get("month_cost_command"):
        m = re.search(acct.get("month_cost_regex") or DEFAULT_COST_RE, cost_txt)
        if m:
            metrics["month_estimated_usd"] = float(m.group(1).replace(",", ""))
    return {"resources": parse(acct, jobs_txt, now), "metrics": metrics}


def stop(acct, job_id):
    host = _host(acct)
    code, out, err = util.run(["ssh", "-o", "BatchMode=yes", host, "scancel %s" % int(job_id)], timeout=60)
    return code == 0, (out + err).strip()[-200:] or "scancel sent"
