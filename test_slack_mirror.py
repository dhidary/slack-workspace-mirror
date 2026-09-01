import json
import tempfile
import unittest
from pathlib import Path

from slack_mirror import (
    LocalArchive,
    Mirror,
    State,
    neutralize_special_mentions,
    slugify,
)


class FakeSource:
    token = "source-token"

    def users_conversations(self, **kwargs):
        return {
            "channels": [
                {"id": "D1", "is_im": True, "user": "U2"},
                {"id": "C1", "is_im": False, "is_mpim": False, "name": "general"},
            ],
            "response_metadata": {"next_cursor": ""},
        }

    def users_info(self, user):
        return {"user": {"id": user, "profile": {"display_name": "Example User"}}}

    def conversations_history(self, channel, **kwargs):
        return {
            "messages": [
                {"ts": "1.0", "user": "U2", "text": f"hello from {channel}"}
            ],
            "response_metadata": {"next_cursor": ""},
        }


class FakeDestination:
    def __init__(self, hidden_names=None):
        self.created = []
        self.posts = []
        self.updates = []
        self.uploads = []
        self.hidden_names = set(hidden_names or [])

    def users_conversations(self, **kwargs):
        return {
            "channels": [{"id": "C_EXISTING", "name_normalized": "general"}],
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_create(self, name, is_private):
        if name in self.hidden_names:
            self.hidden_names.remove(name)
            error = RuntimeError("name_taken")
            error.response = {"error": "name_taken"}
            raise error
        self.created.append((name, is_private))
        return {"channel": {"id": f"G{len(self.created)}"}}

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"2.{len(self.posts)}"}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    def files_upload_v2(self, **kwargs):
        self.uploads.append((kwargs, Path(kwargs["file"]).read_bytes()))
        return {"file": {"id": f"F{len(self.uploads)}"}}


class InaccessibleConversationSource(FakeSource):
    def conversations_history(self, channel, **kwargs):
        if channel == "C1":
            error = RuntimeError("channel_not_found")
            error.response = {"error": "channel_not_found"}
            raise error
        return super().conversations_history(channel, **kwargs)


class MutableSource(FakeSource):
    def __init__(self):
        self.message = {"ts": "1.0", "user": "U2", "text": "original"}

    def conversations_history(self, channel, **kwargs):
        return {
            "messages": [dict(self.message)],
            "response_metadata": {"next_cursor": ""},
        }


class ThreadSource(FakeSource):
    def __init__(self):
        self.history_visible = True
        self.root = {
            "ts": "1.0",
            "user": "U2",
            "text": "root",
            "reply_count": 1,
        }
        self.replies = [
            self.root,
            {"ts": "2.0", "thread_ts": "1.0", "user": "U2", "text": "first"},
        ]

    def conversations_history(self, channel, **kwargs):
        return {
            "messages": [dict(self.root)] if self.history_visible else [],
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_replies(self, channel, ts, **kwargs):
        return {
            "messages": [dict(message) for message in self.replies],
            "response_metadata": {"next_cursor": ""},
        }


class SlackMirrorTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Example User"), "example-user")
        self.assertEqual(
            slugify("Group Example User, Second User"),
            "group-example-user-second-user",
        )

    def test_special_mentions_are_neutralized(self):
        result = neutralize_special_mentions("Hi <!channel> <!here> <!everyone>")
        self.assertNotIn("<!", result)
        self.assertIn("@\u200bchannel", result)

    def test_html_text_decodes_slack_quotes_without_allowing_html(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = LocalArchive(
                FakeSource(),
                State(Path(directory) / "state.sqlite3"),
                Path(directory) / "archive",
                source_team_name="Source Workspace",
                source_user_id="ME",
            )
            rendered = mirror.html_text(
                "&gt; quoted text\nYes <script>\n<mailto:test@example.com|test@example.com>"
            )
            self.assertIn("<blockquote>quoted text</blockquote>", rendered)
            self.assertIn("<div>Yes &lt;script&gt;</div>", rendered)
            self.assertIn('href="mailto:test@example.com"', rendered)
            self.assertNotIn("&amp;gt;", rendered)

    def test_rich_text_renders_formatting_lists_quotes_and_email_links(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = LocalArchive(
                FakeSource(),
                State(Path(directory) / "state.sqlite3"),
                Path(directory) / "archive",
                source_team_name="Source Workspace",
                source_user_id="ME",
            )
            message = {
                "text": "fallback",
                "blocks": [{
                    "type": "rich_text",
                    "elements": [
                        {"type": "rich_text_section", "elements": [
                            {"type": "text", "text": "Email ", "style": {"bold": True}},
                            {"type": "link", "url": "mailto:test@example.com", "text": "test@example.com"},
                        ]},
                        {"type": "rich_text_quote", "elements": [
                            {"type": "text", "text": "quoted", "style": {"strike": True}},
                        ]},
                        {"type": "rich_text_list", "style": "ordered", "elements": [
                            {"type": "rich_text_section", "elements": [
                                {"type": "text", "text": "first"},
                            ]},
                        ]},
                    ],
                }],
            }
            rendered = mirror.message_html(message)
            self.assertIn("<strong>Email </strong>", rendered)
            self.assertIn('href="mailto:test@example.com"', rendered)
            self.assertIn("<blockquote><s>quoted</s></blockquote>", rendered)
            self.assertIn("<ol><li>first</li></ol>", rendered)

    def test_attachment_only_message_has_no_empty_text_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conversation_dir = root / "archive" / "example-user"
            (conversation_dir / "messages").mkdir(parents=True)
            (conversation_dir / "files").mkdir()
            (conversation_dir / "conversation.json").write_text(json.dumps({
                "source_workspace": "Source Workspace",
                "source_conversation": "D1",
                "label": "Example User",
            }))
            file_info = {"id": "F1", "name": "image.png", "mimetype": "image/png"}
            (conversation_dir / "files" / "F1-image.png").write_bytes(b"image")
            (conversation_dir / "messages" / "1_0.json").write_text(json.dumps({
                "author": "Example User",
                "message": {"ts": "1.0", "text": "", "files": [file_info]},
            }))
            mirror = LocalArchive(
                FakeSource(),
                State(root / "state.sqlite3"),
                root / "archive",
                source_team_name="Source Workspace",
                source_user_id="ME",
            )
            mirror.write_conversation_html(conversation_dir)
            rendered = (conversation_dir / "index.html").read_text()
            self.assertNotIn("No text", rendered)
            self.assertIn("<img", rendered)

    def test_state_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.sqlite3")
            state.save_conversation("D1", "G1", "example-user", "Example User")
            state.save_message("D1", "1.0", None, "G1", "2.0")
            state.save_message("D1", "1.0", None, "G1", "2.0")
            self.assertEqual(
                state.conversation("D1")["destination_name"], "example-user"
            )
            self.assertEqual(state.message("D1", "1.0")["destination_ts"], "2.0")

    def test_sync_routes_dm_and_avoids_existing_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.sqlite3")
            destination = FakeDestination()
            mirror = Mirror(
                FakeSource(),
                destination,
                state,
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            mirror.sync_all()
            self.assertEqual(
                destination.created,
                [("example-user", True), ("general-backup", True)],
            )
            self.assertEqual(len(destination.posts), 2)
            mirror.sync_all()
            self.assertEqual(len(destination.posts), 2)

    def test_hidden_destination_name_collision_uses_backup_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.sqlite3")
            destination = FakeDestination(hidden_names={"example-user"})
            mirror = Mirror(
                FakeSource(),
                destination,
                state,
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            mirror.sync_all()
            self.assertIn(("example-user-backup", True), destination.created)

    def test_inaccessible_conversation_does_not_stop_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            mirror = Mirror(
                InaccessibleConversationSource(),
                destination,
                State(Path(directory) / "state.sqlite3"),
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            mirror.sync_all()
            self.assertEqual(len(destination.posts), 1)

    def test_refresh_updates_an_edited_message_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = MutableSource()
            destination = FakeDestination()
            mirror = Mirror(
                source,
                destination,
                State(Path(directory) / "state.sqlite3"),
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            conversation = {"id": "D1", "is_im": True, "user": "U2"}
            mirror.sync_conversation(conversation, refresh=True)
            source.message["text"] = "edited"
            source.message["edited"] = {"ts": "3.0", "user": "U2"}
            mirror.sync_conversation(conversation, refresh=True)
            mirror.sync_conversation(conversation, refresh=True)
            self.assertEqual(len(destination.posts), 1)
            self.assertEqual(len(destination.updates), 1)
            self.assertIn("edited", destination.updates[0]["text"])

    def test_refresh_recovers_reply_to_known_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            source = ThreadSource()
            destination = FakeDestination()
            mirror = Mirror(
                source,
                destination,
                State(Path(directory) / "state.sqlite3"),
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            conversation = {"id": "D1", "is_im": True, "user": "U2"}
            mirror.sync_conversation(conversation, refresh=True)
            source.history_visible = False
            source.replies.append(
                {"ts": "3.0", "thread_ts": "1.0", "user": "U2", "text": "later"}
            )
            mirror.sync_conversation(conversation, refresh=True)
            self.assertEqual(len(destination.posts), 3)
            self.assertEqual(destination.posts[-1]["thread_ts"], "2.1")

    def test_file_is_downloaded_uploaded_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "image.png"
            source_file.write_bytes(b"fake-png")
            state = State(Path(directory) / "state.sqlite3")
            destination = FakeDestination()
            mirror = Mirror(
                FakeSource(),
                destination,
                state,
                source_team_name="Source Workspace",
                source_user_id="ME",
                post_interval=0,
            )
            file_info = {
                "id": "SOURCE_FILE",
                "name": "image.png",
                "url_private_download": source_file.as_uri(),
            }
            mirror.copy_file("D1", "1.0", file_info, "G1", "2.0", "Example User")
            mirror.copy_file("D1", "1.0", file_info, "G1", "2.0", "Example User")
            self.assertEqual(len(destination.uploads), 1)
            self.assertEqual(destination.uploads[0][1], b"fake-png")

    def test_local_archive_writes_messages_and_deletion_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mirror = LocalArchive(
                FakeSource(),
                State(root / "state.sqlite3"),
                root / "archive",
                source_team_name="Source Workspace",
                source_user_id="ME",
            )
            mirror.sync_all()
            message_path = (
                root / "archive" / "example-user" / "messages" / "1_0.json"
            )
            record = json.loads(message_path.read_text())
            self.assertEqual(record["message"]["text"], "hello from D1")
            transcript = root / "archive" / "example-user" / "index.html"
            self.assertIn("Example User", transcript.read_text())
            self.assertIn("hello from D1", transcript.read_text())
            self.assertIn('id="message-search"', transcript.read_text())
            archive_index = (root / "archive" / "index.html").read_text()
            self.assertIn("example-user/index.html", archive_index)
            self.assertIn('id="conversation-search"', archive_index)

            mirror.handle_event(
                {
                    "type": "message",
                    "channel": "D1",
                    "subtype": "message_deleted",
                    "deleted_ts": "1.0",
                }
            )
            self.assertTrue(json.loads(message_path.read_text())["deleted_in_source"])
            self.assertIn("deleted in source", transcript.read_text())

    def test_local_archive_downloads_and_deduplicates_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_file = root / "image.png"
            source_file.write_bytes(b"fake-png")
            conversation_dir = root / "archive" / "example-user"
            (conversation_dir / "files").mkdir(parents=True)
            mirror = LocalArchive(
                FakeSource(),
                State(root / "state.sqlite3"),
                root / "archive",
                source_team_name="Source Workspace",
                source_user_id="ME",
            )
            file_info = {
                "id": "SOURCE_FILE",
                "name": "image.png",
                "url_private_download": source_file.as_uri(),
            }
            mirror.copy_file(
                "D1", "1.0", file_info, str(conversation_dir), "1.0", "Example User"
            )
            mirror.copy_file(
                "D1", "1.0", file_info, str(conversation_dir), "1.0", "Example User"
            )
            (conversation_dir / "messages").mkdir()
            (conversation_dir / "conversation.json").write_text(
                json.dumps(
                    {
                        "source_workspace": "Source Workspace",
                        "source_conversation": "D1",
                        "label": "Example User",
                    }
                )
            )
            (conversation_dir / "messages" / "1_0.json").write_text(
                json.dumps(
                    {
                        "source_workspace": "Source Workspace",
                        "source_conversation": "D1",
                        "author": "Example User",
                        "message": {
                            "ts": "1.0",
                            "text": "See image",
                            "files": [file_info],
                        },
                    }
                )
            )
            mirror.write_conversation_html(conversation_dir)
            files = list((conversation_dir / "files").iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"fake-png")
            transcript = (conversation_dir / "index.html").read_text()
            self.assertIn("<img", transcript)
            self.assertIn(f'files/{files[0].name}', transcript)


if __name__ == "__main__":
    unittest.main()
