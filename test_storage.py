import tempfile
import unittest
from pathlib import Path

from storage import SQLiteStorage


class StorageTests(unittest.TestCase):
    def test_dedup_and_cooldown_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            store = SQLiteStorage(str(db))

            self.assertFalse(store.is_duplicate("PULSE", "abc"))
            self.assertFalse(store.cooldown_active("PULSE", 3600, now=1000))

            store.mark_published("PULSE", "abc", now=1000)
            self.assertTrue(store.is_duplicate("PULSE", "abc"))
            self.assertTrue(store.cooldown_active("PULSE", 3600, now=1001))
            self.assertFalse(store.cooldown_active("PULSE", 3600, now=4600))
            store.close()

            store2 = SQLiteStorage(str(db))
            self.assertTrue(store2.is_duplicate("PULSE", "abc"))
            self.assertTrue(store2.cooldown_active("PULSE", 3600, now=1001))
            store2.close()


if __name__ == "__main__":
    unittest.main()
