#!/usr/bin/env python3
"""MeowWise pre-publish legal-hold gate (Joshua's global rule, 27 Oct 2026; /workspace/content-review/RULE.md).

Used by _build.py (held pages are never rendered, build aborts on any new flag) and by the
git pre-push hook / CI (`python3 _tools/legal_gate.py --check`), so a held draft cannot be built
or pushed live, including on retries. A page is cleared only by Joshua's approval of that exact
version: a line in /workspace/content-review/approvals.jsonl with {"site":"meowwise.com","path":..,"sha256":..}.
Never write that file yourself.

Code-enforced: rule 3 (every image must have a licence/origin record), rule 4 (blog/article/research
pages need >=1 source with an http URL), rule 6 (election wording), rules 1/2/5/7/8 keyword tripwires.
Everything else (defamation judgement, misleading headlines, conflicting sources) is judgement-only.
"""
import glob, hashlib, json, os, re, sys, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "_src")
REVIEW = "/workspace/content-review"
QUEUE = os.path.join(REVIEW, "queue.jsonl")
APPROVALS = os.path.join(REVIEW, "approvals.jsonl")
SITE = "meowwise.com"
LEGAL_SLUGS = {"terms.html", "privacy.html", "disclaimer.html", "contact.html", "thanks.html", "404.html", "photo-credits.html"}

TRIPWIRES = [
    (1, r"\b(fraud|fraudulent|scam(?:mer)?s?|corrupt(?:ion)?|embezzl\w*|criminal(?:ly)?|arrested|charged with|convicted|lawsuit|sued|misconduct|abus(?:e|ed|er) by|cover[- ]up|liar|lied)\b"),
    (2, r"\b(home address|lives at|social security|bank account|password|leaked|private (?:message|email|text)s?|medical records?)\b"),
    (5, r"\b(you should (?:take|give|stop taking)|guaranteed (?:return|cure|results?)|dosage for your|invest in|tax advice|legal advice)\b"),
    (6, r"\b(election|ballot|polling (?:place|station)|voter registration|register to vote|vote (?:by|on)|certified results?|candidate)\b"),
    (7, r"\b((?:MeowWise|we) (?:is|are) (?:endorsed|approved|certified|an official)|official (?:partner|site) of|in partnership with|sponsored by)\b"),
    (8, r"\b(cease and desist|DMCA (?:notice|takedown)|takedown notice|embargo(?:ed)?|reporting restriction|correction request)\b"),
]


def parse(path):
    raw = open(path, encoding="utf-8").read()
    head, body = raw.split("\n---\n", 1)
    meta = {"source": []}
    for line in head.strip().splitlines():
        k, v = line.split(":", 1)
        if k.strip() == "source":
            meta["source"].append(v.strip())
        else:
            meta[k.strip()] = v.strip()
    meta.setdefault("type", "article")
    return meta, head, body, raw


def sha(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def approved(slug, digest):
    if not os.path.exists(APPROVALS):
        return False
    for l in open(APPROVALS, encoding="utf-8"):
        try:
            a = json.loads(l)
        except Exception:
            continue
        if a.get("site") == SITE and a.get("path") == slug and a.get("sha256") == digest:
            return True
    return False


def image_records():
    ok = set()
    for f in ("images/credits.json", "images/ai-assets.json"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            for x in json.load(open(p)):
                if x.get("license") or x.get("origin"):
                    ok.add(x["file"])
    return ok


def check_page(path, imgs):
    slug = os.path.relpath(path, SRC).replace(os.sep, "/")
    meta, head, body, raw = parse(path)
    flags = []
    text = head + "\n" + body
    for src in re.findall(r'(?:src|href)="/?(images/[^"]+)"', text):
        if src not in imgs:
            flags.append((3, src, "image has no licence/origin record in images/credits.json or images/ai-assets.json"))
    if meta["type"] in ("blog", "research") or (meta["type"] == "article" and slug not in LEGAL_SLUGS and meta.get("group") != "tool"):
        has_src = any(re.search(r"\|\s*https?://", s) for s in meta["source"]) or (meta["type"] == "research" and "doi.org/" in body)
        if meta["type"] in ("blog", "research") and not has_src:
            flags.append((4, meta.get("title", slug), "no source with an accessed URL"))
    if slug not in LEGAL_SLUGS:
        plain = re.sub(r"<[^>]+>", " ", text)
        for rule, rx in TRIPWIRES:
            for m in re.finditer(rx, plain, re.I):
                s = max(0, m.start() - 60)
                flags.append((rule, plain[s:m.end() + 60].replace("\n", " ").strip(), f"keyword tripwire '{m.group(0)}'"))
                break
    return slug, meta, raw, flags


def set_hold(path, raw):
    if re.search(r"^status:\s*HOLD_REVIEW\s*$", raw.split("\n---\n", 1)[0], re.M):
        return
    head, body = raw.split("\n---\n", 1)
    open(path, "w", encoding="utf-8").write(head + "\nstatus: HOLD_REVIEW\n---\n" + body)


def run(mode):
    """mode: 'build' (auto-hold new flags, return held slugs) or 'check' (no writes, exit 1 if anything unsafe)."""
    imgs = image_records()
    held, problems = [], []
    for path in sorted(glob.glob(os.path.join(SRC, "**", "*.html"), recursive=True)):
        slug, meta, raw, flags = check_page(path, imgs)
        is_held = meta.get("status", "").upper() == "HOLD_REVIEW"
        if is_held:
            # held: only Joshua's approval of this exact version (minus the status line) clears it
            unheld = re.sub(r"\nstatus:\s*HOLD_REVIEW\s*(?=\n---\n)", "", raw, count=1)
            if approved(slug, sha(unheld)):
                problems.append(f"{slug}: approved by Joshua; remove the status line by hand to publish this exact version")
            held.append(slug)
            continue
        if not flags or approved(slug, sha(raw)):
            continue
        if mode == "build":
            set_hold(path, raw)
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            for rule, flagged, why in flags:
                with open(QUEUE, "a", encoding="utf-8") as q:
                    q.write(json.dumps({"ts": ts, "site": SITE, "bot": "Cat Site Bot", "title": meta.get("title", slug),
                        "flagged": flagged, "rule": rule, "sources": [s.rsplit("|", 1)[-1].strip() for s in meta["source"]],
                        "suggestion": why + "; needs Joshua's review", "draft_path": path, "status": "HOLD_REVIEW"}) + "\n")
            held.append(slug)
            problems.append(f"{slug}: NEW HOLD rules {sorted({f[0] for f in flags})}")
        else:
            problems.append(f"{slug}: flagged rules {sorted({f[0] for f in flags})}: " + "; ".join(f[2] for f in flags))
    # held pages must never exist in the built output or sitemap
    sm = open(os.path.join(ROOT, "sitemap.xml")).read() if os.path.exists(os.path.join(ROOT, "sitemap.xml")) else ""
    for slug in held:
        if mode == "check" and (os.path.exists(os.path.join(ROOT, slug)) or slug in sm):
            problems.append(f"{slug}: HOLD_REVIEW page present in built output/sitemap")
    return held, problems


if __name__ == "__main__":
    mode = "check" if "--check" in sys.argv else "build"
    held, problems = run(mode)
    print(f"legal gate ({mode}): held={held or 'none'}")
    for p in problems:
        print("  ", p)
    sys.exit(1 if (mode == "check" and problems) else 0)
