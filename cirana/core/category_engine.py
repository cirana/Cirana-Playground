"""Local, rule-based application categorization.

The engine is intentionally small and pluggable: anything implementing
``Classifier`` can be registered, so a local model can be added later without
touching callers.
"""
from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Protocol

from cirana.core.models import DEFAULT_CATEGORY, AppEntry


class Classifier(Protocol):
    def classify(self, app: AppEntry) -> str | None:
        """Return a category id, or None if this classifier has no opinion."""


# Exact (normalized) executable stems / names that are unambiguous.
KNOWN_MAPPINGS: dict[str, str] = {
    "code": "development", "devenv": "development", "idea64": "development", "pycharm64": "development",
    "webstorm64": "development", "clion64": "development", "rider64": "development", "studio64": "development",
    "sublime_text": "development", "notepad++": "development", "githubdesktop": "development",
    "windowsterminal": "development", "wt": "development", "postman": "development", "docker desktop": "development",
    "chrome": "browsers", "msedge": "browsers", "firefox": "browsers", "brave": "browsers", "opera": "browsers",
    "vivaldi": "browsers", "arc": "browsers", "tor browser": "browsers",
    "discord": "communication", "slack": "communication", "teams": "communication", "ms-teams": "communication",
    "telegram": "communication", "whatsapp": "communication", "zoom": "communication", "skype": "communication",
    "signal": "communication", "thunderbird": "communication", "outlook": "communication",
    "steam": "gaming", "epicgameslauncher": "gaming", "battle.net": "gaming", "goggalaxy": "gaming",
    "origin": "gaming", "eadesktop": "gaming", "riotclientservices": "gaming", "minecraft": "gaming",
    "spotify": "multimedia", "vlc": "multimedia", "obs64": "multimedia", "obs": "multimedia", "audacity": "multimedia",
    "wmplayer": "multimedia", "potplayermini64": "multimedia", "foobar2000": "multimedia",
    "photoshop": "design", "illustrator": "design", "figma": "design", "blender": "design", "gimp-2": "design",
    "inkscape": "design", "krita": "design", "indesign": "design", "afterfx": "design", "premiere pro": "design",
    "winword": "productivity", "excel": "productivity", "powerpnt": "productivity", "onenote": "productivity",
    "notion": "productivity", "obsidian": "productivity", "acrobat": "productivity", "soffice": "productivity",
    "taskmgr": "utilities", "calc": "utilities", "cmd": "utilities", "powershell": "utilities", "regedit": "utilities",
    "explorer": "utilities", "7zfm": "utilities", "winrar": "utilities", "mspaint": "design",
}

# Known name / executable prefixes (matched exactly, or as a leading token when the key has >= 5 characters).
KNOWN_MAPPINGS.update({
    "ldplayer": "gaming", "genymotion": "development", "handbrake": "multimedia", "mkvtoolnix": "multimedia",
    "subtitleedit": "multimedia", "itunes": "multimedia", "stremio": "multimedia", "qbittorrent": "utilities",
    "jdownloader": "utilities", "msi afterburner": "utilities", "rivatuner": "utilities", "kaspersky": "utilities",
    "oracle vm virtualbox": "utilities", "sandboxie": "utilities", "powertoys": "utilities", "krisp": "communication",
    "xournal": "productivity", "dolby": "multimedia", "galaxy buds": "utilities", "armoury crate": "utilities",
    "nvidia app": "utilities", "claude": "productivity", "chatgpt": "productivity", "capcut": "multimedia",
    "internet download manager": "utilities", "http toolkit": "development", "glazewm": "utilities",
    "zen": "browsers", "nightly": "browsers",
})

# Ordered keyword rules: (category, regex) matched against name, exe name, group and package words.
# Includes the Turkish names Windows shows on a Turkish install; package/exe stems stay English.
KEYWORD_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("development", re.compile(r"\b(visual studio|vs ?code|code|python|node\.?js|git|github|gitlab|docker|terminal|"
                               r"powershell|sdk|compiler|jetbrains|intellij|pycharm|android studio|eclipse|"
                               r"sql|mysql|postgres|mongodb|ide|debugger|developer|devtools|cmake|rust|cargo|"
                               r"java|jdk|unity hub|unreal|wsl|ubuntu|api|postman|dev ?home|nasm|odbc|"
                               r"windows app cert kit|application verifier|command palette|commandpalette)\b", re.I)),
    ("browsers", re.compile(r"\b(browser|chrome|chromium|edge|firefox|brave|opera|vivaldi|tarayıcı\w*)\b", re.I)),
    ("communication", re.compile(r"\b(chat|messenger|mail|posta|email|discord|slack|teams|zoom|skype|telegram|"
                                 r"whatsapp|meet|call|phone|telefon|voip|signal|contacts|kişiler)\b", re.I)),
    ("gaming", re.compile(r"\b(game|games|gaming|steam|launcher|xbox|epic|riot|battle|minecraft|playstation|"
                          r"gog|ubisoft|origin|ea app|nvidia geforce|geforce experience|oyun\w*)\b", re.I)),
    ("multimedia", re.compile(r"\b(zune|müzik|film\w*|fotoğraf\w*|kamera|music|player|video|media|audio|movies|tv|"
                              r"photos|camera|recorder|obs|spotify|vlc|podcast|stream|sound|ses|clipchamp|groove|"
                              r"subtitle|captions?|live ?captions|dolby)\b", re.I)),
    ("design", re.compile(r"\b(photoshop|illustrator|figma|paint|draw|design|sketch|blender|3d|canvas|"
                          r"gimp|inkscape|krita|creative cloud|lightroom|animate|snip)\b", re.I)),
    ("productivity", re.compile(r"\b(word|excel|powerpoint|office|onenote|notes|notion|calendar|takvim|todo|to do|"
                                r"pdf|reader|acrobat|sticky|planner|obsidian|libreoffice|openoffice|sheets|docs|"
                                r"outlook|tasks|notepad|wordpad|not defteri|weather|hava durumu|news|haber\w*|"
                                r"power automate|powerautomate)\b", re.I)),
    ("utilities", re.compile(r"\b(tool|tools|utility|utilities|manager|monitor|cleaner|backup|yedekleme|zip|archive|"
                             r"7-?zip|winrar|calculator|hesap makinesi|settings|ayarlar|immersivecontrolpanel|"
                             r"control panel|denetim masası|registry|disk|driver|update|system|sistem|security|"
                             r"güvenli\w*|sechealth\w*|defender|vpn|explorer|task manager|görev \w+|snipping|clock|"
                             r"alarms?|saat|character map|karakter|command prompt|magnify|büyüteç|narrator|"
                             r"okuyucu\w*|keyboard|klavye\w*|psr|kaydedici\w*|mmc|msinfo32|msconfig|osk|services|"
                             r"hizmetler|yönetimi|yöneticisi|olay|performans|kurtarma|recovery|bellek|memory|"
                             r"yazdırma|printmanagement|sandbox|store|get ?help|yardım|feedback|geri bildirim|"
                             r"quick ?assist|hızlı yardım|remote desktop|uzak masaüstü|voice ?access|ses erişimi|"
                             r"iscsi|defrag\w*|sürücü\w*|run|çalıştır|ekran \w+|windows backup|windows features|"
                             r"get started|başlarken|armoury|buds|graphics|intel)\b", re.I)),
]


def _package_words(app: AppEntry) -> str:
    """English words from a packaged app's identity (``Microsoft.WindowsCalculator_8wekyb3d8bbwe!App``).

    Localized Windows shows names like "Hesap Makinesi"; the package family name stays English.
    """
    if app.launch_type != "aumid":
        return ""
    family = app.target.split("!")[0].split("_")[0]
    if "{" in family or "\\" in family:  # known-folder / GUID style ids carry no package name
        return ""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family.replace(".", " "))
    return spaced


class RuleClassifier:
    """Known mappings first, keyword rules second."""

    def classify(self, app: AppEntry) -> str | None:
        exe_stem = ""
        if app.exe_path:
            exe_stem = PureWindowsPath(app.exe_path).stem.lower()
        elif app.launch_type == "executable":
            exe_stem = PureWindowsPath(app.target).stem.lower()

        name_key = app.name.lower().strip()
        for key in (exe_stem, name_key):
            if key and key in KNOWN_MAPPINGS:
                return KNOWN_MAPPINGS[key]
        for key, category in KNOWN_MAPPINGS.items():
            # Longer names (>=5 chars) are safe to match as a leading token of the display name.
            if len(key) >= 5 and name_key.startswith(key):
                return category

        haystacks = [app.name, exe_stem, app.group, _package_words(app)]
        for category, pattern in KEYWORD_RULES:
            if any(h and pattern.search(h) for h in haystacks):
                return category
        return None


class CategoryEngine:
    def __init__(self, classifiers: list[Classifier] | None = None) -> None:
        self.classifiers: list[Classifier] = classifiers if classifiers is not None else [RuleClassifier()]

    def register(self, classifier: Classifier, first: bool = False) -> None:
        self.classifiers.insert(0 if first else len(self.classifiers), classifier)

    def classify(self, app: AppEntry) -> str:
        for classifier in self.classifiers:
            category = classifier.classify(app)
            if category:
                return category
        return DEFAULT_CATEGORY
