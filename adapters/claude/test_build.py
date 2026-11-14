"""Ensure archive construction happens after the native launcher is signed."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location("claude_bundle_build", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def test_launcher_is_signed_before_mcpb_creation(self):
        actions = []
        def tool(args, **kwargs):
            if "clang" in args:
                actions.append("compile")
                Path(args[args.index("-o") + 1]).write_bytes(b"compiled")
            elif "sign-binary" in args:
                actions.append("sign")
                launcher = Path(args[args.index("--binary") + 1])
                launcher.write_bytes(launcher.read_bytes() + b"-signed")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "adapter.mcpb"
            with patch.object(build, "OUTPUT", output), patch.object(build.subprocess, "run", side_effect=tool), \
                 patch.object(build.shutil, "copy2"), patch("builtins.print"):
                build.main()
            self.assertEqual(actions, ["compile", "sign"])
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("server/d-bond-launcher"), b"compiled-signed")


if __name__ == "__main__":
    unittest.main()
