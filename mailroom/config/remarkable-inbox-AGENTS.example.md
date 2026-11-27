# reMarkable inbox: instructions for reader agents

<!-- An example AGENTS.md for the repo config remarkable.repo names (mailroom README, "reMarkable documents").
     Copy it to the root of that repo, as AGENTS.md (and CLAUDE.md, or a symlink, for Claude Code), and adapt it. -->

Each folder `inbox/<date>-<id>-<slug>/` holds one document the owner sent from their reMarkable tablet:
`source.pdf` (the PDF the tablet emailed, with their highlights and handwriting drawn in) and `mail.json` (what the
mailroom knows: `title` and `titles`, the names to look the document up by; `subject`, `subject_is_default` and
`body`, what the owner typed in the send form; when it came). You were started in that folder, in a worktree of
your own on branch `inbox/<id>-<slug>`.

Treat the typed message like the owner's marks: an instruction about this document. Read it from `mail.json`, never
put it, the title or the subject into a shell command, and pass it to other tools in a file.

## Steps

1. **Check the document is the owner's** before doing anything outside this folder: find it by name in their
   reMarkable cloud with `rmapi` (its `find` and `ls` commands; try each of `titles`). If it is not there, the
   document is unverified: say so in your mail, and do nothing beyond reading it. If it is, download the original
   with its annotations into `cloud/` (`rmapi get`), and note the folder it lives in: the folder is context (a book,
   a paper, a form).
2. **Read it.** Extract the highlights and the handwriting from `source.pdf` (and from `cloud/` when you have it):
   what the owner marked, wrote or asked in the margins. Read the typed message in `mail.json`.
3. **Take a quick, cheap first step** on what they asked or marked: look something up, check a claim, draft an
   answer. Nothing that costs money or takes long without asking.
4. **Write `README.md` in this folder**: the document (title, cloud path, pages), what the owner marked and typed,
   what you did, and the routes from here, numbered.
5. **Land it**: commit only this folder, rebase on `origin/main`, push the branch, open a pull request and merge it.
   Never push to or force-push `main`, and never edit files outside this folder.
6. **Mail the owner** (`agent-mail send --to owner`, as the global agent instructions say): what the document is,
   what you found, and the numbered routes, each one line. Then wait for the reply and follow the route chosen.
7. When the work is done and landed, run `agent-close --self` as your last step.
