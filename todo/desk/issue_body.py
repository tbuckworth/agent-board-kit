"""The sections of a task's issue body, for todo-desk and any script of your own. Python stdlib only.

A body is free notes, then optional `## ` sections; the apps write `## Checklist` and agents write
`## Claude Resolution Notes`. A script imports this module by putting desk/ on sys.path, as bin/todo-desk does.
"""

CHECKLIST, CLAUDE = "## Checklist", "## Claude Resolution Notes"
SECTION_MARKERS = (CHECKLIST, CLAUDE)


def split_sections(body):
    """[(header, text)]: the notes before any '## ' heading have header '', then one entry per '## ' section."""
    out, header, buf = [], "", []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            out.append((header, "\n".join(buf)))
            header, buf = line.strip(), []
        else:
            buf.append(line)
    out.append((header, "\n".join(buf)))
    return out


def join_sections(sections):
    parts = []
    for header, text in sections:
        t = text.strip("\n")
        if header:
            parts.append(header + ("\n" + t if t else ""))
        elif t.strip():
            parts.append(t)
    return "\n\n".join(p.rstrip() for p in parts)


def split_notes(body):
    """(notes, rest): the notes are everything before the first section marker (CHECKLIST or CLAUDE)."""
    body = body or ""
    idx = min([i for i in (body.find(m) for m in SECTION_MARKERS) if i != -1], default=-1)
    return (body, "") if idx == -1 else (body[:idx], body[idx:])
