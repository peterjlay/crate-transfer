import hashlib
import unittest

from crate_transfer.protocol import (
    MAX_FILES,
    MAX_FILE,
    MAX_TOTAL,
    ProtocolError,
    identifier,
    label,
    machine_id,
    validate_manifest,
)
from support import file_record, valid_manifest


class ProtocolTests(unittest.TestCase):
    def test_valid_manifest_preserves_payload(self):
        record = file_record("Track.mp3", b"ID3\x00synthetic-tag-and-audio")
        manifest = valid_manifest([record], [{"path": ["Live", "Set"], "tracks": [record["id"]]}])
        self.assertIs(validate_manifest(manifest), manifest)

    def test_mpeg_video_filenames_are_accepted_case_insensitively(self):
        for extension in (".mpg", ".mpeg", ".MPG", ".MPEG"):
            with self.subTest(extension=extension):
                record = file_record("DJ Qbert - Wave Twisters [2001]" + extension, b"synthetic-video")
                manifest = valid_manifest([record])
                self.assertIs(validate_manifest(manifest), manifest)

    def test_identifiers_machine_ids_and_labels_reject_malformed_values(self):
        for value in (None, 42, "a" * 63, "g" * 64, "A" * 64):
            with self.subTest(case=type(value).__name__):
                with self.assertRaises(ProtocolError):
                    identifier(value)
        for value in (None, 1, "not-a-uuid"):
            with self.subTest(case=type(value).__name__):
                with self.assertRaises(ProtocolError):
                    machine_id(value)
        for value in (None, "", "   ", "bad\nname"):
            with self.subTest(case="invalid-label"):
                with self.assertRaises(ProtocolError):
                    label(value)
        self.assertEqual(machine_id("11111111111141118111111111111111"),
                         "11111111-1111-4111-8111-111111111111")
        self.assertEqual(label("x" * 180), "x" * 180)
        with self.assertRaises(ProtocolError):
            label("x" * 181)

    def test_manifest_rejects_bad_version_sender_and_non_object(self):
        record = file_record("a.mp3", b"a")
        cases = [
            None,
            [],
            dict(valid_manifest([record]), version=2),
            dict(valid_manifest([record]), sender_id="bad"),
            dict(valid_manifest([record]), sender_name="x" * 101),
            dict(valid_manifest([record]), crates=[]),
        ]
        for manifest in cases:
            with self.subTest(case="invalid-envelope"):
                with self.assertRaises(ProtocolError):
                    validate_manifest(manifest)

    def test_filenames_reject_traversal_reserved_names_and_unsupported_types(self):
        for filename in ("../escape.mp3", "folder\\escape.mp3", "bad?.mp3", "CON.mp3",
                         "track.mp3.", "track.txt", ".", ".."):
            with self.subTest(case="unsafe-or-unsupported-name"):
                record = file_record(filename, b"content")
                with self.assertRaises(ProtocolError):
                    validate_manifest(valid_manifest([record]))

    def test_case_insensitive_crate_collision_and_bad_hierarchy_are_rejected(self):
        record = file_record("a.mp3", b"a")
        key = record["id"]
        duplicate_paths = [
            {"path": ["Road"], "tracks": [key]},
            {"path": ["road"], "tracks": [key]},
        ]
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([record], duplicate_paths))
        invalid_path = [{"path": [], "tracks": [key]}]
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([record], invalid_path))

    def test_unknown_unreferenced_and_duplicate_file_references_are_handled(self):
        record = file_record("a.mp3", b"a")
        unknown = [{"path": ["Set"], "tracks": ["0" * 64]}]
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([record], unknown))
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([record], [{"path": ["Set"], "tracks": []}]))
        # Repeated membership is legal, but the file remains referenced.
        manifest = valid_manifest([record], [{"path": ["Set"], "tracks": [record["id"], record["id"]]}])
        self.assertIs(validate_manifest(manifest), manifest)

    def test_content_addressed_ids_and_hashes_must_match_exact_name(self):
        record = file_record("a.mp3", b"a")
        corrupted_hash = dict(record, sha256=hashlib.sha256(b"other").hexdigest())
        corrupted_key = dict(record, id="f" * 64)
        for changed in (corrupted_hash, corrupted_key):
            with self.subTest(case="inconsistent-content-id"):
                with self.assertRaises(ProtocolError):
                    validate_manifest(valid_manifest([changed]))

    def test_file_count_size_and_total_size_bounds_are_enforced(self):
        boundary_records = []
        for index in range(MAX_FILES):
            name = "f%d.mp3" % index
            digest = "a" * 64
            key = hashlib.sha256((digest + "\0" + name).encode()).hexdigest()
            boundary_records.append({"id": key, "sha256": digest, "name": name, "size": 1})
        boundary_manifest = valid_manifest(
            boundary_records, [{"path": ["Boundary"], "tracks": [r["id"] for r in boundary_records]}]
        )
        self.assertIs(validate_manifest(boundary_manifest), boundary_manifest)

        too_many = valid_manifest([None] * (MAX_FILES + 1), [{"path": ["Set"], "tracks": []}])
        with self.assertRaises(ProtocolError):
            validate_manifest(too_many)

        oversized = {
            "id": hashlib.sha256(("a" * 64 + "\0a.mp3").encode()).hexdigest(),
            "sha256": "a" * 64,
            "name": "a.mp3",
            "size": MAX_FILE + 1,
        }
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([oversized]))

        at_file_limit = dict(oversized, size=MAX_FILE)
        accepted_file_limit = valid_manifest([at_file_limit])
        self.assertIs(validate_manifest(accepted_file_limit), accepted_file_limit)

        records = []
        for index in range(MAX_TOTAL // MAX_FILE + 1):
            name = "t%d.mp3" % index
            digest = ("%064x" % (index + 1))
            key = hashlib.sha256((digest + "\0" + name).encode()).hexdigest()
            records.append({"id": key, "sha256": digest, "name": name, "size": MAX_FILE})
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest(records))

        at_total_limit = records[:-1]
        accepted = valid_manifest(at_total_limit)
        self.assertIs(validate_manifest(accepted), accepted)

        boolean_size = dict(file_record("b.mp3", b"b"), size=True)
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([boolean_size]))

    def test_crate_count_and_total_membership_limits_are_enforced(self):
        too_many_crates = [
            {"path": ["Crate %d" % index], "tracks": []}
            for index in range(5001)
        ]
        with self.assertRaises(ProtocolError):
            validate_manifest(valid_manifest([], too_many_crates))

        record = file_record("shared.mp3", b"shared")
        tracks = [record["id"]] * 20000
        at_membership_limit = [
            {"path": ["Set %d" % index], "tracks": tracks}
            for index in range(10)
        ]
        exact = valid_manifest([record], at_membership_limit)
        self.assertIs(validate_manifest(exact), exact)
        over_limit = at_membership_limit + [{"path": ["Set 10"], "tracks": [record["id"]]}]
        with self.assertRaisesRegex(ProtocolError, "Too many memberships"):
            validate_manifest(valid_manifest([record], over_limit))


if __name__ == "__main__":
    unittest.main()
