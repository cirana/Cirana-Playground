from cirana.core.application_registry import ApplicationRegistry
from cirana.core.application_scanner import deduplicate
from cirana.core.category_engine import CategoryEngine
from cirana.core.models import AppEntry, stable_app_id
from cirana.storage.settings_manager import SettingsManager
from tests.support import TempHomeTestCase


def entry(name, launch="shortcut", target=None, source="start_menu_user", exe=""):
    return AppEntry(id=stable_app_id(name), name=name, launch_type=launch, target=target or f"C:\\x\\{name}.lnk",
                    source=source, exe_path=exe)


class RegistryTests(TempHomeTestCase):
    def make(self):
        return ApplicationRegistry(SettingsManager(self.home))

    def test_ids_are_deterministic_across_launch_types(self):
        self.assertEqual(stable_app_id("Visual Studio Code"), stable_app_id("  visual   studio code "))
        self.assertNotEqual(stable_app_id("A"), stable_app_id("B"))

    def test_user_overrides_survive_rescan_and_restart(self):
        reg = self.make()
        apps = [entry("Alpha"), entry("Beta"), entry("Gamma")]
        reg.apply_scan(apps)
        alpha, beta = apps[0].id, apps[1].id
        reg.set_category(alpha, "gaming")
        reg.hide(beta)
        reg.toggle_favorite(alpha)
        reg.apply_scan([entry("Alpha"), entry("Beta")])  # Gamma vanished
        self.assertEqual(reg.category_of(reg.get(alpha)), "gaming")
        self.assertNotIn(beta, [a.id for a in reg.all_apps()])
        reg2 = self.make()  # simulate restart
        self.assertEqual(reg2.category_of(reg2.get(alpha)), "gaming")
        self.assertTrue(reg2.is_favorite(alpha))
        self.assertEqual([a.id for a in reg2.hidden_apps()], [beta])

    def test_override_for_vanished_app_is_kept(self):
        reg = self.make()
        gamma = stable_app_id("Gamma")
        reg.apply_scan([entry("Gamma")])
        reg.set_category(gamma, "design")
        reg.apply_scan([])
        reg.apply_scan([entry("Gamma")])
        self.assertEqual(reg.category_of(reg.get(gamma)), "design")

    def test_category_crud_and_rename(self):
        reg = self.make()
        cid = reg.add_category("Research")
        reg.rename_category("gaming", "Play")
        reg.rename_category(cid, "Science")
        self.assertEqual(reg.category_name("gaming"), "Play")
        self.assertEqual(reg.category_name(cid), "Science")
        with self.assertRaises(ValueError):
            reg.add_category("science")
        reg.apply_scan([entry("Alpha")])
        reg.set_category(stable_app_id("Alpha"), cid)
        reg.delete_category(cid)
        self.assertIn(reg.category_of(reg.get(stable_app_id("Alpha"))), ("other",))
        with self.assertRaises(ValueError):
            reg.delete_category("gaming")

    def test_recents_most_recent_first_and_deduped(self):
        reg = self.make()
        apps = [entry("A"), entry("B")]
        reg.apply_scan(apps)
        reg.record_launch(apps[0].id)
        reg.record_launch(apps[1].id)
        reg.record_launch(apps[0].id)
        self.assertEqual([a.name for a in reg.recents()], ["A", "B"])

    def test_custom_app(self):
        reg = self.make()
        app = reg.add_custom_app("Tool", "C:\\Windows\\notepad.exe", "executable")
        self.assertTrue(self.make().get(app.id).custom)
        reg.remove_custom_app(app.id)
        self.assertIsNone(self.make().get(app.id))


class DedupeTests(TempHomeTestCase):
    def test_prefers_shortcut_over_aumid_and_merges_exe(self):
        lnk = entry("Calc", exe="C:\\Windows\\calc.exe")
        aumid = entry("calc", launch="aumid", target="Microsoft.Calc!App", source="packaged")
        result = deduplicate([aumid, lnk])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].launch_type, "shortcut")

    def test_user_scope_wins_between_equal_shortcuts(self):
        sys_ = entry("X", source="start_menu_system", target="C:\\sys\\X.lnk")
        usr = entry("X", source="start_menu_user", target="C:\\usr\\X.lnk")
        self.assertEqual(deduplicate([sys_, usr])[0].target, "C:\\usr\\X.lnk")

    def test_registry_entry_for_known_exe_is_dropped(self):
        lnk = entry("Chrome Browser", exe="C:\\c\\chrome.exe")
        reg = entry("Google Chrome", launch="executable", target="C:\\c\\chrome.exe", source="registry",
                    exe="C:\\c\\chrome.exe")
        self.assertEqual(len(deduplicate([lnk, reg])), 1)


class CategoryTests(TempHomeTestCase):
    def test_known_apps(self):
        engine = CategoryEngine()
        cases = {
            "Visual Studio Code": "development", "Google Chrome": "browsers", "Discord": "communication",
            "Steam": "gaming", "VLC media player": "multimedia", "Adobe Photoshop 2024": "design",
            "Microsoft Word": "productivity", "Task Manager": "utilities", "Mystery Thing": "other",
        }
        for name, expected in cases.items():
            self.assertEqual(engine.classify(entry(name)), expected, name)

    def test_pluggable_classifier_takes_priority(self):
        class Always:
            def classify(self, app):
                return "design"

        engine = CategoryEngine()
        engine.register(Always(), first=True)
        self.assertEqual(engine.classify(entry("Steam")), "design")


class LocalizedCategoryTests(TempHomeTestCase):
    def aumid(self, name, target):
        return AppEntry(id=stable_app_id(name), name=name, launch_type="aumid", target=target, source="packaged")

    def test_localized_names_use_english_package_identity(self):
        engine = CategoryEngine()
        self.assertEqual(engine.classify(self.aumid("Hesap Makinesi", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")),
                         "utilities")
        self.assertEqual(engine.classify(self.aumid("Qwerty", "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic")),
                         "multimedia")
        self.assertEqual(engine.classify(self.aumid("Ayarlar", "windows.immersivecontrolpanel_cw5n1h2txyewy!x")),
                         "utilities")

    def test_nodejs_shortcut_is_development(self):
        app = entry("Install Additional Tools for Node.js")
        self.assertEqual(CategoryEngine().classify(app), "development")
