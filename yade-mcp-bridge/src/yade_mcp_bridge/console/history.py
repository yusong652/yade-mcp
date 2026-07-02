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

from ..paths import DATA_DIR

logger = logging.getLogger("MCP-Bridge")

HISTORY_FILENAME = os.path.join(DATA_DIR, "console_history.jsonl")
CURSOR_FILENAME = os.path.join(DATA_DIR, "console_cursor.json")

DEFAULT_MAX_ENTRIES = 500


class ConsoleHistory:
    """Append-only console history with JSONL persistence.

    The delivery cursor (last_delivered_id) is persisted to disk so
    that entries are not lost if the MCP client disconnects and
    reconnects.
    """

    def __init__(self, maxEntries=DEFAULT_MAX_ENTRIES, path=None):
        self._maxEntries = maxEntries
        self._path = path or HISTORY_FILENAME
        self._cursorPath = CURSOR_FILENAME
        self._entries = []
        self._nextId = 1
        self._lastDeliveredId = 0
        self.onNewEntry = None  # callback for push notification

        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)

        self._load()
        self._loadCursor()
        logger.info(
            "ConsoleHistory initialized (%d entries, cursor=%d)",
            len(self._entries),
            self._lastDeliveredId,
        )

    def add(self, inputText, output="", result=None, success=True):
        """Record a console entry and return it.

        ``inputText`` is the code the user typed, ``output`` the captured
        stdout, ``result`` the expression result (if any), and ``success``
        whether execution succeeded.
        """
        entry = {
            "id": self._nextId,
            "input": inputText,
            "output": output,
            "result": _serialize(result),
            "success": success,
            "timestamp": time.time(),
        }
        self._nextId += 1
        self._entries.append(entry)
        self._appendToFile(entry)
        self._prune()

        if self.onNewEntry:
            try:
                self.onNewEntry(entry)
            except Exception as e:
                logger.error("Console history callback failed: %s", e)

        return entry

    def consume(self, limit=20):
        """Return undelivered entries and advance the cursor.

        The MCP client is stateless — it just calls this and gets whatever
        is new since the last call. At most ``limit`` entries are returned;
        the result dict carries the entries list and cursor metadata.
        """
        entries = [e for e in self._entries if e["id"] > self._lastDeliveredId]

        if len(entries) > limit:
            entries = entries[-limit:]

        if entries:
            self._lastDeliveredId = entries[-1]["id"]
            self._saveCursor()

        return {
            "entries": entries,
            "cursor": self._lastDeliveredId,
            "has_more": any(e["id"] > self._lastDeliveredId for e in self._entries),
        }

    def _appendToFile(self, entry):
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
        maxId = 0
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                        entryId = entry.get("id", 0)
                        if entryId > maxId:
                            maxId = entryId
                    except json.JSONDecodeError:
                        continue  # skip corrupted lines
        except OSError as e:
            logger.warning("Failed to load console history: %s", e)
            return

        if len(entries) > self._maxEntries:
            entries = entries[-self._maxEntries :]

        self._entries = entries
        self._nextId = maxId + 1

    def _loadCursor(self):
        """Load delivery cursor from disk."""
        if not os.path.exists(self._cursorPath):
            return
        try:
            with open(self._cursorPath, encoding="utf-8") as f:
                data = json.load(f)
                self._lastDeliveredId = data.get("last_delivered_id", 0)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _saveCursor(self):
        """Persist delivery cursor to disk."""
        try:
            with open(self._cursorPath, "w", encoding="utf-8") as f:
                json.dump({"last_delivered_id": self._lastDeliveredId}, f)
        except OSError as e:
            logger.error("Failed to save console cursor: %s", e)

    def _prune(self):
        """Remove oldest entries if over limit and rewrite file."""
        if len(self._entries) <= self._maxEntries:
            return

        self._entries = self._entries[-self._maxEntries :]
        self._rewriteFile()

    def _rewriteFile(self):
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
