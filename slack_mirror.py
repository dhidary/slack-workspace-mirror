#!/usr/bin/env python3
"""Continuously back up Slack conversations to another Slack or a local folder."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


LOG = logging.getLogger("slack-mirror")
DEFAULT_TYPES = "public_channel,private_channel,im,mpim"


def slugify(value: str, fallback: str = "slack-chat") -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"[-_]{2,}", "-", value).strip("-_")
    return (value or fallback)[:80].rstrip("-_")


def slack_date(ts: str) -> str:
    unix = int(float(ts))
    fallback = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(unix))
    return f"<!date^{unix}^{{date_short_pretty}} at {{time}}|{fallback}>"


def neutralize_special_mentions(text: str) -> str:
    for name in ("channel", "everyone", "here"):
        text = text.replace(f"<!{name}>", f"@\u200b{name}")
    return text


def message_fingerprint(message: dict[str, Any]) -> str:
    serialized = json.dumps(
        message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_map (
                    source_channel TEXT PRIMARY KEY,
                    destination_channel TEXT NOT NULL UNIQUE,
                    destination_name TEXT NOT NULL,
                    label TEXT NOT NULL,
                    last_history_ts TEXT
                );
                CREATE TABLE IF NOT EXISTS message_map (
                    source_channel TEXT NOT NULL,
                    source_ts TEXT NOT NULL,
                    source_thread_ts TEXT,
                    destination_channel TEXT NOT NULL,
                    destination_ts TEXT NOT NULL,
                    source_fingerprint TEXT,
                    PRIMARY KEY (source_channel, source_ts)
                );
                CREATE TABLE IF NOT EXISTS file_map (
                    source_channel TEXT NOT NULL,
                    source_ts TEXT NOT NULL,
                    source_file_id TEXT NOT NULL,
                    destination_file_id TEXT,
                    PRIMARY KEY (source_channel, source_ts, source_file_id)
                );
                """
            )
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(message_map)")
            }
            if "source_fingerprint" not in columns:
                self.connection.execute(
                    "ALTER TABLE message_map ADD COLUMN source_fingerprint TEXT"
                )

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def validate_workspace(self, key: str, value: str) -> None:
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            if row and row["value"] != value:
                raise RuntimeError(
                    f"The database belongs to a different {key.replace('_', ' ')}. "
                    "Use a new SLACK_MIRROR_DB path."
                )
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
            )

    def conversation(self, source_channel: str) -> sqlite3.Row | None:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM conversation_map WHERE source_channel = ?",
                (source_channel,),
            ).fetchone()

    def conversations(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM conversation_map ORDER BY label COLLATE NOCASE"
            ).fetchall()

    def source_for_destination(self, destination_channel: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT source_channel FROM conversation_map WHERE destination_channel = ?",
                (destination_channel,),
            ).fetchone()
            return row["source_channel"] if row else None

    def save_conversation(
        self, source_channel: str, destination_channel: str, name: str, label: str
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO conversation_map(
                    source_channel, destination_channel, destination_name, label
                ) VALUES (?, ?, ?, ?)
                """,
                (source_channel, destination_channel, name, label),
            )

    def update_history_ts(self, source_channel: str, ts: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE conversation_map SET last_history_ts = ? WHERE source_channel = ?",
                (ts, source_channel),
            )

    def message(self, source_channel: str, source_ts: str) -> sqlite3.Row | None:
        with self.lock:
            return self.connection.execute(
                """
                SELECT * FROM message_map
                WHERE source_channel = ? AND source_ts = ?
                """,
                (source_channel, source_ts),
            ).fetchone()

    def thread_roots(self, source_channel: str) -> set[str]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT DISTINCT source_thread_ts FROM message_map
                WHERE source_channel = ? AND source_thread_ts IS NOT NULL
                """,
                (source_channel,),
            ).fetchall()
            return {row["source_thread_ts"] for row in rows}

    def save_message(
        self,
        source_channel: str,
        source_ts: str,
        source_thread_ts: str | None,
        destination_channel: str,
        destination_ts: str,
        source_fingerprint: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO message_map(
                    source_channel, source_ts, source_thread_ts,
                    destination_channel, destination_ts, source_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_channel, source_ts) DO UPDATE SET
                    source_thread_ts = excluded.source_thread_ts,
                    source_fingerprint = COALESCE(
                        excluded.source_fingerprint, message_map.source_fingerprint
                    )
                """,
                (
                    source_channel,
                    source_ts,
                    source_thread_ts,
                    destination_channel,
                    destination_ts,
                    source_fingerprint,
                ),
            )

    def file_done(self, source_channel: str, source_ts: str, file_id: str) -> bool:
        with self.lock:
            return (
                self.connection.execute(
                    """
                    SELECT 1 FROM file_map
                    WHERE source_channel = ? AND source_ts = ? AND source_file_id = ?
                    """,
                    (source_channel, source_ts, file_id),
                ).fetchone()
                is not None
            )

    def save_file(
        self,
        source_channel: str,
        source_ts: str,
        source_file_id: str,
        destination_file_id: str | None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO file_map(
                    source_channel, source_ts, source_file_id, destination_file_id
                ) VALUES (?, ?, ?, ?)
                """,
                (source_channel, source_ts, source_file_id, destination_file_id),
            )


class Mirror:
    def __init__(
        self,
        source: Any,
        destination: Any,
        state: State,
        source_team_name: str,
        source_user_id: str,
        conversation_types: str = DEFAULT_TYPES,
        channel_prefix: str = "",
        post_interval: float = 1.05,
        dry_run: bool = False,
    ):
        self.source = source
        self.destination = destination
        self.state = state
        self.source_team_name = source_team_name
        self.source_user_id = source_user_id
        self.conversation_types = conversation_types
        self.channel_prefix = channel_prefix
        self.post_interval = post_interval
        self.dry_run = dry_run
        self.user_cache: dict[str, dict[str, Any]] = {}
        self.destination_channels: dict[str, str] | None = None
        self.copy_lock = threading.RLock()
        self.last_post = 0.0

    @staticmethod
    def paginate(
        client: Any, method_name: str, item_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            response = getattr(client, method_name)(cursor=cursor, limit=200, **kwargs)
            items.extend(response.get(item_key, []))
            cursor = response.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return items

    def conversations(self) -> list[dict[str, Any]]:
        return self.paginate(
            self.source,
            "users_conversations",
            "channels",
            types=self.conversation_types,
            exclude_archived=False,
        )

    def user(self, user_id: str) -> dict[str, Any]:
        if user_id not in self.user_cache:
            try:
                self.user_cache[user_id] = self.source.users_info(user=user_id)["user"]
            except Exception:
                self.user_cache[user_id] = {"id": user_id, "name": user_id}
        return self.user_cache[user_id]

    def user_name(self, user_id: str) -> str:
        user = self.user(user_id)
        profile = user.get("profile", {})
        return (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )

    def conversation_label(self, conversation: dict[str, Any]) -> str:
        if conversation.get("is_im"):
            return self.user_name(conversation["user"])
        if conversation.get("is_mpim"):
            members = self.paginate(
                self.source,
                "conversations_members",
                "members",
                channel=conversation["id"],
            )
            names = sorted(
                self.user_name(member)
                for member in members
                if member != self.source_user_id
            )
            return "Group " + ", ".join(names or [conversation["id"]])
        return conversation.get("name") or conversation["id"]

    def desired_channel_name(self, conversation: dict[str, Any], label: str) -> str:
        if conversation.get("is_mpim"):
            label = "group-" + label.removeprefix("Group ").replace(",", "-")
        return slugify(f"{self.channel_prefix}{label}")

    def load_destination_channels(self) -> dict[str, str]:
        if self.destination_channels is None:
            channels = self.paginate(
                self.destination,
                "users_conversations",
                "channels",
                types="public_channel,private_channel",
                exclude_archived=False,
            )
            self.destination_channels = {
                channel["name_normalized"]: channel["id"] for channel in channels
            }
        return self.destination_channels

    def destination_channel(self, conversation: dict[str, Any]) -> str:
        source_id = conversation["id"]
        mapped = self.state.conversation(source_id)
        if mapped:
            return mapped["destination_channel"]

        label = self.conversation_label(conversation)
        base_name = self.desired_channel_name(conversation, label)
        if self.dry_run:
            LOG.info("%s -> #%s", label, base_name)
            return f"dry-run:{base_name}"

        channels = self.load_destination_channels()
        name = base_name
        suffix = 0
        while True:
            destination_id = channels.get(name)
            if destination_id is None:
                try:
                    response = self.destination.conversations_create(
                        name=name, is_private=True
                    )
                except Exception as error:
                    slack_response = getattr(error, "response", None)
                    if not slack_response or slack_response.get("error") != "name_taken":
                        raise
                    suffix += 1
                    suffix_text = "backup" if suffix == 1 else f"backup-{suffix}"
                    name = slugify(f"{base_name}-{suffix_text}")
                    continue
                destination_id = response["channel"]["id"]
                channels[name] = destination_id
                break
            claimed_by = self.state.source_for_destination(destination_id)
            if claimed_by == source_id:
                break
            suffix += 1
            suffix_text = "backup" if suffix == 1 else f"backup-{suffix}"
            name = slugify(f"{base_name}-{suffix_text}")

        self.state.save_conversation(source_id, destination_id, name, label)
        LOG.info("Mapped %s to #%s", label, name)
        return destination_id

    def conversation_info(self, channel: str) -> dict[str, Any]:
        return self.source.conversations_info(channel=channel)["channel"]

    def author_name(self, message: dict[str, Any]) -> str:
        if message.get("user"):
            return self.user_name(message["user"])
        bot_profile = message.get("bot_profile", {})
        return bot_profile.get("name") or message.get("username") or "Slack system"

    def sanitize_text(self, text: str) -> str:
        def replace_user(match: re.Match[str]) -> str:
            return "@" + self.user_name(match.group(1))

        text = re.sub(r"<@([A-Z0-9]+)>", replace_user, text)
        text = re.sub(r"<#([A-Z0-9]+)\|([^>]+)>", r"#\2", text)
        return neutralize_special_mentions(text)

    def render_message(self, message: dict[str, Any]) -> str:
        author = html.escape(self.author_name(message), quote=False)
        text = self.sanitize_text(message.get("text", "")).strip()
        if not text:
            text = "_[attachment or Slack system message]_"
        edited = " · edited" if message.get("edited") else ""
        return (
            f"*{author}* · {slack_date(message['ts'])}{edited} "
            f"· _{html.escape(self.source_team_name, quote=False)}_\n{text}"
        )

    def wait_to_post(self) -> None:
        remaining = self.post_interval - (time.monotonic() - self.last_post)
        if remaining > 0:
            time.sleep(remaining)

    def history(
        self, channel: str, oldest: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"channel": channel, "inclusive": False}
        if oldest:
            kwargs["oldest"] = oldest
        return self.paginate(
            self.source, "conversations_history", "messages", **kwargs
        )

    def thread(self, channel: str, root_ts: str) -> list[dict[str, Any]]:
        return self.paginate(
            self.source,
            "conversations_replies",
            "messages",
            channel=channel,
            ts=root_ts,
        )

    def ensure_thread_parent(
        self, conversation: dict[str, Any], source_thread_ts: str
    ) -> sqlite3.Row | None:
        row = self.state.message(conversation["id"], source_thread_ts)
        if row:
            return row
        messages = self.thread(conversation["id"], source_thread_ts)
        if messages:
            self.copy_message(conversation, messages[0])
        return self.state.message(conversation["id"], source_thread_ts)

    def copy_message(
        self,
        conversation: dict[str, Any],
        message: dict[str, Any],
        update: bool = False,
    ) -> None:
        source_channel = conversation["id"]
        source_ts = message.get("ts")
        if not source_ts:
            return
        subtype = message.get("subtype")
        if subtype in {"message_deleted", "tombstone"}:
            return

        with self.copy_lock:
            destination_channel = self.destination_channel(conversation)
            if self.dry_run:
                return

            source_thread_ts = message.get("thread_ts")
            destination_thread_ts = None
            if source_thread_ts and source_thread_ts != source_ts:
                parent = self.ensure_thread_parent(conversation, source_thread_ts)
                if parent:
                    destination_thread_ts = parent["destination_ts"]

            mapped = self.state.message(source_channel, source_ts)
            rendered = self.render_message(message)
            fingerprint = message_fingerprint(message)
            changed = mapped and mapped["source_fingerprint"] != fingerprint
            if mapped and update and changed:
                if mapped["source_fingerprint"] is not None or message.get("edited"):
                    self.destination.chat_update(
                        channel=mapped["destination_channel"],
                        ts=mapped["destination_ts"],
                        text=rendered,
                    )
                destination_ts = mapped["destination_ts"]
            elif mapped:
                destination_ts = mapped["destination_ts"]
            else:
                self.wait_to_post()
                response = self.destination.chat_postMessage(
                    channel=destination_channel,
                    text=rendered,
                    thread_ts=destination_thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                self.last_post = time.monotonic()
                destination_ts = response["ts"]

            self.state.save_message(
                source_channel,
                source_ts,
                source_thread_ts,
                destination_channel,
                destination_ts,
                fingerprint,
            )

            attachment_thread_ts = destination_thread_ts or destination_ts
            for file_info in message.get("files", []):
                self.copy_file(
                    source_channel,
                    source_ts,
                    file_info,
                    destination_channel,
                    attachment_thread_ts,
                    self.author_name(message),
                )

    def copy_file(
        self,
        source_channel: str,
        source_ts: str,
        file_info: dict[str, Any],
        destination_channel: str,
        destination_thread_ts: str,
        author: str,
    ) -> None:
        file_id = file_info.get("id")
        if not file_id or self.state.file_done(source_channel, source_ts, file_id):
            return
        if not file_info.get("url_private_download") and not file_info.get("url_private"):
            try:
                file_info = self.source.files_info(file=file_id)["file"]
            except Exception:
                LOG.warning("Could not retrieve metadata for Slack file %s", file_id)

        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            LOG.warning("Slack file %s has no downloadable URL; recording it as skipped", file_id)
            self.state.save_file(source_channel, source_ts, file_id, None)
            return

        filename = file_info.get("name") or f"slack-file-{file_id}"
        title = file_info.get("title") or filename
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.source.token}"}
        )
        temp_path: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(delete=False) as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                    temp_path = output.name
            result = self.destination.files_upload_v2(
                channel=destination_channel,
                thread_ts=destination_thread_ts,
                file=temp_path,
                filename=filename,
                title=title,
                initial_comment=f"Attachment from {author}'s mirrored message",
            )
            uploaded = result.get("file") or (result.get("files") or [{}])[0]
            self.state.save_file(
                source_channel, source_ts, file_id, uploaded.get("id")
            )
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    def sync_conversation(
        self, conversation: dict[str, Any], refresh: bool = False
    ) -> None:
        mapped = self.state.conversation(conversation["id"])
        oldest = None if refresh else (mapped["last_history_ts"] if mapped else None)
        messages = self.history(conversation["id"], oldest=oldest)
        if not messages and not refresh:
            self.destination_channel(conversation)
            return

        roots = [
            message
            for message in messages
            if not message.get("thread_ts") or message.get("thread_ts") == message.get("ts")
        ]
        replies_seen = {
            message["ts"]: message
            for message in messages
            if message.get("thread_ts") and message.get("thread_ts") != message.get("ts")
        }
        for root in sorted(roots, key=lambda item: float(item["ts"])):
            self.copy_message(conversation, root, update=refresh)

        thread_roots = {
            root["ts"] for root in roots if root.get("reply_count")
        }
        if refresh:
            thread_roots.update(self.state.thread_roots(conversation["id"]))
        for root_ts in sorted(thread_roots, key=float):
            try:
                thread = self.thread(conversation["id"], root_ts)
            except Exception as error:
                response = getattr(error, "response", None)
                code = response.get("error") if response else None
                if code not in {"channel_not_found", "not_in_channel", "thread_not_found"}:
                    raise
                LOG.warning("Could not refresh thread %s (%s)", root_ts, code)
                continue
            for index, reply in enumerate(thread):
                replies_seen.pop(reply.get("ts"), None)
                self.copy_message(
                    conversation,
                    reply,
                    update=refresh or index == 0,
                )
        for reply in sorted(replies_seen.values(), key=lambda item: float(item["ts"])):
            self.copy_message(conversation, reply, update=refresh)

        if messages:
            latest = max(messages, key=lambda item: float(item["ts"]))["ts"]
            self.state.update_history_ts(conversation["id"], latest)
        else:
            self.destination_channel(conversation)

    def sync_all(self, refresh: bool = False) -> None:
        conversations = self.conversations()
        LOG.info("Found %d source conversations", len(conversations))
        if self.dry_run:
            for conversation in conversations:
                self.destination_channel(conversation)
            return
        for index, conversation in enumerate(conversations, start=1):
            LOG.info("Syncing conversation %d/%d", index, len(conversations))
            try:
                self.sync_conversation(conversation, refresh=refresh)
            except Exception as error:
                response = getattr(error, "response", None)
                code = response.get("error") if response else None
                if code not in {"channel_not_found", "not_in_channel"}:
                    raise
                LOG.warning(
                    "Skipping inaccessible source conversation %s (%s)",
                    conversation["id"],
                    code,
                )

    def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "message":
            return
        channel = event.get("channel")
        if not channel:
            return
        conversation = self.conversation_info(channel)
        subtype = event.get("subtype")
        if subtype == "message_changed":
            changed = event.get("message", {})
            self.copy_message(conversation, changed, update=True)
            return
        if subtype == "message_deleted":
            mapped = self.state.message(channel, event.get("deleted_ts", ""))
            if mapped:
                self.destination.chat_update(
                    channel=mapped["destination_channel"],
                    ts=mapped["destination_ts"],
                    text=f"_Message deleted in {self.source_team_name}._",
                )
            return
        self.copy_message(conversation, event)


class LocalArchive(Mirror):
    """Write Slack conversations to a local folder instead of another Slack."""

    def __init__(
        self,
        source: Any,
        state: State,
        archive_dir: Path,
        source_team_name: str,
        source_user_id: str,
        conversation_types: str = DEFAULT_TYPES,
        channel_prefix: str = "",
        dry_run: bool = False,
    ):
        super().__init__(
            source=source,
            destination=None,
            state=state,
            source_team_name=source_team_name,
            source_user_id=source_user_id,
            conversation_types=conversation_types,
            channel_prefix=channel_prefix,
            post_interval=0,
            dry_run=dry_run,
        )
        self.archive_dir = archive_dir.expanduser().resolve()
        self.defer_html = False

    def destination_channel(self, conversation: dict[str, Any]) -> str:
        source_id = conversation["id"]
        mapped = self.state.conversation(source_id)
        if mapped:
            return mapped["destination_channel"]

        label = self.conversation_label(conversation)
        base_name = self.desired_channel_name(conversation, label)
        name = base_name
        suffix = 0
        while True:
            path = self.archive_dir / name
            claimed_by = self.state.source_for_destination(str(path))
            if not path.exists() and claimed_by is None:
                break
            if claimed_by == source_id:
                break
            suffix += 1
            suffix_text = "backup" if suffix == 1 else f"backup-{suffix}"
            name = slugify(f"{base_name}-{suffix_text}")

        if self.dry_run:
            LOG.info("%s -> %s", label, path)
            return str(path)

        (path / "messages").mkdir(parents=True, exist_ok=True)
        (path / "files").mkdir(exist_ok=True)
        metadata = {
            "source_workspace": self.source_team_name,
            "source_conversation": source_id,
            "label": label,
        }
        (path / "conversation.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.state.save_conversation(source_id, str(path), name, label)
        LOG.info("Mapped %s to %s", label, path)
        return str(path)

    @staticmethod
    def message_path(conversation_dir: Path, source_ts: str) -> Path:
        return conversation_dir / "messages" / f"{source_ts.replace('.', '_')}.json"

    @staticmethod
    def local_file_path(conversation_dir: Path, file_info: dict[str, Any]) -> Path:
        file_id = file_info.get("id") or "unknown"
        original = Path(file_info.get("name") or f"slack-file-{file_id}")
        stem = slugify(original.stem, fallback="slack-file")
        suffix = re.sub(r"[^a-zA-Z0-9.]", "", original.suffix)[:16]
        return conversation_dir / "files" / f"{file_id}-{stem}{suffix}"

    def html_text(self, text: str) -> str:
        text = html.unescape(self.sanitize_text(text))

        def inline(line: str) -> str:
            output: list[str] = []
            position = 0
            for match in re.finditer(r"<((?:https?://|mailto:)[^>|]+)(?:\|([^>]+))?>", line):
                output.append(html.escape(line[position : match.start()]))
                url = html.escape(match.group(1), quote=True)
                label = html.escape(match.group(2) or match.group(1))
                output.append(f'<a href="{url}">{label}</a>')
                position = match.end()
            output.append(html.escape(line[position:]))
            return "".join(output)

        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith(">"):
                lines.append(f"<blockquote>{inline(line[1:].lstrip())}</blockquote>")
            elif line:
                lines.append(f"<div>{inline(line)}</div>")
            else:
                lines.append("<br>")
        return "\n".join(lines)

    @staticmethod
    def rich_text_leaf(element: dict[str, Any]) -> str:
        kind = element.get("type")
        if kind == "text":
            rendered = html.escape(str(element.get("text", ""))).replace("\n", "<br>")
        elif kind == "link":
            url = str(element.get("url", ""))
            label = html.escape(str(element.get("text") or url))
            if url.startswith(("http://", "https://", "mailto:")):
                rendered = f'<a href="{html.escape(url, quote=True)}">{label}</a>'
            else:
                rendered = label
        elif kind == "emoji":
            rendered = f':{html.escape(str(element.get("name", "emoji")))}:'
        else:
            return ""

        style = element.get("style") or {}
        for name, tag in (
            ("code", "code"),
            ("bold", "strong"),
            ("italic", "em"),
            ("underline", "u"),
            ("strike", "s"),
        ):
            if style.get(name):
                rendered = f"<{tag}>{rendered}</{tag}>"
        return rendered

    def rich_text_element(self, element: dict[str, Any], inline: bool = False) -> str:
        kind = element.get("type")
        if kind in {"text", "link", "emoji"}:
            return self.rich_text_leaf(element)
        children = element.get("elements") or []
        if kind == "rich_text_section":
            rendered = "".join(self.rich_text_element(child, inline=True) for child in children)
            return rendered if inline else f"<div>{rendered}</div>"
        if kind == "rich_text_quote":
            rendered = "".join(self.rich_text_element(child, inline=True) for child in children)
            return f"<blockquote>{rendered}</blockquote>"
        if kind == "rich_text_preformatted":
            text = "".join(str(child.get("text", "")) for child in children)
            return f"<pre><code>{html.escape(text)}</code></pre>"
        if kind == "rich_text_list":
            tag = "ol" if element.get("style") == "ordered" else "ul"
            items = "".join(
                f"<li>{self.rich_text_element(child, inline=True)}</li>"
                for child in children
            )
            return f"<{tag}>{items}</{tag}>"
        if kind == "rich_text":
            return "".join(self.rich_text_element(child) for child in children)
        return ""

    def message_html(self, message: dict[str, Any]) -> str:
        rendered = "".join(
            self.rich_text_element(block)
            for block in message.get("blocks", [])
            if block.get("type") == "rich_text"
        )
        return rendered or self.html_text(message.get("text", ""))

    def record_author(self, record: dict[str, Any]) -> str:
        if record.get("author"):
            return str(record["author"])
        message = record.get("message", {})
        recovered = message.get("archive_recovery", {}).get("rendered_text", "")
        for line in recovered.splitlines():
            if " · " in line:
                return line.split(" · ", 1)[0]
        return self.author_name(message)

    def attachment_html(
        self, conversation_dir: Path, file_info: dict[str, Any]
    ) -> str:
        path = self.local_file_path(conversation_dir, file_info)
        if not path.exists() and file_info.get("id"):
            prefix = f'{file_info["id"]}-'
            path = next(
                (candidate for candidate in (conversation_dir / "files").iterdir()
                 if candidate.name.startswith(prefix)),
                path,
            )
        name = html.escape(file_info.get("name") or path.name)
        if not path.exists():
            return f'<span class="missing">Attachment unavailable: {name}</span>'
        relative = html.escape(f"files/{path.name}", quote=True)
        mimetype = file_info.get("mimetype", "")
        is_image = mimetype.startswith("image/") or path.suffix.lower() in {
            ".gif",
            ".jpeg",
            ".jpg",
            ".png",
            ".webp",
        }
        preview = (
            f'<img loading="lazy" src="{relative}" alt="{name}">' if is_image else ""
        )
        return f'<a class="attachment" href="{relative}">{preview}<span>{name}</span></a>'

    @staticmethod
    def write_html(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    def write_conversation_html(self, conversation_dir: Path) -> None:
        metadata_path = conversation_dir / "conversation.json"
        if not metadata_path.exists():
            return
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        records: list[dict[str, Any]] = []
        for path in (conversation_dir / "messages").glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                LOG.warning("Could not read archived message %s", path)
        records.sort(key=lambda record: float(record.get("message", {}).get("ts", 0)))

        messages: list[str] = []
        for record in records:
            message = record.get("message", {})
            source_ts = message.get("ts", "0")
            thread_ts = message.get("thread_ts")
            reply_class = " reply" if thread_ts and thread_ts != source_ts else ""
            deleted_class = " deleted" if record.get("deleted_in_source") else ""
            author = html.escape(self.record_author(record))
            timestamp = time.strftime(
                "%Y-%m-%d %I:%M:%S %p %Z", time.localtime(float(source_ts))
            )
            edited = '<span class="badge">edited</span>' if message.get("edited") else ""
            deleted = (
                '<span class="badge deleted-badge">deleted in source</span>'
                if record.get("deleted_in_source")
                else ""
            )
            text = self.message_html(message)
            attachments = "".join(
                self.attachment_html(conversation_dir, file_info)
                for file_info in message.get("files", [])
            )
            text_html = (
                f'<div class="text">{text}</div>'
                if text
                else ("" if attachments else '<div class="text empty">No text content</div>')
            )
            messages.append(
                f'<article class="message{reply_class}{deleted_class}" id="m-{source_ts.replace(".", "-")}">'
                f'<header><strong>{author}</strong><time>{timestamp}</time>{edited}{deleted}</header>'
                f'{text_html}<div class="attachments">{attachments}</div></article>'
            )

        label = html.escape(metadata.get("label", conversation_dir.name))
        workspace = html.escape(metadata.get("source_workspace", self.source_team_name))
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} — Slack archive</title>
<style>
body{{margin:0;background:#f7f7f8;color:#1d1c1d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:900px;margin:auto;padding:32px 20px 80px}}a{{color:#1264a3}}nav{{margin-bottom:28px}}h1{{margin-bottom:4px}}.summary{{color:#616061;margin-top:0}}.message{{background:white;border:1px solid #ddd;border-radius:10px;margin:10px 0;padding:14px 16px}}.message.reply{{margin-left:42px;border-left:4px solid #8f7ab8}}header{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}time{{color:#616061;font-size:12px}}.text{{line-height:1.5;margin-top:7px;overflow-wrap:anywhere}}.text ol,.text ul{{padding-left:24px}}.empty{{color:#777;font-style:italic}}blockquote{{border-left:4px solid #bbb;color:#444;margin:8px 0;padding:4px 12px}}pre{{background:#f4f4f4;border-radius:6px;overflow:auto;padding:10px}}.badge{{background:#eee;border-radius:10px;color:#555;font-size:11px;padding:2px 7px}}.deleted{{opacity:.65}}.deleted-badge,.missing{{color:#9b1c1c}}.attachments{{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px}}.attachments:empty{{display:none}}.attachment{{display:flex;flex-direction:column;gap:5px;max-width:420px}}.attachment img{{border:1px solid #ddd;border-radius:8px;height:auto;max-height:420px;max-width:100%}}
body{{margin:0;background:#f7f7f8;color:#1d1c1d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:900px;margin:auto;padding:32px 20px 80px}}a{{color:#1264a3}}nav{{margin-bottom:28px}}h1{{margin-bottom:4px}}.summary{{color:#616061;margin-top:0}}.search{{display:block;margin:20px 0 16px}}.search input{{background:white;border:1px solid #bbb;border-radius:8px;box-sizing:border-box;font:inherit;padding:11px 13px;width:100%}}.search input:focus{{border-color:#1264a3;outline:2px solid #b7d7ef}}.search-status{{color:#616061;display:block;font-size:13px;margin-top:6px;min-height:18px}}.message{{background:white;border:1px solid #ddd;border-radius:10px;margin:10px 0;padding:14px 16px}}.message.reply{{margin-left:42px;border-left:4px solid #8f7ab8}}header{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}time{{color:#616061;font-size:12px}}.text{{line-height:1.5;margin-top:7px;overflow-wrap:anywhere}}.text ol,.text ul{{padding-left:24px}}.empty{{color:#777;font-style:italic}}blockquote{{border-left:4px solid #bbb;color:#444;margin:8px 0;padding:4px 12px}}pre{{background:#f4f4f4;border-radius:6px;overflow:auto;padding:10px}}.badge{{background:#eee;border-radius:10px;color:#555;font-size:11px;padding:2px 7px}}.deleted{{opacity:.65}}.deleted-badge,.missing{{color:#9b1c1c}}.attachments{{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px}}.attachments:empty{{display:none}}.attachment{{display:flex;flex-direction:column;gap:5px;max-width:420px}}.attachment img{{border:1px solid #ddd;border-radius:8px;height:auto;max-height:420px;max-width:100%}}
</style></head><body><main><nav><a href="../index.html">← All conversations</a></nav>
<h1>{label}</h1><p class="summary">{workspace} · {len(records)} messages</p>
<label class="search" for="message-search">Search messages, people, and files
<input id="message-search" type="search" placeholder="Type to filter this conversation" autocomplete="off">
<span class="search-status" id="message-search-status" aria-live="polite"></span></label>
{''.join(messages) or '<p>No messages archived yet.</p>'}
</main><script>
const messageSearch=document.querySelector("#message-search");
const archivedMessages=[...document.querySelectorAll(".message")];
const messageSearchStatus=document.querySelector("#message-search-status");
messageSearch.addEventListener("input",()=>{{
  const query=messageSearch.value.trim().toLocaleLowerCase();
  let visible=0;
  for(const message of archivedMessages){{
    const match=!query||message.textContent.toLocaleLowerCase().includes(query);
    message.hidden=!match;
    if(match)visible+=1;
  }}
  messageSearchStatus.textContent=query?`${{visible}} of ${{archivedMessages.length}} messages`:"";
}});
</script></body></html>"""
        self.write_html(conversation_dir / "index.html", page)

    def write_archive_index(self) -> None:
        rows: list[str] = []
        total_messages = 0
        for conversation in self.state.conversations():
            folder = Path(conversation["destination_channel"])
            count = len(list((folder / "messages").glob("*.json")))
            total_messages += count
            label = html.escape(conversation["label"])
            name = html.escape(conversation["destination_name"], quote=True)
            rows.append(
                f'<li><a href="{name}/index.html"><strong>{label}</strong>'
                f'<span>{count} messages</span></a></li>'
            )
        workspace = html.escape(self.source_team_name)
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{workspace} — Slack archive</title>
<style>
body{{margin:0;background:#f7f7f8;color:#1d1c1d;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:760px;margin:auto;padding:40px 20px}}h1{{margin-bottom:4px}}p{{color:#616061}}.search{{display:block;margin:20px 0 16px}}.search input{{background:white;border:1px solid #bbb;border-radius:8px;box-sizing:border-box;font:inherit;padding:11px 13px;width:100%}}.search input:focus{{border-color:#1264a3;outline:2px solid #b7d7ef}}.search-status{{color:#616061;display:block;font-size:13px;margin-top:6px;min-height:18px}}ul{{list-style:none;padding:0}}li a{{align-items:center;background:white;border:1px solid #ddd;border-radius:10px;color:#1264a3;display:flex;justify-content:space-between;margin:9px 0;padding:14px 16px;text-decoration:none}}li span{{color:#616061;font-size:13px}}
</style></head><body><main><h1>{workspace}</h1><p>{len(rows)} conversations · {total_messages} messages</p>
<label class="search" for="conversation-search">Search channels and DMs
<input id="conversation-search" type="search" placeholder="Type a channel or person" autocomplete="off">
<span class="search-status" id="conversation-search-status" aria-live="polite"></span></label>
<ul>{''.join(rows)}</ul></main><script>
const conversationSearch=document.querySelector("#conversation-search");
const archivedConversations=[...document.querySelectorAll("li")];
const conversationSearchStatus=document.querySelector("#conversation-search-status");
conversationSearch.addEventListener("input",()=>{{
  const query=conversationSearch.value.trim().toLocaleLowerCase();
  let visible=0;
  for(const conversation of archivedConversations){{
    const match=!query||conversation.textContent.toLocaleLowerCase().includes(query);
    conversation.hidden=!match;
    if(match)visible+=1;
  }}
  conversationSearchStatus.textContent=query?`${{visible}} of ${{archivedConversations.length}} conversations`:"";
}});
</script></body></html>"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.write_html(self.archive_dir / "index.html", page)

    def copy_message(
        self,
        conversation: dict[str, Any],
        message: dict[str, Any],
        update: bool = False,
    ) -> None:
        source_channel = conversation["id"]
        source_ts = message.get("ts")
        if not source_ts or message.get("subtype") in {"message_deleted", "tombstone"}:
            return

        with self.copy_lock:
            conversation_dir = Path(self.destination_channel(conversation))
            if self.dry_run:
                return

            source_thread_ts = message.get("thread_ts")
            if source_thread_ts and source_thread_ts != source_ts:
                self.ensure_thread_parent(conversation, source_thread_ts)

            record = {
                "source_workspace": self.source_team_name,
                "source_conversation": source_channel,
                "author": self.author_name(message),
                "message": message,
            }
            self.message_path(conversation_dir, source_ts).write_text(
                json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.state.save_message(
                source_channel,
                source_ts,
                source_thread_ts,
                str(conversation_dir),
                source_ts,
                message_fingerprint(message),
            )
            for file_info in message.get("files", []):
                self.copy_file(
                    source_channel,
                    source_ts,
                    file_info,
                    str(conversation_dir),
                    source_ts,
                    self.author_name(message),
                )
            if not self.defer_html:
                self.write_conversation_html(conversation_dir)
                self.write_archive_index()

    def copy_file(
        self,
        source_channel: str,
        source_ts: str,
        file_info: dict[str, Any],
        destination_channel: str,
        destination_thread_ts: str,
        author: str,
    ) -> None:
        file_id = file_info.get("id")
        if not file_id or self.state.file_done(source_channel, source_ts, file_id):
            return
        if not file_info.get("url_private_download") and not file_info.get("url_private"):
            try:
                file_info = self.source.files_info(file=file_id)["file"]
            except Exception:
                LOG.warning("Could not retrieve metadata for Slack file %s", file_id)

        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            LOG.warning("Slack file %s has no downloadable URL", file_id)
            self.state.save_file(source_channel, source_ts, file_id, None)
            return

        destination = self.local_file_path(Path(destination_channel), file_info)
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.source.token}"}
        )
        temp_path: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, delete=False
                ) as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                    temp_path = output.name
            Path(temp_path).replace(destination)
            temp_path = None
            self.state.save_file(source_channel, source_ts, file_id, str(destination))
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "message" or not event.get("channel"):
            return
        channel = event["channel"]
        if event.get("subtype") == "message_deleted":
            mapped = self.state.conversation(channel)
            source_ts = event.get("deleted_ts")
            if mapped and source_ts:
                conversation_dir = Path(mapped["destination_channel"])
                path = self.message_path(conversation_dir, source_ts)
                record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                record["deleted_in_source"] = True
                path.write_text(
                    json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                self.write_conversation_html(conversation_dir)
            return
        conversation = self.conversation_info(channel)
        message = (
            event.get("message", {})
            if event.get("subtype") == "message_changed"
            else event
        )
        self.copy_message(conversation, message, update=True)

    def sync_conversation(
        self, conversation: dict[str, Any], refresh: bool = False
    ) -> None:
        self.defer_html = True
        try:
            super().sync_conversation(conversation, refresh=refresh)
        finally:
            self.defer_html = False
        mapped = self.state.conversation(conversation["id"])
        if mapped:
            self.write_conversation_html(Path(mapped["destination_channel"]))

    def sync_all(self, refresh: bool = False) -> None:
        super().sync_all(refresh=refresh)
        if not self.dry_run:
            self.write_archive_index()


class CompositeMirror:
    def __init__(self, mirrors: list[Any]):
        self.mirrors = mirrors

    def sync_all(self, refresh: bool = False) -> None:
        for mirror in self.mirrors:
            mirror.sync_all(refresh=refresh)

    def handle_event(self, event: dict[str, Any]) -> None:
        for mirror in self.mirrors:
            mirror.handle_event(event)


def make_client(token: str) -> Any:
    try:
        from slack_sdk import WebClient
        from slack_sdk.http_retry.builtin_handlers import (
            RateLimitErrorRetryHandler,
            ServerErrorRetryHandler,
        )
    except ModuleNotFoundError as error:
        raise SystemExit(
            "slack-sdk is not installed. Run: .venv/bin/pip install -r requirements.txt"
        ) from error
    client = WebClient(token=token)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=20))
    client.retry_handlers.append(ServerErrorRetryHandler(max_retry_count=5))
    return client


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "once", "watch"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    target = os.environ.get("SLACK_MIRROR_TARGET", "local").lower()
    if target not in {"slack", "local", "both"}:
        raise SystemExit("SLACK_MIRROR_TARGET must be slack, local, or both.")

    source_token = require_env("SLACK_SOURCE_USER_TOKEN")
    source = make_client(source_token)
    source_auth = source.auth_test()
    source_name = source_auth.get("team") or "source Slack"
    conversation_types = os.environ.get("SLACK_MIRROR_TYPES", DEFAULT_TYPES)
    channel_prefix = os.environ.get("SLACK_CHANNEL_PREFIX", "")
    mirrors: list[Any] = []

    if target in {"slack", "both"}:
        destination = make_client(require_env("SLACK_DEST_USER_TOKEN"))
        destination_auth = destination.auth_test()
        if source_auth["team_id"] == destination_auth["team_id"]:
            raise SystemExit("Source and destination tokens point to the same workspace.")
        default_db = Path(__file__).with_name("slack-mirror.sqlite3")
        state = State(Path(os.environ.get("SLACK_MIRROR_DB", default_db)))
        state.validate_workspace("source_team_id", source_auth["team_id"])
        state.validate_workspace("destination_team_id", destination_auth["team_id"])
        mirrors.append(
            Mirror(
                source=source,
                destination=destination,
                state=state,
                source_team_name=source_name,
                source_user_id=source_auth["user_id"],
                conversation_types=conversation_types,
                channel_prefix=channel_prefix,
                post_interval=float(os.environ.get("SLACK_POST_INTERVAL", "1.05")),
                dry_run=args.mode == "dry-run",
            )
        )
        LOG.info("Slack target: %s", destination_auth.get("team"))

    if target in {"local", "both"}:
        archive_dir = Path(os.environ.get("SLACK_ARCHIVE_DIR", "slack-archive"))
        local_db = Path(
            os.environ.get(
                "SLACK_LOCAL_MIRROR_DB", archive_dir / ".slack-mirror.sqlite3"
            )
        )
        state = State(local_db)
        state.validate_workspace("source_team_id", source_auth["team_id"])
        state.validate_workspace("destination_team_id", f"local:{archive_dir.resolve()}")
        mirrors.append(
            LocalArchive(
                source=source,
                state=state,
                archive_dir=archive_dir,
                source_team_name=source_name,
                source_user_id=source_auth["user_id"],
                conversation_types=conversation_types,
                channel_prefix=channel_prefix,
                dry_run=args.mode == "dry-run",
            )
        )
        LOG.info("Local target: %s", archive_dir.resolve())

    mirror = mirrors[0] if len(mirrors) == 1 else CompositeMirror(mirrors)
    LOG.info("Source: %s", source_name)
    if args.mode == "dry-run":
        mirror.sync_all()
        return 0
    if args.mode == "once":
        mirror.sync_all(refresh=True)
        return 0

    source_app_token = require_env("SLACK_SOURCE_APP_TOKEN")
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ModuleNotFoundError as error:
        raise SystemExit("Socket Mode support is unavailable in slack-sdk") from error

    socket_client = SocketModeClient(app_token=source_app_token, web_client=source)

    def process_socket_request(client: Any, request: Any) -> None:
        if request.type != "events_api":
            return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        try:
            mirror.handle_event(request.payload.get("event", {}))
        except Exception:
            LOG.exception("Failed to mirror a Slack event; Slack may redeliver it")

    socket_client.socket_mode_request_listeners.append(process_socket_request)
    socket_client.connect()
    LOG.info("Live mirroring is connected. Press Ctrl-C to stop.")
    mirror.sync_all(refresh=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        LOG.info("Stopping")
    finally:
        socket_client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
