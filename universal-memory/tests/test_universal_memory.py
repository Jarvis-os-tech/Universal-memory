#!/usr/bin/env python3
"""Tests for Universal Memory.

Run: python3 tests/test_universal_memory.py -v      (or: make test)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = str(HERE.parent / "src" / "universal_memory.py")
PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="universal-memory-test-")
        self.env = {
            **os.environ,
            "UNIVERSAL_MEMORY_DB": str(pathlib.Path(self.dir) / "memory.db"),
            "UNIVERSAL_MEMORY_CONFIG": str(pathlib.Path(self.dir) / "config.json"),
            "UNIVERSAL_MEMORY_ACTOR": "test-agent",
            "NO_COLOR": "1",
        }
        self.cli("init")
        # tests are never a TTY, so deletes must go through the approval flow

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def cli(self, *args: str, input_: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, SCRIPT, *args], input=input_, capture_output=True, text=True,
            env=self.env, timeout=60,
        )

    def ok(self, *args: str, input_: str | None = None) -> str:
        r = self.cli(*args, input_=input_)
        self.assertEqual(r.returncode, 0,
                         f"{' '.join(args)} -> {r.returncode}\n{r.stderr}")
        return r.stdout

    def json_out(self, *args: str) -> object:
        return json.loads(self.ok(*args, "--json"))

    def token(self) -> str:
        rows = json.loads(self.ok("pending", "--json"))
        self.assertTrue(rows, "expected a pending approval")
        return rows[0]["token"]


class TestCrud(Base):
    def test_put_get_append(self):
        self.ok("put", "ns1:key1", "hello")
        self.assertEqual(self.ok("get", "ns1:key1").strip(), "hello")
        self.ok("append", "ns1:key1", "world")
        self.assertEqual(self.ok("get", "ns1:key1").strip(), "hello\nworld")

    def test_default_namespace(self):
        self.ok("put", "solo", "v")
        row = self.json_out("get", "solo")
        self.assertEqual(row["ns"], "global")
        self.assertEqual(row["key"], "solo")

    def test_metadata_on_get(self):
        self.ok("put", "a:b", "v", "--tags", "x,y")
        row = self.json_out("get", "a:b")
        self.assertEqual(row["tags"], ["x", "y"])
        self.assertEqual(row["actor"], "test-agent")
        self.assertEqual(row["type"], "kv")
        self.assertEqual(row["version"], 1)

    def test_overwrite_bumps_version(self):
        self.ok("put", "a:b", "one")
        self.ok("put", "a:b", "two")
        row = self.json_out("get", "a:b")
        self.assertEqual(row["value"], "two")
        self.assertEqual(row["version"], 2)

    def test_stdin(self):
        self.ok("put", "a:multi", "--stdin", input_="line1\nline2")
        self.assertEqual(self.ok("get", "a:multi"), "line1\nline2\n")

    def test_missing_key_exits_2(self):
        r = self.cli("get", "nope:missing")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no memory", r.stderr)

    def test_log_type_prefixes_timestamps(self):
        self.ok("log", "runs:today", "first")
        time.sleep(1.1)
        self.ok("log", "runs:today", "second")
        row = self.json_out("get", "runs:today")
        self.assertEqual(row["type"], "log")
        lines = row["value"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[0], lines[1])
        self.assertTrue(lines[0].startswith("["))

    def test_value_size_limit(self):
        big = "x" * 2_000_000
        r = self.cli("put", "a:big", "--stdin", input_=big)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("limit", r.stderr)


class TestSearchAndList(Base):
    def setUp(self):
        super().setUp()
        self.ok("put", "hermes:stack", "openKylin voice stack in Downloads")
        self.ok("put", "opencode:rules", "never guess CLI flags")
        self.ok("put", "shared:voice", "voice pipeline notes", "--tags", "voice")

    def test_search_matches_across_namespaces(self):
        rows = self.json_out("search", "voice")
        keys = {(r["ns"], r["key"]) for r in rows}
        self.assertIn(("hermes", "stack"), keys)
        self.assertIn(("shared", "voice"), keys)

    def test_search_namespace_filter(self):
        rows = self.json_out("search", "voice", "--ns", "shared")
        self.assertEqual([r["key"] for r in rows], ["voice"])

    def test_search_no_match(self):
        rows = self.json_out("search", "zzzznotpresent")
        self.assertEqual(rows, [])

    def test_search_survives_fts_metacharacters(self):
        for q in ('"quoted"', "a AND b", "foo(bar)", "col:value", "*"):
            r = self.cli("search", q, "--json")
            self.assertEqual(r.returncode, 0, f"{q}: {r.stderr}")

    def test_list_and_namespaces(self):
        rows = self.json_out("list")
        self.assertEqual(len(rows), 3)
        ns = self.json_out("list", "--namespaces")
        self.assertEqual(sorted(r["ns"] for r in ns),
                         ["hermes", "opencode", "shared"])

    def test_tag_filter(self):
        rows = self.json_out("list", "--ns", "shared", "--tag", "voice")
        self.assertEqual([r["key"] for r in rows], ["voice"])
        rows = self.json_out("list", "--ns", "shared", "--tag", "absent")
        self.assertEqual(rows, [])


class TestDeleteApproval(Base):
    """The one place a human is required."""

    def setUp(self):
        super().setUp()
        self.ok("put", "shared:note", "important fact")

    def test_noninteractive_delete_is_refused(self):
        r = self.cli("del", "shared:note")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertTrue(self.json_out("get", "shared:note"),
                        "record must survive an unapproved delete")

    def test_delete_response_carries_redeem_instructions(self):
        r = self.cli("del", "shared:note", "--json")
        payload = json.loads(r.stdout)
        self.assertFalse(payload["deleted"])
        self.assertTrue(payload["approval_required"])
        self.assertIn("universal-memory approve", payload["approve_with"])
        self.assertEqual(payload["expires_at"][-6:], "+00:00")

    def test_approve_then_record_gone(self):
        self.cli("del", "shared:note")
        out = self.ok("approve", self.token())
        self.assertIn("deleted", out)
        self.assertEqual(self.cli("get", "shared:note").returncode, 2)

    def test_token_cannot_be_reused(self):
        self.cli("del", "shared:note")
        tok = self.token()
        self.ok("approve", tok)
        r = self.cli("approve", tok)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already", r.stderr)

    def test_deny_keeps_record(self):
        self.cli("del", "shared:note")
        self.ok("deny", self.token())
        self.assertTrue(self.json_out("get", "shared:note"))
        self.assertEqual(self.json_out("pending"), [])

    def test_pending_human_output_names_the_token(self):
        self.cli("del", "shared:note")
        tok = self.token()
        text = self.ok("pending")
        self.assertIn(tok, text)
        self.assertIn("shared/note", text)
        self.assertIn("universal-memory approve", text)
        self.assertIn("universal-memory deny", text)

    def test_pending_empty(self):
        self.assertIn("no pending", self.ok("pending"))

    def test_unknown_token(self):
        r = self.cli("approve", "not-a-real-token")
        self.assertEqual(r.returncode, 2)

    def test_expired_token_requires_reapproval(self):
        cfg = json.loads(pathlib.Path(self.env["UNIVERSAL_MEMORY_CONFIG"]).read_text())
        cfg["approval_ttl_seconds"] = 0
        pathlib.Path(self.env["UNIVERSAL_MEMORY_CONFIG"]).write_text(json.dumps(cfg))
        self.cli("del", "shared:note")
        r = self.cli("approve", self.token())
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertTrue(self.json_out("get", "shared:note"))

    def test_delete_audited(self):
        self.cli("del", "shared:note")
        tok = self.token()
        self.ok("approve", tok)
        rows = json.loads(self.ok("list", "--json"))  # smoke
        import sqlite3
        con = sqlite3.connect(self.env["UNIVERSAL_MEMORY_DB"])
        ops = [r[0] for r in con.execute("SELECT op FROM audit ORDER BY id")]
        con.close()
        self.assertIn("delete_requested", ops)
        self.assertIn("approve", ops)
        self.assertIn("delete", ops)


class TestMcpStdio(Base):
    def drive(self, msgs: list[dict]) -> list[dict]:
        inp = "\n".join(json.dumps(m) for m in msgs) + "\n"
        r = subprocess.run([PY, SCRIPT, "mcp"], input=inp, capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]

    @staticmethod
    def call(reply: dict) -> dict:
        return json.loads(reply["result"]["content"][0]["text"])

    def handshake(self) -> list[dict]:
        return [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]

    def test_initialize_and_tools_list(self):
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        init = out[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        names = [t["name"] for t in out[-1]["result"]["tools"]]
        self.assertEqual(sorted(names),
                         ["memory_append", "memory_delete", "memory_get",
                          "memory_list", "memory_put", "memory_search"])

    def test_put_get_append_search(self):
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_put", "arguments": {
                 "ns": "browser", "key": "chatgpt", "value": "prefers terse"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "memory_append", "arguments": {
                 "ns": "browser", "key": "chatgpt", "text": " + fast"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "memory_search", "arguments": {"query": "terse"}}},
        ])
        self.assertFalse(out[1]["result"]["isError"])
        self.assertIn("terse", out[2]["result"]["content"][0]["text"])
        self.assertEqual(self.call(out[3])["count"], 1)

    def test_delete_tool_never_deletes(self):
        self.ok("put", "browser:x", "keep me")
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_delete", "arguments": {
                 "ns": "browser", "key": "x"}}}])
        body = self.call(out[-1])
        self.assertFalse(body["deleted"])
        self.assertTrue(body["approval_required"])
        self.assertIn("universal-memory approve", body["approve_with"])
        self.assertTrue(self.json_out("get", "browser:x"),
                        "MCP delete must not remove anything by itself")

    def test_missing_record_is_tool_error_not_crash(self):
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_get", "arguments": {"key": "nope"}}}])
        self.assertTrue(out[-1]["result"]["isError"])

    def test_unknown_method(self):
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 9, "method": "nope/nope"}])
        self.assertEqual(out[-1]["error"]["code"], -32601)

    def test_approve_is_not_an_mcp_tool(self):
        out = self.drive(self.handshake() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_approve", "arguments": {"token": "x"}}}])
        self.assertEqual(out[-1]["error"]["code"], -32602)


class TestMcpHttp(Base):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.proc = subprocess.Popen(
            [PY, SCRIPT, "serve", "--port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=self.env)
        banner = self.proc.stdout.readline() + self.proc.stdout.readline()
        self.assertIn("Bearer", banner)
        self.token = banner.split("Bearer", 1)[1].strip().splitlines()[0]
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        super().tearDown()

    def post(self, payload, token: str | None = None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_needs_no_auth(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=10) as r:
            self.assertTrue(json.loads(r.read())["ok"])

    def test_bad_token_rejected(self):
        status, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              token="wrong")
        self.assertEqual(status, 401)

    def test_missing_token_rejected(self):
        status, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 401)

    def test_roundtrip_over_http(self):
        st, out = self.post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "grok", "version": "1"}}},
            token=self.token)
        self.assertEqual(st, 200)
        self.assertEqual(out["result"]["serverInfo"]["name"], "universal")

        st, out = self.post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "memory_put", "arguments": {
                "ns": "grok", "key": "fact", "value": "from browser"}}},
            token=self.token)
        self.assertEqual(st, 200)
        self.assertFalse(out["result"]["isError"])

        st, out = self.post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "memory_delete", "arguments": {
                "ns": "grok", "key": "fact"}}},
            token=self.token)
        self.assertEqual(st, 200)
        self.assertTrue(self.json_out("get", "grok:fact"),
                        "HTTP delete must not remove the record either")

    def test_concurrent_writes(self):
        import threading
        errors: list[str] = []

        def worker(i: int) -> None:
            st, out = self.post({
                "jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {"name": "memory_put", "arguments": {
                    "ns": "load", "key": f"k{i}", "value": f"v{i}"}}},
                token=self.token)
            if st != 200 or out.get("result", {}).get("isError"):
                errors.append(f"{st} {out}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.json_out("list", "--ns", "load",
                                           "--limit", "1000")), 24)


def load_um():
    """Import the module under test so pure functions can be unit-tested."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("universal_memory", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestOverlap(Base):
    """Which agents have stored the same thing."""

    def store(self):
        um = load_um()
        s = um.Store(self.env["UNIVERSAL_MEMORY_DB"])
        return um, s

    def test_identical_content_across_namespaces(self):
        same = "The voice stack lives in Downloads and uses PaddleSpeech engines"
        self.cli("put", "hermes:stack", same)
        self.cli("put", "opencode:stack", same)
        um, s = self.store()
        try:
            clusters = um.compute_overlap(s)
        finally:
            s.close()
        self.assertTrue(clusters)
        top = clusters[0]
        self.assertEqual(top["reason"], "identical content")
        self.assertEqual(top["score"], 1.0)
        self.assertEqual(sorted(top["namespaces"]), ["hermes", "opencode"])

    def test_same_key_across_namespaces(self):
        self.cli("put", "hermes:rules", "never guess a CLI flag")
        self.cli("put", "codex:rules", "always verify flags in help output")
        um, s = self.store()
        try:
            clusters = um.compute_overlap(s)
        finally:
            s.close()
        reasons = {c["reason"] for c in clusters}
        self.assertIn("same key", reasons)
        cluster = next(c for c in clusters if c["reason"] == "same key")
        self.assertEqual(sorted(cluster["namespaces"]), ["codex", "hermes"])

    def test_similar_wording_across_namespaces(self):
        # different keys, near-identical wording -> only the similarity pass
        # can find this one (the "same key" pass must not be what matches)
        self.cli("put", "hermes:style",
                 "user prefers terse answers with code level detail always")
        self.cli("put", "browser:communication",
                 "user prefers terse answers with code-level detail always")
        um, s = self.store()
        try:
            clusters = um.compute_overlap(s)
        finally:
            s.close()
        reasons = {c["reason"] for c in clusters}
        self.assertIn("very similar wording", reasons)
        near = next(c for c in clusters if c["reason"] == "very similar wording")
        self.assertEqual(sorted(near["namespaces"]), ["browser", "hermes"])
        self.assertGreaterEqual(near["score"], 0.72)

    def test_no_cluster_within_one_namespace(self):
        same = "The voice stack lives in Downloads and uses PaddleSpeech engines"
        self.cli("put", "hermes:a", same)
        self.cli("put", "hermes:b", same)
        um, s = self.store()
        try:
            clusters = um.compute_overlap(s)
        finally:
            s.close()
        self.assertEqual(clusters, [])


class TestWebApi(Base):
    """The dashboard backend."""

    def setUp(self):
        super().setUp()
        cfg = json.loads(
            pathlib.Path(self.env["UNIVERSAL_MEMORY_CONFIG"]).read_text())
        self.token = cfg["http"]["token"]
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [PY, SCRIPT, "web", "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        for _ in range(80):
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        self.fail("web server did not start")

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        super().tearDown()

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token is not None:
            req.add_header("X-UMem-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                return r.status, (json.loads(body) if body[:1] in (b"{", b"[")
                                  else body.decode())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] in (b"{", b"[")
                            else body.decode())

    def post(self, path, payload, token=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        if token is not None:
            req.add_header("X-UMem-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_is_open(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_dashboard_html_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Universal", body)
        self.assertIn("Shared facts", body)
        self.assertIn("Agents", body)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dashboard_javascript_parses(self):
        """A broken inline script would silently blank the whole dashboard."""
        status, html = self.get("/")
        self.assertEqual(status, 200)
        m = re.search(r"<script>(.*)</script>", html, re.S)
        self.assertIsNotNone(m, "dashboard has no script block")
        fd, path = tempfile.mkstemp(suffix=".js")
        os.close(fd)
        try:
            with open(path, "w") as f:
                f.write(m.group(1))
            r = subprocess.run(["node", "--check", path], capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            os.unlink(path)

    def test_dashboard_lists_every_view(self):
        _, html = self.get("/")
        for view in ("overview", "records", "agents", "shared", "activity",
                     "approvals"):
            self.assertIn(f'data-v="{view}"', html)

    def test_api_requires_token(self):
        for path in ("/api/stats", "/api/records", "/api/agents",
                     "/api/overlap", "/api/approvals", "/api/audit"):
            status, _ = self.get(path)
            self.assertEqual(status, 401, path)

    def test_write_endpoints_require_token(self):
        status, _ = self.post("/api/delete-request",
                              {"ns": "shared", "key": "x"})
        self.assertEqual(status, 401)

    def test_stats(self):
        self.cli("put", "hermes:one", "alpha value for stats")
        status, body = self.get("/api/stats", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["records"], 1)
        self.assertEqual(body["namespaces"][0]["ns"], "hermes")
        self.assertIsInstance(body["agents"], list)
        self.assertEqual(body["pending"], 0)

    def test_token_also_accepted_as_query_param(self):
        status, body = self.get(f"/api/stats?t={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("records", body)

    def test_agents_listing_detects_installed_runtimes(self):
        status, body = self.get("/api/agents", self.token)
        self.assertEqual(status, 200)
        by_id = {a["id"]: a for a in body}
        for known in ("hermes", "opencode", "claude-code", "codex"):
            self.assertIn(known, by_id)
        self.assertTrue(by_id["hermes"]["installed"])
        self.assertTrue(by_id["opencode"]["installed"])
        self.assertFalse(by_id["claude-code"]["installed"])
        for a in body:
            self.assertIn("manual", a)
            self.assertIn("connected", a)

    def test_records_and_single_record(self):
        self.cli("put", "hermes:doc", "some remembered text")
        status, body = self.get("/api/records?ns=hermes", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertIn("some remembered text", body["results"][0]["value"])
        status, one = self.get("/api/record?ns=hermes&key=doc", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(one["key"], "doc")

    def test_missing_record_is_404(self):
        status, _ = self.get("/api/record?ns=x&key=nope", self.token)
        self.assertEqual(status, 404)

    def test_search_endpoint(self):
        self.cli("put", "hermes:s", "kangaroo is unusual")
        status, body = self.get("/api/search?q=kangaroo", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)

    def test_delete_request_via_api_does_not_delete(self):
        self.cli("put", "shared:doomed", "keep me around")
        status, body = self.post("/api/delete-request",
                                 {"ns": "shared", "key": "doomed"}, self.token)
        self.assertEqual(status, 200)
        self.assertFalse(body["deleted"])
        self.assertTrue(body["approval_required"])
        self.assertIn("universal-memory approve", body["approve_with"])
        self.assertEqual(self.cli("get", "shared:doomed").returncode, 0)

    def test_approve_via_api_deletes(self):
        self.cli("put", "shared:doomed", "bye")
        _, req = self.post("/api/delete-request",
                           {"ns": "shared", "key": "doomed"}, self.token)
        status, body = self.post("/api/approve", {"token": req["token"]},
                                 self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["approved"])
        self.assertEqual(self.cli("get", "shared:doomed").returncode, 2)

    def test_deny_via_api_keeps_record(self):
        self.cli("put", "shared:kept", "stay")
        _, req = self.post("/api/delete-request",
                           {"ns": "shared", "key": "kept"}, self.token)
        status, body = self.post("/api/deny", {"token": req["token"]}, self.token)
        self.assertEqual(status, 200)
        self.assertFalse(body["approved"])
        self.assertEqual(self.cli("get", "shared:kept").returncode, 0)

    def test_overlap_endpoint(self):
        same = "shared fact about where the voice stack is installed today"
        self.cli("put", "hermes:fact", same)
        self.cli("put", "opencode:fact", same)
        status, body = self.get("/api/overlap", self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertEqual(body[0]["reason"], "identical content")

    def test_connect_unknown_agent_is_404(self):
        status, _ = self.post("/api/agents/nope/connect", {}, self.token)
        self.assertEqual(status, 404)

    def test_connect_is_idempotent_for_connected_agents(self):
        # both installed runtimes on this machine are already wired up
        for agent in ("hermes", "opencode"):
            status, body = self.post(f"/api/agents/{agent}/connect", {},
                                     self.token)
            self.assertEqual(status, 200, agent)
            self.assertTrue(body["connected"], f"{agent}: {body}")

    def test_audit_endpoint(self):
        self.cli("put", "shared:aud", "value")
        status, body = self.get("/api/audit?limit=5", self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body)
        self.assertEqual(body[0]["op"], "put")


class TestSurfaceSplit(Base):
    """`serve` must not carry the dashboard; `web` must not carry MCP — the
    MCP endpoint stays up continuously while the dashboard is started on
    demand, so they cannot share a port."""

    def setUp(self):
        super().setUp()
        self.proc = None

    def tearDown(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        super().tearDown()

    def start(self, *cmd):
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [PY, SCRIPT, *cmd, "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/health", timeout=2) as r:
                    if r.status == 200:
                        return base, json.loads(r.read())
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        self.fail("server did not start")

    @staticmethod
    def hit(base, path, method="GET"):
        req = urllib.request.Request(base + path, method=method,
                                     data=b"{}" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_serve_is_mcp_only(self):
        base, health = self.start("serve")
        self.assertEqual(health["surfaces"], ["mcp"])
        self.assertEqual(health["mcp_endpoint"], "/mcp")
        self.assertIsNone(health["dashboard"])

        status, body = self.hit(base, "/")
        self.assertEqual(status, 404)
        self.assertIn(b"universal-memory web", body)
        status, _ = self.hit(base, "/api/stats")
        self.assertEqual(status, 404)

        # MCP is mounted: it refuses on auth, not on existence
        status, _ = self.hit(base, "/mcp", method="POST")
        self.assertEqual(status, 401)

    def test_web_is_dashboard_only(self):
        base, health = self.start("web")
        self.assertEqual(health["surfaces"], ["dashboard"])
        self.assertIsNone(health["mcp_endpoint"])

        status, body = self.hit(base, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Universal", body)

        status, body = self.hit(base, "/mcp", method="POST")
        self.assertEqual(status, 404)
        self.assertIn(b"universal-memory serve", body)

    def test_both_surfaces_with_flag(self):
        base, health = self.start("serve", "--dashboard")
        self.assertEqual(sorted(health["surfaces"]), ["dashboard", "mcp"])
        self.assertEqual(self.hit(base, "/")[0], 200)
        self.assertEqual(self.hit(base, "/mcp", method="POST")[0], 401)

    def test_default_ports_do_not_collide(self):
        cfg = json.loads(
            pathlib.Path(self.env["UNIVERSAL_MEMORY_CONFIG"]).read_text())
        self.assertNotEqual(int(cfg["http"]["port"]),
                            int(cfg["http"]["web_port"]))

    def test_web_defaults_to_its_own_port(self):
        # no --port: `web` must bind the configured web port, not the MCP one
        port = int(json.loads(
            pathlib.Path(self.env["UNIVERSAL_MEMORY_CONFIG"]).read_text()
        )["http"]["web_port"])
        base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([PY, SCRIPT, "web"], env=self.env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(80):
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    self.assertEqual(r.status, 200)
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        self.fail(f"web did not start on {port}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
