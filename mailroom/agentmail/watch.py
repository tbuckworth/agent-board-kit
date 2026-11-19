"""What the watch daemons (agent-failover, agent-memwatch) share: their loop, their options and their log line.
--dry-run implies --once, and the step itself must act on nothing; --json is for the step's report."""
import copy
import time


def log(msg):
    """One line on stdout, which systemd puts in the journal, with the time in front."""
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def add_options(ap, dry_run_help):
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help=dry_run_help + " (implies --once)")
    ap.add_argument("--json", action="store_true")


def once(a):
    """Whether these options (add_options) ask for a single pass."""
    return a.once or a.dry_run


def run(a, step, state, interval):
    """Run the loop with options a. step(state dict) -> the new state; state: the daemon's jsonfile.JsonFile;
    interval() -> seconds to sleep between passes. Each pass reads the state file, hands step a copy (a step may
    change the dict it is given and return it) and writes the result back when it differs (never on a dry run);
    a pass that raises is logged on stdout and the loop carries on. A state file that is not JSON fails every pass,
    naming the file (jsonfile.read), until the operator fixes or deletes it. Returns the exit code of a single pass
    (1 if it raised); loops forever otherwise."""
    single = once(a)
    while True:
        try:
            old = state.read()
            new = step(copy.deepcopy(old))
            if not a.dry_run and new != old:
                state.write(new)
        except Exception as e:  # noqa: BLE001 - a watch must outlive one bad pass
            log("error: %s: %s" % (type(e).__name__, e))
            if single:
                return 1
        if single:
            return 0
        time.sleep(interval())
