import shutil
import tempfile
import unittest
from pathlib import Path


class TempHomeTestCase(unittest.TestCase):
    """Gives every test its own isolated data directory (never %LOCALAPPDATA%)."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="cirana-test-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
