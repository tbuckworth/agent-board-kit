// The phone's snooze choices: the Mac's rule (Snooze.presets in mac/Sources/AgentBoard/Mail.swift), kept the same, so
// a thread snoozed on either comes back at the same moment. Change both together; tests/test_mailweb.py runs this file
// under node on the cases the Mac's --selftest-mail checks.
"use strict";

// Gmail's choices at `now` (epoch milliseconds), in this device's time zone: later today (three hours on, to the
// nearest hour, while that is still today), tomorrow 08:00, this weekend (Saturday 08:00, offered Monday to Friday) and
// next week (Monday 08:00 of the coming week). A choice at the same moment as an earlier one is left out. Each is
// {label, at}, `at` in epoch seconds.
function snoozePresets(now) {
  const out = [];
  const add = (label, d) => {
    const at = d.getTime() / 1000;
    if (d.getTime() > now && !out.some((c) => c.at === at)) out.push({ label: label, at: at });
  };
  const today = new Date(now);
  const ahead = new Date(now + 3 * 3600 * 1000);
  const later = new Date(ahead);
  later.setMinutes(0, 0, 0);
  if (ahead.getTime() - later.getTime() >= 1800 * 1000) later.setHours(later.getHours() + 1);
  if (later.toDateString() === today.toDateString()) add("Later today", later);
  const y = today.getFullYear(), m = today.getMonth(), day = today.getDate(), weekday = today.getDay();  // 0 Sunday
  add("Tomorrow", new Date(y, m, day + 1, 8));
  if (weekday !== 0 && weekday !== 6) add("This weekend", new Date(y, m, day + 6 - weekday, 8));
  const tomorrow = new Date(y, m, day + 1);
  add("Next week", new Date(y, m, day + 1 + (8 - tomorrow.getDay()) % 7, 8));
  return out;
}

if (typeof module !== "undefined") module.exports = { snoozePresets };
