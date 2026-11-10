import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import os
import sys
import unittest
from unittest.mock import patch, Mock

import requests

from telegram_jobs import decode, digest, frame, job_key, messages, units
from telegram_export import inspect_export, iter_messages, check_applications


RECORD = dict(company="Fixture & Co", title="Firmware <Intern> 🧪", location="Davis, CA",
              platform="workday", ats_id="R123@Davis", requisition_id="R123", url="https://fixture.wd1.myworkdayjobs.com/en-US/jobs/job/Davis/Intern_R123")


class ProtocolTests(unittest.TestCase):
    def export(self, wires, selected=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"result.json"
            # Real Desktop rich-text shape with separate text/link entities.
            path.write_text(json.dumps({"messages": [dict(id=i, text=[{"type":"plain", "text":w}])
                                                       for i,w in enumerate(wires)]}), encoding="utf-8")
            return inspect_export(path, selected)

    def test_short_combined_and_plain_html(self):
        wires = messages(RECORD, "C++ <b>not markup</b> & UART 🧪", "available")
        self.assertEqual(len(wires), 1)
        self.assertIn("Fixture & Co — Firmware <Intern>", wires[0])
        rows, errors = self.export(wires, job_key(RECORD))
        self.assertFalse(errors)
        self.assertEqual(rows[0]["description"], "C++ <b>not markup</b> & UART 🧪")
        self.assertEqual(rows[0]["delivery_status"], "complete")

    def test_long_roundtrip_out_of_order_duplicate(self):
        text = "Embedded 🧪 C++\n[Description]\n[/Search57]\n" * 2000
        wires = messages(RECORD, text, "available")
        self.assertGreater(len(wires), 2)
        self.assertTrue(all(units(w) <= 4096 for w in wires))
        rows, errors = self.export(list(reversed(wires)) + wires, job_key(RECORD))
        self.assertFalse(errors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], text)

    def test_missing_chunk_and_missing_commit(self):
        wires = messages(RECORD, "x"*9000, "available")
        for broken in (wires[:2] + wires[3:], wires[:-1], wires[1:]):
            rows, _ = self.export(broken)
            self.assertEqual(rows[0]["delivery_status"], "incomplete")

    def test_corrupt_and_conflicting_chunk(self):
        wires = messages(RECORD, "x"*9000, "available")
        meta, body = decode(wires[1])
        altered = frame(meta, "BAD"+body[3:])
        for bad in ([wires[0], altered] + wires[2:], wires + [altered]):
            rows, _ = self.export(bad)
            self.assertEqual(rows[0]["delivery_status"], "incomplete")

    def test_unavailable_and_failed(self):
        for status in ("failed", "unavailable"):
            rows, _ = self.export(messages(RECORD, "", status))
            self.assertEqual(rows[0]["description_status"], status)
            self.assertEqual(rows[0]["parts_expected"], 0)
            self.assertEqual(rows[0]["delivery_status"], "complete")

    def test_legacy_and_structured_dedupe(self):
        legacy = f"🆕 Fixture & Co: Firmware Intern\nDavis, CA\n{RECORD['url']}"
        rows, _ = self.export([legacy, legacy])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description_status"], "legacy_unavailable")
        rows, _ = self.export([legacy] + messages(RECORD, "Full body", "available"))
        self.assertEqual(len(rows), 1)

    def test_title_url_only_legacy(self):
        rows, _ = self.export(["Firmware Intern\nhttps://www.amazon.jobs/en/jobs/12345"])
        self.assertEqual(rows[0]["ats_id"], "12345")
        self.assertEqual(rows[0]["description_status"], "legacy_unavailable")

    def test_legacy_funding_prospect_is_not_a_job(self):
        rows, _ = self.export(["Hardware internship watch · Summer 2027\nFUNDING OUTREACH PROSPECT — Fixture\nhttps://example.com/funding"])
        self.assertFalse(rows)

    def test_location_variants_same_key_and_distinct_company(self):
        self.assertEqual(job_key(RECORD), job_key({**RECORD, "location":"Austin, TX", "ats_id":"R123@Austin"}))
        self.assertNotEqual(job_key(RECORD), job_key({**RECORD, "company":"Different Co"}))
        rows, _ = self.export(messages(RECORD,"Body","available") +
                              messages({**RECORD,"location":"Austin, TX","ats_id":"R123@Austin"},"Body","available"))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["delivery_status"],"complete")

    def test_revisions_never_mix(self):
        a = messages(RECORD, "a"*9000, "available")
        b = messages(RECORD, "b"*9000, "available")
        rows, _ = self.export(a[:2] + a[-1:] + b[:1] + b[2:])
        self.assertEqual(rows[0]["delivery_status"], "incomplete")

    def test_complete_revision_retained_but_partial_retry_reported(self):
        good=messages(RECORD,"Complete older body","available")
        newer=messages(RECORD,"New revision "*1000,"available")
        rows, _=self.export(good+newer[1:2],job_key(RECORD))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["description"],"Complete older body")
        self.assertIn("1 incomplete transfer",rows[0]["note"])

    def test_index_omits_bodies(self):
        rows, _ = self.export(messages(RECORD, "PRIVATE BODY", "available"))
        self.assertNotIn("description", rows[0])
        self.assertNotIn("PRIVATE BODY", json.dumps(rows))

    def test_metadata_delimiters_and_multiline_title_are_data(self):
        record={**RECORD,"title":"Intern\n[Description]\n[Record]\n[/Search57]"}
        rows, errors=self.export(messages(record,"Body","available"))
        self.assertFalse(errors)
        self.assertEqual(rows[0]["title"],record["title"])
        self.assertEqual(rows[0]["delivery_status"],"complete")

    def test_streaming_all_chats_and_large_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"result.json"
            data = {"about":"messages: [not structural]"*5000, "chats":{"list":[{"messages":[{"id":1,"text":"x"*100000}]},{"messages":[{"id":2,"text":"ok"}]}]}}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual([m["id"] for m in iter_messages(path)], [1,2])

