# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Console history storage with JSONL persistence.

Captures user IPython input/output and persists to a JSONL file
(one JSON object per line). Crash-safe: partial writes only lose
the last entry.

The bridge tracks a delivery cursor so the MCP client is stateless —
it just calls consume() and gets whatever is new.
"""

import json
import logging
import os
import time

logger = logging.getLogger("YADE-Bridge")

DATA_DIR = ".yade-mcp"
HISTORY_FILENAME = os.path.join(DATA_DIR, "console_history.jsonl")
CURSOR_FILENAME = os.path.join(DATA_DIR, "console_cursor.json")

DEFAULT_MAX_ENTRIES = 500


class ConsoleHistory:
    """Append-only console history with JSONL persistence.

    The delivery cursor (last_delivered_id) is persisted to disk so
    that entries are not lost if the MCP client disconnects and
    reconnects.
    """

    def __init__(self, max_entries=DEFAULT_MAX_ENTRIES, path=None):
        self._max_entries = max_entries
        self._path = path or HISTORY_FILENAME
        self._cursor_path = CURSOR_FILENAME
        self._entries = []
        self._next_id = 1
        self._last_delivered_id = 0
        self.on_new_entry = None  # callback for push notification

        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

        self._load()
        self._load_cursor()
        logger.info(
            "ConsoleHistory initialized (%d entries, cursor=%d)",
            len(self._entries),
            self._last_delivered_id,
        )

    def add(self, input_text, output="", result=None, success=True):
        """Record a console entry.

        Args:
            input_text: The code the user typed.
            output: Captured stdout during execution.
            result: Expression result (if any).
            success: Whether execution succeeded.

        Returns:
            The entry dict.
        """
        entry = {
            "id": self._next_id,
            "input": input_text,
            "output": output,
            "result": _serialize(result),
            "success": success,
            "timestamp": time.time(),
        }
        self._next_id += 1
        self._entries.append(entry)
        self._append_to_file(entry)
        self._prune()

        if self.on_new_entry:
            try:
                self.on_new_entry(entry)
            except Exception as e:
                logger.error("Console history callback failed: %s", e)

        return entry

    def consume(self, limit=20):
        """Return undelivered entries and advance the cursor.

        The MCP client is stateless — it just calls this and gets
        whatever is new since the last call.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            dict with entries list and metadata.
        """
        entries = [e for e in self._entries if e["id"] > self._last_delivered_id]

        if len(entries) > limit:
            entries = entries[-limit:]

        if entries:
            self._last_delivered_id = entries[-1]["id"]
            self._save_cursor()

        return {
            "entries": entries,
            "cursor": self._last_delivered_id,
            "has_more": any(e["id"] > self._last_delivered_id for e in self._entries),
        }

    def query(self, since_id=0, limit=20):
        """Return entries with id > since_id (read-only, does not advance cursor).

        Args:
            since_id: Return entries after this ID (0 = all).
            limit: Maximum number of entries to return.

        Returns:
            dict with entries list and latest_id.
        """
        filtered = [e for e in self._entries if e["id"] > since_id]
        if len(filtered) > limit:
            filtered = filtered[-limit:]

        latest_id = self._entries[-1]["id"] if self._entries else 0

        return {
            "entries": filtered,
            "latest_id": latest_id,
            "total": len(self._entries),
        }

    @property
    def latest_id(self):
        return self._entries[-1]["id"] if self._entries else 0

    def _append_to_file(self, entry):
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to write console history: %s", e)

    def _load(self):
        """Load entries from JSONL file."""
        if not os.path.exists(self._path):
            return

        entries = []
        max_id = 0
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                        entry_id = entry.get("id", 0)
                        if entry_id > max_id:
                            max_id = entry_id
                    except json.JSONDecodeError:
                        continue  # skip corrupted lines
        except OSError as e:
            logger.warning("Failed to load console history: %s", e)
            return

        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries :]

        self._entries = entries
        self._next_id = max_id + 1

    def _load_cursor(self):
        """Load delivery cursor from disk."""
        if not os.path.exists(self._cursor_path):
            return
        try:
            with open(self._cursor_path, encoding="utf-8") as f:
                data = json.load(f)
                self._last_delivered_id = data.get("last_delivered_id", 0)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save_cursor(self):
        """Persist delivery cursor to disk."""
        try:
            with open(self._cursor_path, "w", encoding="utf-8") as f:
                json.dump({"last_delivered_id": self._last_delivered_id}, f)
        except OSError as e:
            logger.error("Failed to save console cursor: %s", e)

    def _prune(self):
        """Remove oldest entries if over limit and rewrite file."""
        if len(self._entries) <= self._max_entries:
            return

        self._entries = self._entries[-self._max_entries :]
        self._rewrite_file()

    def _rewrite_file(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for entry in self._entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Failed to rewrite console history: %s", e)


def _serialize(value):
    """Serialize a value for JSON storage."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
