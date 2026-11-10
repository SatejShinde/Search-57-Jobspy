"""Targeted adapters for python-jobspy==1.4.1; never invoke scrape/search.

Only public board pages are requested. A denial is terminal for this attempt;
no proxy, CAPTCHA solving, authentication, or transport escalation is added.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

import requests

BOARDS = {"linkedin": {"linkedin.com", "www.linkedin.com"},
          "indeed": {"indeed.com", "www.indeed.com"},
          "zip_recruiter": {"ziprecruiter.com", "www.ziprecruiter.com"}}


def text(value):
    return value if isinstance(value, str) and value not in {"nan", "None"} else ""


def board_id(record):
    value = text(record.get("jobspy_id"))
    prefix = {"linkedin":"li-", "indeed":"in-", "zip_recruiter":"zr-"}.get(record["platform"], "")
    if prefix and value.startswith(prefix):
        return value[len(prefix):]
    parsed = urlsplit(record["url"])
    if record["platform"] == "indeed":
        return parse_qs(parsed.query).get("jk", [""])[0]
    if record["platform"] == "linkedin":
        match = re.search(r"(?:/|[-])(\d+)(?:/)?$", parsed.path)
        return match[1] if match else ""
    return value


def validate_url(record):
    parsed = urlsplit(record["url"])
    if (parsed.scheme != "https" or parsed.hostname not in BOARDS.get(record["platform"], set())
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError("Not an approved public board URL")


def classification(value, source, *, full=False, reason=""):
    value = text(value)
    if not value.strip():
        return dict(text="",status="unavailable",source=source,note=reason or "No public description returned")
    limited = bool(re.search(r"(?:\.\.\.|…|\bread more|\bshow more)\s*$", value, re.I))
    partial = limited or not full
    return dict(text=value, status="partial" if partial else "available", source=source,
                note=("Possible upstream excerpt/truncation; " if limited else "")+
                     (reason or ("Full source body returned; source completeness not independently certified"
                                  if full else "Listing excerpt/unknown completeness")))


def build_scraper(platform):
    from jobspy.model import ScraperInput, DescriptionFormat, Country
    from jobspy.util import RequestsRotating, TLSRotating, create_session
    config = ScraperInput(fetch_description=False, country_indeed=Country.USA,
                          description_format=DescriptionFormat.MARKDOWN, proxies=[])
    if platform == "linkedin":
        from jobspy.linkedin import LinkedIn
        from jobspy.linkedin.constant import headers
        scraper = LinkedIn()
        scraper.scraper_input = config
        scraper.session = create_session(RequestsRotating([], clear_cookies=True), config, headers)
    elif platform == "zip_recruiter":
        from jobspy.ziprecruiter import ZipRecruiter
        scraper = ZipRecruiter()
        scraper.scraper_input = config
        scraper.session = create_session(TLSRotating([]), config)
        scraper.session.impersonate = "safari"  # same initial transport as upstream
    else:
        raise ValueError("No upstream targeted method for this board")
    scraper.session.request_timeout = 12
    scraper.session.verify = True  # explicit TLS verification, never weaken it
    return scraper


def indeed_detail(url):
    from bs4 import BeautifulSoup
    from jobspy.model import DescriptionFormat
    from jobspy.util import description_fields
    response = requests.get(url, timeout=12, allow_redirects=False)
    if response.status_code != 200:
        return "", f"Public detail request returned HTTP {response.status_code}; no bypass attempted"
    soup = BeautifulSoup(response.text, "html.parser")
    content = soup.find(id="jobDescriptionText")
    if content:
        return description_fields(str(content),DescriptionFormat.MARKDOWN).get("description") or "", ""
    for script in soup.find_all("script",type="application/ld+json"):
        try: values=json.loads(script.string or script.get_text())
        except ValueError: continue
        stack=values if isinstance(values,list) else [values]
        while stack:
            value=stack.pop()
            if not isinstance(value,dict): continue
            stack.extend(value.get("@graph",[]))
            if "JobPosting" in value.get("@type",[]) and isinstance(value.get("description"),str):
                return description_fields(value["description"],DescriptionFormat.MARKDOWN).get("description") or "", ""
    return "", "No full description container; challenge, closed page or unsupported page"


def retrieve(entry, listing=""):
    record=entry["record"]; platform=record["platform"]
    validate_url(record)
    # Indeed's installed search adapter parses description.html, not a teaser.
    cached=classification(listing,"listing:"+platform,full=platform=="indeed")
    if cached["status"] == "available":
        return cached
    if platform == "indeed":
        value,reason=indeed_detail(record["url"])
        detail=classification(value,"targeted:indeed",full=True,reason=reason)
    else:
        scraper=build_scraper(platform)
        try:
            status=[]
            original_get=scraper.session.get
            def get(*args,**kwargs):
                response=original_get(*args,**kwargs)
                status.append(response.status_code)
                return response
            scraper.session.get=get
            native_id=board_id(record)
            if platform=="linkedin" and not native_id.isdigit():
                raise ValueError("LinkedIn job ID unavailable")
            # Do not call scrape(): these two methods require only the same
            # initialized scraper_input/session that scrape() normally creates.
            fields=(scraper._fetch_details(native_id) if platform=="linkedin" else
                    scraper._fetch_details(native_id or None,record["url"]))
            reason=(f"Public detail returned HTTP {status[-1]}; no bypass attempted"
                    if status and status[-1]!=200 else "")
            detail=classification(fields.get("description"),"targeted:"+platform,full=True,reason=reason)
        finally:
            scraper.session.get=original_get
            scraper.session.close()
    if detail["text"]:
        return detail
    if text(listing).strip():
        cached.update(status="partial",note="Targeted detail unavailable; retained listing excerpt")
        return cached
    return detail
