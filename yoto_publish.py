"""Deterministically publish finished audio files as a Yoto MYO playlist."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import shlex
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API_ROOT = "https://api.yotoplay.com"
TOKEN_URL = "https://login.yotoplay.com/oauth/token"
AUTHORIZE_URL = "https://login.yotoplay.com/authorize"
SCOPES = "family:library:view user:content:manage offline_access"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_path(value, base):
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else (base / path).resolve()


def profile_path(value):
    candidate = Path(os.path.expanduser(value))
    if candidate.is_absolute() or candidate.parent != Path(".") or candidate.suffix:
        return candidate.resolve()
    return (Path.home() / ".config" / "yoto-prep" / "profiles" / f"{value}.json").resolve()


def load_manifest(path, profile=None):
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    if data.get("version") != 1:
        raise ValueError("Playlist manifest version must be 1.")
    title = data.get("title")
    tracks = data.get("tracks")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Playlist manifest needs a non-empty title.")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("Playlist manifest needs at least one track.")
    if len(tracks) > 100:
        raise ValueError("Yoto playlists support at most 100 tracks.")
    seen_titles = set()
    for number, track in enumerate(tracks, 1):
        if not isinstance(track, dict):
            raise ValueError(f"Track {number} must be an object.")
        track_title = track.get("title")
        if not isinstance(track_title, str) or not track_title.strip():
            raise ValueError(f"Track {number} needs a non-empty title.")
        if track_title in seen_titles:
            raise ValueError(f"Duplicate track title: {track_title}")
        seen_titles.add(track_title)
        if "audio" not in track or "icon" not in track:
            raise ValueError(f"Track {number} needs audio and icon.")
        track["icon_path"] = manifest_path(track["icon"], path.parent)
        track["audio_path"] = manifest_path(track["audio"], path.parent)
    yoto = dict(data.get("yoto", {}))
    if profile:
        selected = profile_path(profile)
        if not selected.is_file():
            raise ValueError(f"Yoto profile is missing: {selected}")
        configured = json.loads(selected.read_text())
        if not configured.get("client_id"):
            raise ValueError(f"Yoto profile needs a client_id: {selected}")
        yoto = configured
        data["profile_path"] = selected
        data["client_id"] = configured["client_id"]
        token_value = configured.get(
            "token_file", str(Path.home() / ".config" / "yoto-prep" / "tokens" / f"{selected.stem}.json"))
        state_dir = manifest_path(
            configured.get("state_dir", str(Path.home() / ".local" / "state" / "yoto-prep" / selected.stem)),
            selected.parent)
        yoto["token_path"] = manifest_path(token_value, selected.parent)
        yoto["state_path"] = state_dir / f"{path.stem}.json"
    yoto.setdefault("redirect_uri", "http://127.0.0.1:8788/callback")
    parsed = urllib.parse.urlparse(yoto["redirect_uri"])
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path != "/callback":
        raise ValueError("yoto.redirect_uri must be http://127.0.0.1:<port>/callback")
    if not profile:
        token_value = yoto.get("token_file", "~/.hermes/yoto-auth.json")
        yoto["token_path"] = manifest_path(token_value, path.parent)
        default_state = path.parent / ".yoto-publish" / (path.stem + ".json")
        yoto["state_path"] = manifest_path(yoto["state_file"], path.parent) if yoto.get("state_file") else default_state
    data["yoto"] = yoto
    data["path"] = path
    return data


def load_setting(name):
    if os.environ.get(name):
        return os.environ[name]
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.is_file():
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                parts = shlex.split(value, comments=True)
                if len(parts) == 1:
                    return parts[0]
    raise ValueError(f"{name} is missing from the environment and ~/.hermes/.env")


def form_request(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def token_expiring(token, margin=60):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        expires = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return expires <= time.time() + margin
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return True


def refresh_tokens(token_path, tokens, client_id):
    refreshed = form_request(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": tokens["refresh_token"],
    })
    if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
        raise ValueError("Yoto returned an incomplete refresh response.")
    refreshed["obtained_at"] = int(time.time())
    refreshed["scopes_requested"] = SCOPES
    atomic_json(token_path, refreshed)
    return refreshed


def authorize(token_path, client_id, redirect_uri, prompt_login=False):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    params = {
        "audience": API_ROOT,
        "scope": SCOPES,
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if prompt_login:
        params["prompt"] = "login"
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback" or query.get("state", [None])[0] != state:
                self.send_error(400, "Invalid OAuth callback")
                return
            if query.get("error"):
                result["error"] = query["error"][0]
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "missing authorization code"
            body = b"<h2>Yoto authorization received.</h2><p>You can close this tab.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    parsed = urllib.parse.urlparse(redirect_uri)
    server = HTTPServer(("127.0.0.1", parsed.port), Callback)
    try:
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
        print(f"Opening Yoto authorization in the Mac browser; callback: {redirect_uri}", flush=True)
        webbrowser.open(url)
        while not result:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in result:
        raise ValueError("Yoto authorization was not completed.")
    tokens = form_request(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code_verifier": verifier,
        "code": result["code"],
        "redirect_uri": redirect_uri,
    })
    if not tokens.get("access_token") or not tokens.get("refresh_token"):
        raise ValueError("Yoto returned an incomplete authorization response.")
    tokens["obtained_at"] = int(time.time())
    tokens["scopes_requested"] = SCOPES
    atomic_json(token_path, tokens)
    return tokens


def access_token(manifest, force_login=False):
    client_id = manifest.get("client_id") or load_setting("YOTO_CLIENT_ID")
    path = manifest["yoto"]["token_path"]
    if force_login or not path.is_file():
        tokens = authorize(path, client_id, manifest["yoto"]["redirect_uri"], force_login)
    else:
        tokens = json.loads(path.read_text())
    if token_expiring(tokens.get("access_token", "")):
        if not tokens.get("refresh_token"):
            tokens = authorize(path, client_id, manifest["yoto"]["redirect_uri"])
        else:
            tokens = refresh_tokens(path, tokens, client_id)
    return tokens["access_token"]


class YotoApi:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, body=None, content_type=None, timeout=60):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(API_ROOT + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:1000]
            raise ValueError(f"Yoto API {method} {path} failed ({error.code}): {detail}") from error

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body, content_type=None):
        return self.request("POST", path, body, content_type)


def load_state(path):
    if not path.is_file():
        return {"version": 1, "icons": {}, "audio": {}}
    state = json.loads(path.read_text())
    state.setdefault("version", 1)
    state.setdefault("icons", {})
    state.setdefault("audio", {})
    return state


def upload_icon(api, path):
    query = urllib.parse.urlencode({"autoConvert": "true", "filename": path.stem})
    result = api.post("/media/displayIcons/user/me/upload?" + query,
                      path.read_bytes(), "image/png")
    media_id = result.get("displayIcon", {}).get("mediaId")
    if not media_id:
        raise ValueError(f"Yoto did not return a mediaId for icon {path}")
    return media_id


def upload_audio(api, path, previous=None):
    entry = dict(previous or {})
    transcode = entry.get("transcode", {})
    if transcode.get("transcodedSha256"):
        return entry
    if not entry.get("uploadId"):
        upload = api.get("/media/transcode/audio/uploadUrl").get("upload", {})
        if not upload.get("uploadUrl") or not upload.get("uploadId"):
            raise ValueError("Yoto did not return an audio upload URL.")
        request = urllib.request.Request(upload["uploadUrl"], data=path.read_bytes(), method="PUT",
                                         headers={"Content-Type": "audio/mp4"})
        with urllib.request.urlopen(request, timeout=300) as response:
            if response.status not in (200, 201, 204):
                raise ValueError(f"Audio upload failed with status {response.status}.")
        entry["uploadId"] = upload["uploadId"]
    deadline = time.monotonic() + 15 * 60
    while time.monotonic() < deadline:
        result = api.get(f"/media/upload/{entry['uploadId']}/transcoded?loudnorm=false")
        transcode = result.get("transcode", {})
        if transcode.get("transcodedSha256"):
            entry["transcode"] = transcode
            return entry
        time.sleep(3)
    raise ValueError(f"Yoto transcoding timed out for {path.name}")


def build_payload(manifest, state, audio_paths):
    chapters = []
    total_duration = 0
    total_size = 0
    for number, track in enumerate(manifest["tracks"], 1):
        audio_digest = sha256(audio_paths[track["title"]])
        transcode = state["audio"][audio_digest]["transcode"]
        info = transcode["transcodedInfo"]
        icon_media_id = state["icons"][sha256(track["icon_path"])]
        icon = "yoto:#" + icon_media_id
        item = {
            "key": "01", "title": track["title"],
            "trackUrl": "yoto:#" + transcode["transcodedSha256"],
            "duration": info["duration"], "fileSize": info["fileSize"],
            "channels": info.get("channels"), "format": info["format"],
            "type": "audio", "overlayLabel": str(number),
            "display": {"icon16x16": icon},
        }
        item = {key: value for key, value in item.items() if value is not None}
        chapters.append({
            "key": f"{number:02d}", "title": track["title"],
            "overlayLabel": str(number), "display": {"icon16x16": icon},
            "tracks": [item],
        })
        total_duration += info["duration"]
        total_size += info["fileSize"]
    return {
        "title": manifest["title"],
        "content": {"chapters": chapters, "playbackType": "linear"},
        "metadata": {"media": {"duration": total_duration, "fileSize": total_size}},
    }


def card_matches(card, payload):
    if card.get("title") != payload["title"]:
        return False
    actual = card.get("content", {}).get("chapters", [])
    expected = payload["content"]["chapters"]
    if len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if left.get("title") != right["title"] or left.get("display") != right["display"]:
            return False
        left_tracks = left.get("tracks", [])
        if len(left_tracks) != 1:
            return False
        for key in ("title", "trackUrl", "format", "display"):
            if left_tracks[0].get(key) != right["tracks"][0].get(key):
                return False
    return True


def find_or_create_card(api, payload):
    matches = []
    for summary in api.get("/content/mine").get("cards", []):
        if summary.get("title") == payload["title"]:
            card_id = summary.get("cardId")
            card = api.get("/content/" + card_id).get("card", {})
            if card_matches(card, payload):
                matches.append(card)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("Multiple identical Yoto playlists already exist; refusing to choose one.")
    same_title = [card for card in api.get("/content/mine").get("cards", [])
                  if card.get("title") == payload["title"]]
    if same_title:
        raise ValueError(f'A different Yoto playlist already uses the title "{payload["title"]}".')
    return api.post("/content", payload).get("card", {})


def publish(manifest, audio_paths, force_login=False):
    for track in manifest["tracks"]:
        if not track["icon_path"].is_file():
            raise ValueError(f"Icon file is missing: {track['icon_path']}")
    state_path = manifest["yoto"]["state_path"]
    state = load_state(state_path)
    api = YotoApi(access_token(manifest, force_login))
    for track in manifest["tracks"]:
        icon_digest = sha256(track["icon_path"])
        # Import the state shape used by the original one-off publisher.
        legacy_icon = state["icons"].get(track["icon_path"].stem)
        if icon_digest not in state["icons"] and isinstance(legacy_icon, str):
            state["icons"][icon_digest] = legacy_icon
        if icon_digest not in state["icons"]:
            print(f"Uploading icon: {track['title']}", flush=True)
            state["icons"][icon_digest] = upload_icon(api, track["icon_path"])
            atomic_json(state_path, state)
        audio_path = audio_paths[track["title"]]
        audio_digest = sha256(audio_path)
        entry = state["audio"].get(audio_digest)
        legacy_audio = state["audio"].get(track["title"])
        if (not entry and isinstance(legacy_audio, dict)
                and legacy_audio.get("sha256") == audio_digest):
            entry = legacy_audio
            state["audio"][audio_digest] = legacy_audio
        if not entry or not entry.get("transcode", {}).get("transcodedSha256"):
            print(f"Uploading/transcoding audio: {track['title']}", flush=True)
            state["audio"][audio_digest] = upload_audio(api, audio_path, entry)
            atomic_json(state_path, state)
    payload = build_payload(manifest, state, audio_paths)
    card = find_or_create_card(api, payload)
    if not card.get("cardId"):
        raise ValueError("Yoto did not return a cardId after playlist creation.")
    verified = api.get("/content/" + card["cardId"]).get("card", {})
    if not card_matches(verified, payload):
        raise ValueError("The saved Yoto playlist does not match the manifest.")
    state["card"] = {"cardId": verified["cardId"], "title": verified["title"]}
    atomic_json(state_path, state)
    return verified


def run(manifest_file, profile=None, force_login=False):
    manifest = load_manifest(manifest_file, profile)
    audio_paths = {}
    for track in manifest["tracks"]:
        if not track["audio_path"].is_file():
            raise ValueError(f"Audio file is missing: {track['audio_path']}")
        audio_paths[track["title"]] = track["audio_path"]
    card = publish(manifest, audio_paths, force_login)
    print(f"Verified Yoto playlist: {card['title']} ({card['cardId']})")
    print(f"Tracks: {len(card['content']['chapters'])}")
    return card
