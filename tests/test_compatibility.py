import unittest

from crate_transfer.compatibility import add_compatibility_warnings, compatibility_warnings
from crate_transfer.protocol import ProtocolError, validate_manifest
from support import valid_manifest


class CompatibilityTests(unittest.TestCase):
    def test_both_serato_version_directions_have_explicit_conversion_limits(self):
        downgrade = "\n".join(compatibility_warnings("serato", "serato3"))
        self.assertIn("Version downgrade", downgrade)
        self.assertIn("database-only metadata", downgrade)
        self.assertIn("not a database downgrade", downgrade)
        upgrade = "\n".join(compatibility_warnings("serato3", "serato"))
        self.assertIn("Version upgrade", upgrade)
        self.assertIn("not an in-place library upgrade", upgrade)
        self.assertIn("not imported", upgrade)

    def test_unknown_installed_versions_are_not_claimed_compatible(self):
        for app in ("rekordbox-native", "engine", "traktor", "mixxx", "djay", "virtualdj-native"):
            warning = "\n".join(compatibility_warnings(app, app))
            self.assertIn("version is not determined", warning)
            self.assertIn("unsupported schemas are refused", warning)
            self.assertIn("not downgraded", warning)
            self.assertIn("not upgraded in place", warning)

    def test_warning_roundtrip_is_validated_deduplicated_and_does_not_include_source_path(self):
        manifest = valid_manifest([])
        manifest["destination"] = {"format": "serato3"}
        add_compatibility_warnings(manifest, "serato", "serato3")
        first = dict(manifest)
        add_compatibility_warnings(manifest, "serato", "serato3")
        self.assertEqual(manifest, first)
        self.assertEqual(manifest["source_format"], "serato")
        self.assertIs(validate_manifest(manifest), manifest)
        for invalid in ("unsupported", {"format": "serato", "path": "/private/source"}, None):
            manifest["source_format"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ProtocolError, "source format"):
                validate_manifest(manifest)
