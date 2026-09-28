"""User-owned B presentation, separate from runtime authority and checkpoints."""
from __future__ import annotations

import unicodedata


class ProcessPresentation:
    def __init__(self, storage):
        self.storage = storage

    def read(self, process_id: str) -> dict:
        return self.storage.read_process_presentation(process_id)

    def rename(self, process_id: str, display_name: str) -> dict:
        name = str(display_name).strip()
        if not name or len(name) > 80 or any(unicodedata.category(c).startswith("C") for c in name):
            raise ValueError("display_name must be 1-80 characters without control characters")
        return self.storage.update_process_presentation(process_id, display_name=name)

    def archive(self, process_id: str, archived: bool = True) -> dict:
        return {**self.storage.update_process_presentation(process_id, archived=archived),
                "execution_unchanged": True, "data_deleted": False}
