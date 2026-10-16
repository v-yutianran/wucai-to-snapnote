import json
import tempfile
import unittest
import subprocess
import threading
import http.server
import os
import io
import contextlib
from unittest.mock import patch
from pathlib import Path

import backup


class SnapnoteMemory:
    """只用于隔离 IO 测试，不连接真实服务。"""

    def __init__(self):
        self.notes = {}
        self.captures = 0
        self.timeout_after_create = False
        self.reject_readback = False

    def __call__(self, name, arguments):
        if name in ("memo_capture", "memo_create"):
            self.captures += 1
            uuid = f"note-{self.captures}"
            self.notes[uuid] = (arguments["text"][:500] if name == "memo_capture" else
                                "".join(op["insert"] for op in arguments["body"]["delta"]["ops"]))
            if self.timeout_after_create:
                raise backup.BackupError("模拟：服务已创建，但响应丢失")
            return {"note_uuid": uuid}
        if name == "memo_get":
            if self.reject_readback:
                raise backup.BackupError("模拟读回失败")
            text = self.notes[arguments["note_uuid"]]
            plain = "\n".join(line.strip() for line in text.splitlines())
            delta_text = plain if arguments.get("evidence_kind") == "note_body" else text
            return {"item": {"body": {"delta": {"ops": [{"insert": delta_text + "\n"}]},
                                      "plain_text": plain}}}
        if name == "memo_search":
            self.assert_search_arguments(arguments)
            matches = [{"uuid": uuid} for uuid, text in self.notes.items() if arguments["query"] in text]
            offset = arguments["offset"]
            return {"items": matches[offset:offset + 20], "has_more": offset + 20 < len(matches)}
        raise AssertionError(name)

    @staticmethod
    def assert_search_arguments(arguments):
        assert arguments["include_archived"] is True


class ContentAndRecordsTests(unittest.TestCase):
    def test_ids_preserve_int64_and_reject_invalid_values(self):
        self.assertEqual(backup.normalize_id(9223372036854775807), "9223372036854775807")
        self.assertEqual(backup.normalize_id("00123"), "123")
        for value in [None, True, 1.5, "", "1.5", -1, "../123"]:
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.normalize_id(value)

    def test_note_preserves_text_and_actual_annotation_spelling(self):
        highlight = {"h_id": 123, "content": "中文 **原文**\n第二行", "annonation": "原始批注",
                     "tags": "阅读,AI", "image_url": "https://example.org/image.png", "created_at": 0}
        text = backup.render_note(highlight, "cn")
        for expected in [highlight["content"], "原始批注", "阅读,AI", highlight["image_url"],
                         "[wucai:cn:h_id:123]", "1970-01-01 08:00:00"]:
            self.assertIn(expected, text)
        self.assertNotIn("![", text)
        highlight["annotation"] = "规范字段"
        self.assertIn("规范字段", backup.render_note(highlight, "cn"))
        self.assertIn("图片链接", backup.render_note({"h_id": 4, "image_url": "https://example.org/p.png"}, "cn"))
        for invalid in [False, 0, [], {}]:
            with self.subTest(invalid=invalid), self.assertRaises(backup.BackupError):
                backup.render_note({"h_id": 4, "content": invalid}, "cn")

    def test_all_daily_files_are_read_and_corruption_is_not_an_empty_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(backup.load_records(root), set())
            (root / "records").mkdir()
            for day, ids in [("2026-10-15", ["1", "9223372036854775807"]), ("2026-10-16", ["2", "1"])]:
                (root / "records" / f"{day}.json").write_text(json.dumps(ids))
            self.assertEqual(backup.load_records(root), {"1", "2", "9223372036854775807"})
            (root / "records/2026-10-16.json").write_text("broken")
            with self.assertRaises(backup.BackupError):
                backup.load_records(root)


class BackupFlowTests(unittest.TestCase):
    def test_long_notes_preserve_full_text_and_line_edge_spaces_through_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root, remote = Path(directory), SnapnoteMemory()
            highlight = {"h_id": 1, "content": "  保留前后空格  \n" + "长原文" * 300}
            backup.backup_items(root, [highlight], "cn", remote, lambda: "2026-10-16")
            expected = backup.render_note(highlight, "cn")
            self.assertEqual(backup.note_text(remote, "note-1").rstrip("\n"), expected)
            self.assertEqual(backup.load_records(root), {"1"})

    def test_note_readback_accepts_the_live_item_envelope(self):
        text = backup.render_note({"h_id": 1, "content": "完整正文"}, "cn")
        response = {"item": {"body": {"plain_text": text}}, "count": 1, "note_uuid": "note-1"}
        self.assertEqual(backup.note_text(lambda name, arguments: response, "note-1"), text)

    def test_wucai_explicit_null_highlights_are_accepted_only_on_the_terminal_page(self):
        def pages(params):
            return {"highlights": [{"h_id": 10}], "next_cursor": "last"} if not params["cursor"] else {
                "highlights": None, "next_cursor": ""}
        self.assertEqual([item["h_id"] for item in backup.fetch_highlights(pages)], [10])
        for invalid in [{"highlights": None, "next_cursor": "not-terminal"}, {"next_cursor": ""}]:
            with self.subTest(invalid=invalid), self.assertRaises(backup.BackupError):
                backup.fetch_highlights(lambda _: invalid)

    def test_repeated_search_pages_stop_instead_of_looping(self):
        searches = 0

        def call(name, arguments):
            nonlocal searches
            if name == "memo_get":
                return {"body": {"plain_text": "[wucai:cn:h_id:1]"}}
            searches += 1
            self.assertLessEqual(searches, 2, "搜索未检测重复页")
            return {"items": [{"uuid": "same-note"}], "has_more": True}

        with self.assertRaises(backup.BackupError):
            backup.find_note(call, "1", "cn")

    def test_complete_pagination_deduplicates_and_rejects_cursor_cycles(self):
        def pages(params):
            self.assertEqual(params["range"], "7d")
            return {"highlights": [{"h_id": 1}, {"h_id": 2}], "next_cursor": "next"} if not params["cursor"] else {
                "highlights": [{"h_id": "1"}, {"h_id": 3}], "next_cursor": ""}
        self.assertEqual([x["h_id"] for x in backup.fetch_highlights(pages)], [1, 2, 3])
        with self.assertRaises(backup.BackupError):
            backup.fetch_highlights(lambda _: {"highlights": [], "next_cursor": "same"})

    def test_success_records_only_confirmed_ids_and_second_run_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root, remote = Path(directory), SnapnoteMemory()
            items = [{"h_id": 1, "content": "原文"}, {"h_id": 2, "annonation": "批注"}]
            stats = backup.backup_items(root, items, "cn", remote, lambda: "2026-10-16")
            self.assertEqual(stats["created"], 2)
            self.assertEqual(json.loads((root / "records/2026-10-16.json").read_text()), ["1", "2"])
            again = backup.backup_items(root, items, "cn", remote, lambda: "2026-10-17")
            self.assertEqual(again["skipped"], 2)
            self.assertEqual(remote.captures, 2)
            self.assertFalse((root / "records/2026-10-17.json").exists())

    def test_timeout_is_recovered_even_after_highlight_leaves_seven_day_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root, remote = Path(directory), SnapnoteMemory()
            remote.timeout_after_create = True
            with self.assertRaises(backup.BackupError):
                backup.backup_items(root, [{"h_id": 1, "content": "原文"}], "cn", remote, lambda: "2026-10-16")
            self.assertEqual(backup.load_records(root), set())
            self.assertTrue((root / ".runtime/pending.json").exists())
            result = backup.backup_items(root, [], "cn", remote, lambda: "2026-10-17")
            self.assertEqual(result["recovered"], 1)
            self.assertEqual(remote.captures, 1)
            self.assertEqual(json.loads((root / "records/2026-10-17.json").read_text()), ["1"])
            self.assertFalse((root / ".runtime/pending.json").exists())

    def test_missing_ambiguous_and_changed_remote_notes_never_trigger_recreation(self):
        for problem in ["missing", "multiple", "changed"]:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                root, remote = Path(directory), SnapnoteMemory()
                text = backup.render_note({"h_id": 5, "content": "预期正文"}, "cn")
                backup.atomic_json(root / ".runtime/pending.json", {
                    "h_id": "5", "region": "cn", "digest": backup.body_digest(text), "note_uuid": None})
                if problem == "multiple":
                    remote.notes = {"a": text, "b": text}
                elif problem == "changed":
                    remote.notes = {"a": text + "内容被修改"}
                with self.assertRaises(backup.BackupError):
                    backup.backup_items(root, [{"h_id": 5, "content": "预期正文"}], "cn", remote, lambda: "2026-10-16")
                self.assertEqual(remote.captures, 0)
                self.assertEqual(backup.load_records(root), set())

    def test_readback_failure_preserves_uuid_without_logging_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root, remote = Path(directory), SnapnoteMemory()
            remote.reject_readback = True
            with self.assertRaises(backup.BackupError):
                backup.backup_items(root, [{"h_id": 6}], "cn", remote, lambda: "2026-10-16")
            pending = json.loads((root / ".runtime/pending.json").read_text())
            self.assertEqual(pending["note_uuid"], "note-1")
            self.assertEqual(backup.load_records(root), set())
            remote.reject_readback = False
            backup.backup_items(root, [], "cn", remote, lambda: "2026-10-17")
            self.assertEqual(remote.captures, 1)

    def test_record_write_failure_recovers_without_recreating_remote_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root, remote = Path(directory), SnapnoteMemory()
            write = backup.atomic_json

            def fail_records(file, value):
                if file.parent.name == "records":
                    raise OSError("模拟记录写入失败")
                write(file, value)

            with patch("backup.atomic_json", side_effect=fail_records), self.assertRaises(OSError):
                backup.backup_items(root, [{"h_id": 7}], "cn", remote, lambda: "2026-10-16")
            self.assertEqual(backup.load_records(root), set())
            backup.backup_items(root, [], "cn", remote, lambda: "2026-10-17")
            self.assertEqual(remote.captures, 1)
            self.assertEqual(backup.load_records(root), {"7"})


class HttpAndBoundaryTests(unittest.TestCase):
    def test_native_http_rpc_validates_ack_and_does_not_expose_server_errors(self):
        replies = [
            {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"note_uuid": "created"}}},
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "DO-NOT-LOG-SECRET"}},
            {"jsonrpc": "2.0", "id": 9, "result": {"structuredContent": {}}},
            {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "structuredContent": {}}},
        ]
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                test.assertEqual(self.headers["Authorization"], "Bearer fake-token")
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.assertEqual(request["params"]["name"], "snapnote.memo_capture")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(replies.pop(0)).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            self.assertEqual(backup.rpc(url, "fake-token", "memo_capture", {"text": "合成内容"})["note_uuid"], "created")
            for _ in range(3):
                with self.assertRaises(backup.BackupError) as error:
                    backup.rpc(url, "fake-token", "memo_capture", {})
                self.assertNotIn("DO-NOT-LOG-SECRET", str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_skill_receives_only_the_new_environment_token_via_child_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "skill.py"
            script.write_text("import os,json,sys\np=json.load(sys.stdin)\nassert p['range']=='7d'\n"
                              "assert os.environ['WUCAI_API_TOKEN']=='wct-synthetic-token'\n"
                              "print(json.dumps({'code':1,'data':{'highlights':[],'next_cursor':''}}))\n")
            with patch.dict(os.environ, {"WUCAI_API_TOKEN": "legacy-must-not-win"}):
                data = backup.skill_page({"range": "7d"}, "wct-synthetic-token", script)
            self.assertEqual(data, {"highlights": [], "next_cursor": ""})

    def test_atomic_write_failure_preserves_previous_records_and_running_lock_blocks_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "records/2026-10-16.json"
            backup.atomic_json(file, ["1"])
            with patch("backup.os.replace", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    backup.atomic_json(file, ["1", "2"])
            self.assertEqual(json.loads(file.read_text()), ["1"])
            with backup.run_lock(root):
                with self.assertRaises(backup.BackupError):
                    with backup.run_lock(root):
                        self.fail("second lock acquired")


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root, self.remote = base / "work", base / "remote.git"
        self.root.mkdir()
        self.command(base, "git", "init", "--bare", str(self.remote))
        self.command(self.root, "git", "init", "-b", "main")
        self.command(self.root, "git", "config", "user.name", "Synthetic Test")
        self.command(self.root, "git", "config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text(".runtime/\n")
        (self.root / "README.md").write_text("Synthetic test repository\n")
        self.command(self.root, "git", "add", "--", ".gitignore", "README.md")
        self.command(self.root, "git", "commit", "-m", "test: synthetic baseline")
        self.command(self.root, "git", "remote", "add", "origin", str(self.remote))
        self.command(self.root, "git", "push", "-u", "origin", "main")

    @staticmethod
    def command(root, *args):
        return subprocess.run(args, cwd=root, text=True, capture_output=True, check=True).stdout.strip()

    def test_records_are_pushed_and_unrelated_changes_are_rejected(self):
        backup.atomic_json(self.root / "records/2026-10-16.json", ["1"])
        backup.sync_records(self.root, str(self.remote))
        remote = self.command(self.root, "git", "--git-dir", str(self.remote), "show", "main:records/2026-10-16.json")
        self.assertEqual(json.loads(remote), ["1"])
        head = self.command(self.root, "git", "rev-parse", "HEAD")
        backup.sync_records(self.root, str(self.remote))
        self.assertEqual(self.command(self.root, "git", "rev-parse", "HEAD"), head)
        backup.atomic_json(self.root / "records/2026-10-16.json", ["1", "2"])
        backup.sync_records(self.root, str(self.remote))
        self.assertEqual(json.loads(self.command(self.root, "git", "--git-dir", str(self.remote),
                                               "show", "main:records/2026-10-16.json")), ["1", "2"])
        (self.root / "README.md").write_text("unrelated local change")
        with self.assertRaises(backup.BackupError):
            backup.git_preflight(self.root, str(self.remote))

    def test_push_failure_keeps_local_confirmation_and_next_run_pushes_it_first(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        backup.atomic_json(self.root / "records/2026-10-16.json", ["2"])
        with self.assertRaises(backup.BackupError):
            backup.sync_records(self.root, str(self.remote))
        self.assertEqual(backup.load_records(self.root), {"2"})
        hook.write_text("#!/bin/sh\nexit 0\n")
        backup.sync_records(self.root, str(self.remote))
        self.assertEqual(self.command(self.root, "git", "rev-parse", "HEAD"),
                         self.command(self.root, "git", "--git-dir", str(self.remote), "rev-parse", "main"))

    def test_unpushed_code_commit_is_blocked(self):
        (self.root / "README.md").write_text("new code behavior")
        self.command(self.root, "git", "add", "--", "README.md")
        self.command(self.root, "git", "commit", "-m", "test: unrelated code")
        with self.assertRaises(backup.BackupError):
            backup.git_preflight(self.root, str(self.remote))

    def test_github_https_rewritten_to_ssh_keeps_the_same_repository_identity(self):
        original_git = backup.git
        actual_url = "git@github.com:v-yutianran/wucai-to-snapnote.git"

        def rewritten_git(root, *arguments):
            if arguments in [("remote", "get-url", "origin"), ("remote", "get-url", "--push", "--all", "origin")]:
                return actual_url
            return original_git(root, *arguments)

        with patch("backup.git", side_effect=rewritten_git):
            self.assertTrue(backup.git_preflight(self.root))
            actual_url = "git@github.com:another-owner/wucai-to-snapnote.git"
            with self.assertRaises(backup.BackupError):
                backup.git_preflight(self.root)

    def test_separate_push_destination_must_match_the_authorized_repository(self):
        self.command(self.root, "git", "config", "remote.origin.pushurl", str(self.root / "another-remote.git"))
        with self.assertRaises(backup.BackupError):
            backup.git_preflight(self.root, str(self.remote))

    def test_record_deletion_is_blocked(self):
        file = self.root / "records/2026-10-16.json"
        backup.atomic_json(file, ["1"])
        backup.sync_records(self.root, str(self.remote))
        file.unlink()
        with self.assertRaises(backup.BackupError):
            backup.git_preflight(self.root, str(self.remote))
        self.command(self.root, "git", "add", "--", "records/2026-10-16.json")
        self.command(self.root, "git", "commit", "-m", "test: synthetic deletion")
        with self.assertRaises(backup.BackupError):
            backup.git_preflight(self.root, str(self.remote))

    def test_real_cli_dry_run_and_two_successive_runs_with_isolated_http_and_git(self):
        remote = SnapnoteMemory()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name, args = req["params"]["name"].split(".")[-1], req["params"]["arguments"]
                content = {"service_key": "snapnote.data_source", "service_version": "v1alpha1"} if name == "service_profile" else remote(name, args)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": content}}).encode())

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = Path(self.temporary.name) / "fake_skill.py"
        script.write_text("import json,sys,os\np=json.load(sys.stdin)\nassert os.environ['WUCAI_API_TOKEN']=='wct-synthetic-token'\n"
                          "data={'highlights':[{'h_id':1,'content':'合成正文'}],'next_cursor':'next'} if not p['cursor'] else "
                          "{'highlights':[{'h_id':1},{'h_id':2,'annonation':'合成批注'}],'next_cursor':''}\n"
                          "print(json.dumps({'code':1,'data':data}))\n")
        env = {"WUCAI_TOKEN": "wct-synthetic-token", "SNAPNOTE_MCP_TOKEN": "synthetic-snapnote-token",
               "SNAPNOTE_MCP_URL": f"http://127.0.0.1:{server.server_port}/mcp"}
        with patch.dict(os.environ, env), patch("backup.SKILL_SCRIPT", script), patch("backup.REMOTE", str(self.remote)):
            head = self.command(self.root, "git", "rev-parse", "HEAD")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(backup.main(["--dry-run"], root=self.root), 0)
            self.assertEqual(remote.captures, 0)
            self.assertFalse((self.root / ".runtime").exists())
            self.assertFalse((self.root / "records").exists())
            self.assertEqual(self.command(self.root, "git", "rev-parse", "HEAD"), head)
            with contextlib.redirect_stdout(output):
                self.assertEqual(backup.main([], root=self.root), 0)
            self.assertEqual(remote.captures, 2)
            self.assertEqual(backup.load_records(self.root), {"1", "2"})
            synced_head = self.command(self.root, "git", "rev-parse", "HEAD")
            with contextlib.redirect_stdout(output):
                self.assertEqual(backup.main([], root=self.root), 0)
            self.assertEqual(remote.captures, 2)
            self.assertEqual(self.command(self.root, "git", "rev-parse", "HEAD"), synced_head)
            self.assertNotIn("wct-synthetic-token", output.getvalue())
            self.assertNotIn("synthetic-snapnote-token", output.getvalue())
            self.assertNotIn("合成正文", output.getvalue())
            self.assertNotIn("合成批注", output.getvalue())
        thread.join(timeout=2)


class SettingsTests(unittest.TestCase):
    def test_wucai_token_is_required_even_when_legacy_token_exists(self):
        with self.assertRaises(backup.BackupError):
            backup.settings({"WUCAI_API_TOKEN": "wct-legacy", "SNAPNOTE_MCP_TOKEN": "other"})
        values = backup.settings({"WUCAI_TOKEN": "wct-synthetic", "SNAPNOTE_MCP_TOKEN": "other",
                                  "SNAPNOTE_MCP_URL": "https://example.invalid/mcp"})
        self.assertEqual(values, ("wct-synthetic", "https://example.invalid/mcp", "other"))
        for url in ["https://user:password@example.invalid/mcp", "file:///tmp/mcp", "https://example.invalid/mcp?token=secret"]:
            with self.subTest(url=url), self.assertRaises(backup.BackupError):
                backup.settings({"WUCAI_TOKEN": "wct-synthetic", "SNAPNOTE_MCP_TOKEN": "other", "SNAPNOTE_MCP_URL": url})


if __name__ == "__main__":
    unittest.main()
