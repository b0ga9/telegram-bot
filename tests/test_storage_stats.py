import tempfile
import unittest
from pathlib import Path
from storage import SQLiteStorage

class StorageStatsTests(unittest.TestCase):
    def test_stats_survive_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'stats.db'
            db = SQLiteStorage(str(path))
            db.increment_stat('market_checks', now=1000)
            db.increment_stat('market_checks', now=1010)
            db.increment_stat('market_published', now=1020)
            db.close()
            db = SQLiteStorage(str(path))
            stats = db.get_stats()
            self.assertEqual(stats['market_checks'], 2)
            self.assertEqual(stats['market_published'], 1)
            self.assertEqual(stats['last_market_check_at'], 1010)
            db.close()

if __name__ == '__main__':
    unittest.main()
