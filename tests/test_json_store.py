import json

from cirana.storage.json_store import JsonStore
from tests.support import TempHomeTestCase


class JsonStoreTests(TempHomeTestCase):
    def make(self, **kw):
        return JsonStore(self.home / "x.json", lambda: {"items": []}, 1,
                         lambda d: isinstance(d, dict) and isinstance(d.get("items"), list), **kw)

    def test_default_created_and_roundtrip(self):
        store = self.make()
        self.assertEqual(store.load(), {"items": []})
        store.save({"items": [1, 2]})
        self.assertEqual(self.make().load(), {"items": [1, 2]})

    def test_corrupt_file_is_quarantined_and_backup_used(self):
        store = self.make()
        store.save({"items": [1]})
        store.save({"items": [1, 2]})  # creates .bak holding [1]
        store.path.write_text("{ not json", encoding="utf-8")
        data = self.make().load()
        self.assertEqual(data, {"items": [1]})
        self.assertTrue(list(self.home.glob("x.json.corrupt-*")))

    def test_corrupt_without_backup_resets_to_default(self):
        store = self.make()
        store.path.write_text("garbage", encoding="utf-8")
        self.assertEqual(store.load(), {"items": []})

    def test_invalid_schema_rejected(self):
        store = self.make()
        store.path.write_text(json.dumps({"version": 1, "data": {"items": "nope"}}), encoding="utf-8")
        self.assertEqual(store.load(), {"items": []})

    def test_newer_version_is_not_loaded(self):
        store = self.make()
        store.path.write_text(json.dumps({"version": 99, "data": {"items": [1]}}), encoding="utf-8")
        self.assertEqual(store.load(), {"items": []})

    def test_save_rejects_invalid_data_and_leaves_no_tmp(self):
        store = self.make()
        store.load()
        with self.assertRaises(ValueError):
            store.save({"items": "bad"})
        self.assertFalse(list(self.home.glob("*.tmp")))

    def test_migration(self):
        old = JsonStore(self.home / "m.json", lambda: {"items": []}, 1)
        old.save({"old": True})
        new = JsonStore(self.home / "m.json", lambda: {"items": []}, 2, migrate=lambda v, d: {"items": [d["old"]]})
        self.assertEqual(new.load(), {"items": [True]})
