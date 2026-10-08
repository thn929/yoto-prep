import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import yoto_publish as publish


class WorkflowManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.icon = self.root / "icon.png"
        self.icon.write_bytes(b"png")
        self.audio = self.root / "audio.m4a"
        self.audio.write_bytes(b"audio")
        self.manifest_path = self.root / "playlist.json"

    def write_manifest(self, profile=None, **changes):
        data = {
            "version": 1,
            "title": "Test Playlist",
            "tracks": [{
                "title": "First Book",
                "audio": "audio.m4a",
                "icon": "icon.png",
            }],
        }
        data.update(changes)
        self.manifest_path.write_text(json.dumps(data))
        return publish.load_manifest(self.manifest_path, profile)

    def test_manifest_paths_and_defaults(self):
        manifest = self.write_manifest()
        track = manifest["tracks"][0]
        self.assertEqual(track["audio_path"], self.audio.resolve())
        self.assertEqual(track["icon_path"], self.icon.resolve())
        self.assertEqual(manifest["yoto"]["redirect_uri"],
                         "http://127.0.0.1:8788/callback")

    def test_manifest_rejects_duplicate_titles_and_bad_callback(self):
        duplicate = {
            "title": "First Book", "audio": "audio.m4a", "icon": "icon.png",
        }
        original = {
            "title": "First Book", "audio": "audio.m4a", "icon": "icon.png",
        }
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.write_manifest(tracks=[original, duplicate])
        with self.assertRaisesRegex(ValueError, "redirect_uri"):
            self.write_manifest(yoto={"redirect_uri": "https://example.com/callback"})

    def test_named_account_profile_has_separate_token_and_state(self):
        profile = self.root / "friend.json"
        profile.write_text(json.dumps({
            "client_id": "friend-client",
            "redirect_uri": "http://127.0.0.1:9898/callback",
            "token_file": "friend-token.json",
            "state_dir": "friend-state",
        }))
        manifest = self.write_manifest(profile=str(profile))
        self.assertEqual(manifest["client_id"], "friend-client")
        self.assertEqual(manifest["yoto"]["token_path"],
                         (self.root / "friend-token.json").resolve())
        self.assertEqual(manifest["yoto"]["state_path"],
                         (self.root / "friend-state" / "playlist.json").resolve())
        self.assertEqual(manifest["yoto"]["redirect_uri"],
                         "http://127.0.0.1:9898/callback")

class PayloadTests(unittest.TestCase):
    def test_payload_order_and_card_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "book.m4a"
            icon = root / "icon.png"
            audio.write_bytes(b"audio")
            icon.write_bytes(b"icon")
            audio_digest = publish.sha256(audio)
            icon_digest = publish.sha256(icon)
            manifest = {"title": "Books", "tracks": [{
                "title": "Book", "icon_path": icon,
            }]}
            state = {
                "icons": {icon_digest: "icon-id"},
                "audio": {audio_digest: {"transcode": {
                    "transcodedSha256": "audio-id",
                    "transcodedInfo": {"duration": 12, "fileSize": 34,
                                       "channels": "stereo", "format": "opus"},
                }}},
            }
            payload = publish.build_payload(manifest, state, {"Book": audio})
            chapter = payload["content"]["chapters"][0]
            self.assertEqual(chapter["key"], "01")
            self.assertEqual(chapter["tracks"][0]["trackUrl"], "yoto:#audio-id")
            self.assertEqual(chapter["display"]["icon16x16"], "yoto:#icon-id")
            card = json.loads(json.dumps(payload))
            card["cardId"] = "abcde"
            card["content"]["chapters"][0]["tracks"][0]["duration"] = 13
            self.assertTrue(publish.card_matches(card, payload))
            card["content"]["chapters"][0]["tracks"][0]["trackUrl"] = "yoto:#other"
            self.assertFalse(publish.card_matches(card, payload))

    def test_cached_icon_still_processes_audio_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "book.m4a"
            icon = root / "icon.png"
            audio.write_bytes(b"audio")
            icon.write_bytes(b"icon")
            manifest = {
                "title": "Books",
                "yoto": {"state_path": root / "state.json"},
                "tracks": [{"title": "Book", "audio_path": audio,
                            "icon_path": icon}],
            }
            (root / "state.json").write_text(json.dumps({
                "version": 1, "icons": {publish.sha256(icon): "icon-id"}, "audio": {},
            }))
            api = Mock()
            api.get.side_effect = [
                {"cards": []}, {"cards": []},
                {"card": {"cardId": "abcde", "title": "Books",
                          "content": {"chapters": []}}},
            ]
            api.post.return_value = {"card": {"cardId": "abcde"}}
            transcode = {"transcodedSha256": "audio-id", "transcodedInfo": {
                "duration": 12, "fileSize": 34, "format": "opus"}}
            with patch.object(publish, "access_token", return_value="token"), \
                    patch.object(publish, "YotoApi", return_value=api), \
                    patch.object(publish, "upload_audio",
                                 return_value={"transcode": transcode}):
                with self.assertRaisesRegex(ValueError, "does not match"):
                    publish.publish(manifest, {"Book": audio})
            state = json.loads((root / "state.json").read_text())
            self.assertIn(publish.sha256(audio), state["audio"])


if __name__ == "__main__":
    unittest.main()
