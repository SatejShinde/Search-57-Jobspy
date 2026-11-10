"""Search57 Telegram wire format v1. Standard library only; shared with export tools."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MARKER = "[Search57 v1]\n"
BODY = "\n[Description]\n"
END = "\n[/Search57]"
LIMIT = 4096


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def units(text):
    # Conservative for astral Unicode; never split a Python Unicode code point.
    return len(text.encode("utf-16-le")) // 2


def normalized_url(url):
    u = urlsplit(str(url))
    query = [(k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith("utm_")
             and k.lower() not in {"source", "ref", "referrer", "gh_src", "fbclid", "gclid"}]
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"),
                       urlencode(sorted(query)), ""))


def job_key(record):
    if record.get("collector") == "jobspy":
        identity = str(record.get("jobspy_id") or "")
        prefix={"linkedin":"li-","indeed":"in-","zip_recruiter":"zr-"}.get(record['platform'],"")
        if prefix and identity.startswith(prefix):identity=identity[len(prefix):]
        if not identity and record['platform']=='linkedin':
            match=re.search(r"(?:/|[-])(\d+)/?$",urlsplit(record['url']).path)
            identity=match[1] if match else ""
        if not identity and record['platform']=='indeed':
            identity=dict(parse_qsl(urlsplit(record['url']).query)).get('jk','')
        identity=identity or normalized_url(record["url"])
        return "s57_" + digest(f"collector:jobspy|{record['platform']}|{identity}")[:32]
    company = re.sub(r"\W+", "", record.get("company", "").casefold())
    req = str(record.get("requisition_id") or record.get("ats_id") or "").split("@")[0]
    identity = req.casefold() or normalized_url(record["url"])
    scope = f"canary:{record['canary_id']}|" if record.get("canary") else ""
    return "s57_" + digest(f"{scope}{company}|{record.get('platform', 'unknown')}|{identity}")[:32]


def frame(meta, text=""):
    line = lambda value: str(value).replace("\r", " ").replace("\n", " ")
    readable = (f"{line(meta['company'])} — {line(meta['title'])}\n{line(meta['location'])}\n{line(meta['url'])}\n"
                f"Description: {meta['status']} · {meta['parts']} part(s)\n"
                if meta.get("kind") == "job" else
                f"Description {meta.get('part', 0)}/{meta['parts']} · {meta['key']}\n")
    label = "CANARY TEST — isolated delivery check\n" if meta.get("canary") else ""
    wire = MARKER + label + readable + "[Record]\n" + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + BODY + text + END
    if units(wire) > LIMIT:
        raise ValueError("Search57 message exceeds Telegram limit; nothing truncated")
    return wire


def decode(text):
    if not text.startswith(MARKER) or not text.endswith(END):
        return None
    head, body = text[len(MARKER):-len(END)].split(BODY, 1)
    head = head.rsplit("[Record]\n", 1)[-1]
    meta = json.loads(head)
    if meta.get("v") != 1 or meta.get("kind") not in {"job", "part", "complete"}:
        raise ValueError("Unsupported Search57 envelope")
    if not re.fullmatch(r"s57_[0-9a-f]{32}", meta.get("key", "")):
        raise ValueError("Invalid Search57 key")
    if not re.fullmatch(r"[0-9a-f]{64}", meta.get("sha256", "")):
        raise ValueError("Invalid Search57 digest")
    if not isinstance(meta.get("parts"), int) or not 0 <= meta["parts"] <= 100000:
        raise ValueError("Invalid part count")
    return meta, body


def messages(record, description, status, note=""):
    """No transport truncation. Digest is revision identity and integrity check.

    One compact JSON metadata line is deliberate: fields cannot forge delimiters.
    Description text is always plain text, with original whitespace preserved.
    """
    description = description or ""
    sha = digest(description)
    common = dict(v=1, key=job_key(record), sha256=sha)
    if record.get("collector"):
        common["collector"] = record["collector"]
    if record.get("canary"):
        common.update(canary=True, canary_id=record["canary_id"])
    meta = {**common, **record, "kind": "job", "status": status, "note": note,
            "parts": 1 if description else 0, "part": 1 if description else 0}
    # A short description and all posting metadata fit in one readable record.
    try:
        return [frame(meta, description)]
    except ValueError:
        if not description:
            raise
    # Smaller fixed payload budget leaves ample envelope space, even with emojis.
    pieces = []
    start = 0
    while start < len(description):
        end = start
        used = 0
        while end < len(description) and used + units(description[end]) <= 3400:
            used += units(description[end]); end += 1
        pieces.append(description[start:end]); start = end
    meta.update(parts=len(pieces), part=0)
    result = [frame(meta)]
    result += [frame({**common, "kind": "part", "parts": len(pieces), "part": i}, text)
               for i, text in enumerate(pieces, 1)]
    result.append(frame({**common, "kind": "complete", "parts": len(pieces), "part": 0}))
    return result
