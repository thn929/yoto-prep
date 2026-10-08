"""Prepare one audiobook using installed yt-dlp, ffmpeg and Whisper CLIs."""
import argparse
from contextlib import contextmanager
from difflib import SequenceMatcher
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
PHRASES = {"intro": "you too silly goose", "outro": "isn't reading fun?"}


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text())


def run(args, log):
    with log.open("a") as stream:
        result = subprocess.run([str(a) for a in args], stdout=subprocess.PIPE,
                                stderr=stream, text=True)
    if result.returncode:
        raise ValueError(f"{args[0]} failed (exit {result.returncode}); see {log}")
    return result.stdout.strip()


def require(*names):
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise ValueError("Missing installed tool(s): " + ", ".join(missing))


@contextmanager
def lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Job is already running: {folder.name}") from None
        yield


def video_id(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http"):
        raise ValueError("Provide a YouTube video URL.")
    parts = parsed.path.strip("/").split("/")
    if host == "youtu.be":
        ident = parts[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        ident = parse_qs(parsed.query).get("v", [""])[0]
        if not ident and len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
            ident = parts[1]
    else:
        ident = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident):
        raise ValueError("Provide a single YouTube video URL (playlists are not supported).")
    return ident


def stamp(seconds):
    tenths = round(seconds * 10)
    minutes, remainder = divmod(tenths, 600)
    return f"{minutes:02d}:{remainder // 10:02d}.{remainder % 10}"


def words_from(transcript, offset, length):
    """Keep original word times; never substitute segment times for cut points."""
    words = []
    previous = 0.0
    for segment in transcript.get("segments", []):
        reliable = (segment.get("no_speech_prob", 0) < 0.6
                    and segment.get("avg_logprob", 0) > -1.0)
        for word in segment.get("words", []):
            start, end = float(word["start"]), float(word["end"])
            probability = float(word.get("probability", 0))
            valid = (all(math.isfinite(x) for x in (start, end, probability))
                     and 0 <= start < end <= length + 0.05 and start >= previous - 0.05)
            words.append({"text": word["word"].strip(), "start": start + offset,
                          "end": end + offset, "probability": probability,
                          "reliable": reliable and valid})
            previous = end
    return words


def tokens_from(words):
    tokens = []
    for index, word in enumerate(words):
        text = unicodedata.normalize("NFKC", word["text"]).lower()
        text = text.replace("’", "'").replace("‘", "'").replace("'", "")
        for token in re.findall(r"[a-z]+", text):
            tokens.append({"text": token, "first": index, "last": index})
    # Whisper may split the contraction across two timestamped words.
    merged = []
    i = 0
    while i < len(tokens):
        pair = tuple(t["text"] for t in tokens[i:i + 2])
        if pair in (("is", "not"), ("isn", "t"), ("is", "nt")):
            merged.append({"text": "isnt", "first": tokens[i]["first"],
                           "last": tokens[i + 1]["last"]})
            i += 2
        else:
            merged.append(tokens[i])
            i += 1
    return merged


def detect(words, kind):
    target = ["you", "too", "silly", "goose"] if kind == "intro" else ["isnt", "reading", "fun"]
    tokens = tokens_from(words)
    candidates = []
    for i in range(len(tokens) - len(target) + 1):
        window = tokens[i:i + len(target)]
        actual = [token["text"] for token in window]
        if kind == "intro" and actual[1] in ("to", "two"):
            actual[1] = "too"
        ratios = [SequenceMatcher(None, a, b).ratio() for a, b in zip(actual, target)]
        # A single small spelling difference is allowed; missing/replaced words aren't.
        if min(ratios) < 0.8 or sum(r < 1 for r in ratios) > 1 or sum(ratios) / len(ratios) < 0.93:
            continue
        first, last = window[0]["first"], window[-1]["last"]
        # Don't cut through a Whisper word containing text outside the phrase.
        if (i and tokens[i - 1]["last"] == first) or (i + len(target) < len(tokens) and tokens[i + len(target)]["first"] == last):
            continue
        matched = words[first:last + 1]
        if not all(w["reliable"] for w in matched):
            continue
        probabilities = [w["probability"] for w in matched]
        average = sum(probabilities) / len(probabilities)
        confident = min(probabilities) >= 0.5 and average >= 0.7
        # Exact phrases can survive one uncertain word away from the cut.
        # Keep a strong aligned cut word, and never relax fuzzy matches.
        cut_word = matched[-1] if kind == "intro" else matched[0]
        exact_with_weak_word = (
            all(r == 1 for r in ratios)
            and min(probabilities) >= 0.2
            and sum(p < 0.5 for p in probabilities) <= 1
            and average >= 0.65
            and cut_word["probability"] >= 0.7
        )
        if not (confident or exact_with_weak_word):
            continue
        if matched[-1]["end"] - matched[0]["start"] > 8:
            continue
        if any(b["start"] - a["end"] > 2 for a, b in zip(matched, matched[1:])):
            continue
        if kind == "outro" and i and tokens[i - 1]["text"] == "wow":
            prefix = tokens[i - 1]
            wow = words[prefix["first"]]
            gap = matched[0]["start"] - wow["end"]
            if not math.isfinite(gap) or gap <= 2:
                # A nearby but uncertain prefix invalidates this crop proposal.
                # Never silently fall back to leaving that prefix in the audio.
                shared_word = i > 1 and tokens[i - 2]["last"] == prefix["first"]
                if (not wow["reliable"] or wow["probability"] < 0.7
                        or not math.isfinite(gap) or gap < -0.05 or shared_word):
                    return None, 'OUTRO: preceding "wow" has uncertain timing or confidence'
                matched = words[prefix["first"]:last + 1]
        candidates.append({"start": matched[0]["start"], "end": matched[-1]["end"],
                           "text": " ".join(w["text"] for w in matched),
                           "match_score": round(sum(ratios) / len(ratios), 3)})
    if len(candidates) != 1:
        reason = "not found confidently" if not candidates else f"ambiguous ({len(candidates)} matches)"
        return None, f"{kind.upper()}: {reason}"
    return candidates[0], None


def quiet_gaps(source, low, high, log):
    """Return source-relative quiet intervals from a tiny decoded audio window."""
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info",
            "-ss", str(low), "-i", str(source), "-t", str(high - low),
            "-map", "0:a:0", "-af", "asetpts=PTS-STARTPTS,silencedetect=noise=-40dB:d=0.06",
            "-f", "null", "-"]
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    with log.open("a") as stream:
        stream.write(result.stderr)
    if result.returncode:
        raise ValueError(f"Audio boundary analysis failed; see {log}")
    gaps, start = [], None
    for event, value in re.findall(r"silence_(start|end):\s*([-+0-9.eE]+)", result.stderr):
        time = max(low, min(high, low + float(value)))
        if event == "start":
            start = time
        elif start is not None:
            gaps.append((start, time))
            start = None
    if start is not None:
        gaps.append((start, high))
    return gaps


def refine_boundary(source, match, words, kind, duration, log):
    original = match["end"] if kind == "intro" else match["start"]
    # Search mostly toward the gap between the removed phrase and the book.
    low = max(0, original - (0.15 if kind == "intro" else 0.35))
    high = min(duration, original + (0.35 if kind == "intro" else 0.15))
    if kind == "intro":
        low = max(low, match["start"] + 0.03)
        neighbors = [w for w in words if w["start"] >= original]
        if neighbors:
            high = min(high, neighbors[0]["start"] - 0.03)
    else:
        high = min(high, match["end"] - 0.03)
        neighbors = [w for w in words if w["end"] <= original]
        if neighbors:
            low = max(low, neighbors[-1]["end"] + 0.03)
    result = {"original": original, "selected": original, "method": "whisper",
              "reason": "No clear quiet gap within the protected search window."}
    if high - low < 0.06:
        return result
    gaps = quiet_gaps(source, low, high, log)
    candidates = [(max(low, a), min(high, b)) for a, b in gaps]
    candidates = [(a, b) for a, b in candidates if b - a >= 0.06 - 1e-6]
    if candidates:
        a, b = min(candidates, key=lambda gap: abs((gap[0] + gap[1]) / 2 - original))
        result.update(selected=(a + b) / 2, method="silence", gap=[a, b],
                      reason="Midpoint of nearby quiet gap (-40 dB, at least 60 ms).")
    return result


def context(words, boundary, after):
    selected = [w["text"] for w in words
                if (boundary <= w["start"] < boundary + 25 if after
                    else boundary - 25 < w["end"] <= boundary)]
    return " ".join(selected) or "[No transcript context available in this edge window.]"


def report(job, folder, edge_words):
    lines = [f"{job['title']} ({job['id']})", ""]
    for kind in PHRASES:
        lines.append(kind.upper())
        match = job.get("boundaries", {}).get(kind)
        if match:
            boundary = match["end"] if kind == "intro" else match["start"]
            excerpt = context(edge_words[kind], boundary, kind == "intro")
            refinement = job.get("refinements", {}).get(kind)
            if refinement:
                boundary = refinement["selected"]
            if kind == "intro":
                lines += [f'"{match["text"]}" | {stamp(boundary)}', f'  -> "{excerpt}"']
            else:
                lines += [f'"{excerpt}"', f'  <- {stamp(boundary)} | "{match["text"]}"']
            if refinement:
                delta_ms = round((boundary - refinement["original"]) * 1000)
                lines.append(f'  Boundary: {refinement["method"]} ({delta_ms:+d} ms). {refinement["reason"]}')
        else:
            lines.append(f'No unique confident match for "{PHRASES[kind]}".')
        lines.append("")
    if job.get("keep"):
        start, end = job["keep"]["start"], job["keep"]["end"]
        lines += ["KEEP", f"{stamp(start)} -> {stamp(end)}", f"Duration: {stamp(end - start)}", "",
                  "Ready to crop:", f"  yoto-prep crop {job['id']}"]
    else:
        lines += ["NOT READY TO CROP", *job.get("errors", [])]
    lines += ["", "Cut times use Whisper word alignment with nearby quiet-gap refinement; verify the context above.",
              "Preview context is limited to the requested edge windows.", f"Job files: {folder}"]
    text = "\n".join(lines) + "\n"
    (folder / "report.txt").write_text(text)
    print(text, flush=True)


def prepare(url, automatic=False):
    require("yt-dlp", "ffmpeg", "ffprobe", "whisper")
    ident = video_id(url)
    folder = (ROOT / "jobs" / ident).resolve()
    with lock(folder):
        state = folder / "job.json"
        job = read(state) if state.exists() else {"version": 1, "id": ident, "url": url, "title": ident}
        # Clear old approval data before any new attempt.
        job.update(status="preparing", boundaries={}, refinements={}, errors=[])
        job.pop("keep", None)
        job.pop("manual_review", None)
        job.pop("automatic_keep", None)
        save(state, job)
        edge_words = {"intro": [], "outro": []}
        try:
            source = Path(job.get("source", folder / "missing"))
            if not source.is_file():
                print("Downloading audio...", flush=True)
                downloaded = run(["yt-dlp", "--ignore-config", "--no-playlist", "--no-progress",
                                  "--write-info-json", "-f", "bestaudio", "-x", "--audio-format", "best",
                                  "-o", folder / "source.%(ext)s", "--print", "after_move:filepath",
                                  "--", f"https://www.youtube.com/watch?v={ident}"], folder / "download.log")
                source = Path(downloaded.splitlines()[-1]).resolve()
                if source.parent != folder or not source.is_file():
                    raise ValueError("yt-dlp did not return the expected source audio path.")
                metadata = read(folder / "source.info.json")
                job.update(source=str(source), title=metadata.get("title") or ident)
                save(state, job)
            duration = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=noprint_wrappers=1:nokey=1", source], folder / "ffmpeg.log"))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid source audio duration.")
            job["duration"] = duration
            job["model"] = "small.en"
            job["edges"] = {}
            for kind, offset, length in (("intro", 0, min(60, duration)),
                                         ("outro", max(0, duration - 240), min(240, duration))):
                clip = folder / f"{kind}.wav"
                transcript = folder / f"{kind}.json"
                job["edges"][kind] = {"offset": offset, "duration": length, "transcript": str(transcript)}
                if not transcript.exists():
                    print(f"Transcribing {kind} edge ({stamp(length)})...", flush=True)
                    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source,
                         "-ss", offset, "-t", length, "-vn", "-ac", "1", "-ar", "16000", clip], folder / "ffmpeg.log")
                    run(["whisper", clip, "--model", job["model"], "--language", "en",
                         "--task", "transcribe", "--word_timestamps", "True", "--fp16", "False",
                         "--device", "cpu", "--output_format", "json", "--output_dir", folder,
                         "--verbose", "False", "--condition_on_previous_text", "False"], folder / "whisper.log")
                data = read(transcript)
                edge_words[kind] = words_from(data, offset, length)
                (folder / f"{kind}.txt").write_text("\n".join(
                    f"[{stamp(float(s['start']) + offset)} -> {stamp(float(s['end']) + offset)}] {s['text'].strip()}"
                    for s in data.get("segments", [])) + "\n")
                match, error = detect(edge_words[kind], kind)
                job["boundaries"][kind] = match
                if error:
                    job["errors"].append(error)
                save(state, job)
            if not job["errors"]:
                start, end = job["boundaries"]["intro"]["end"], job["boundaries"]["outro"]["start"]
                if not 0 <= start < end <= duration:
                    job["errors"].append("Detected boundaries are reversed or outside the audio.")
                else:
                    for kind in PHRASES:
                        job["refinements"][kind] = refine_boundary(
                            source, job["boundaries"][kind], edge_words[kind], kind,
                            duration, folder / "boundary-analysis.log")
                    start = job["refinements"]["intro"]["selected"]
                    end = job["refinements"]["outro"]["selected"]
                    if not 0 <= start < end <= duration:
                        job["errors"].append("Refined boundaries are reversed or outside the audio.")
                    else:
                        job["keep"] = {"start": start, "end": end}
            job["status"] = "ready" if job.get("keep") else "needs_review"
            save(state, job)
            report(job, folder, edge_words)
        except (ValueError, OSError, KeyError, IndexError) as error:
            job.update(status="failed", errors=[str(error)])
            job.pop("keep", None)
            save(state, job)
            report(job, folder, edge_words)
            raise ValueError(f"Preparation failed; inspect {folder / 'report.txt'}") from error
        if not job.get("keep"):
            raise ValueError(f"No safe crop proposed; inspect {folder / 'report.txt'}")
    if automatic:
        crop(ident)


def crop(ident):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ident):
        raise ValueError("Invalid job ID; use the video ID shown in the report.")
    folder = (ROOT / "jobs" / ident).resolve()
    if not (folder / "job.json").is_file():
        raise ValueError(f"Unknown job: {ident}")
    saved = read(folder / "job.json")
    if saved.get("manual_review") and saved.get("status") in ("ready", "cropped"):
        from yoto_review import export
        output = export(ident, saved["keep"]["start"], saved["keep"]["end"])
        print(f"Output: {output}")
        return
    with lock(folder):
        job = read(folder / "job.json")
        keep = job.get("keep")
        if job.get("status") not in ("ready", "cropped") or not keep:
            raise ValueError(f"Job has no safe crop; inspect {folder / 'report.txt'}")
        start, end = float(keep["start"]), float(keep["end"])
        if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= job["duration"]:
            raise ValueError("Saved crop timestamps are invalid.")
        source = Path(job["source"])
        if source.parent != folder or not source.is_file():
            raise ValueError(f"Source audio missing or outside the job directory: {source}")
        require("ffmpeg")
        output_dir = ROOT / "output"
        output_dir.mkdir(exist_ok=True)
        title = re.sub(r"[^\w .-]+", "_", job["title"], flags=re.UNICODE).strip(" .")[:120] or ident
        destination = output_dir / f"{title} [{ident}].m4a"
        approved_output = Path(job.get("output", ""))
        if (job.get("cropped_keep") == keep and approved_output.parent == output_dir
                and approved_output.is_file()):
            print(f"Output: {approved_output}")
            return
        if destination.exists():
            if job.get("output") == str(destination) and job.get("cropped_keep") == keep:
                print(f"Output: {destination}")
                return
            raise ValueError(f"Output already exists; move it aside before cropping: {destination}")
        temporary = output_dir / f".{ident}.partial.m4a"
        try:
            # Decode then trim at sample boundaries; stream-copy seeking is not accurate enough.
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source, "-map", "0:a:0",
                 "-af", f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS",
                 "-vn", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                 temporary], folder / "ffmpeg.log")
            # Hard-link publication refuses to overwrite a destination created concurrently.
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        job.update(status="cropped", output=str(destination), cropped_keep=dict(keep))
        save(folder / "job.json", job)
        print(f"Output: {destination}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "publish":
        publish_parser = argparse.ArgumentParser(
            prog="yoto-prep publish",
            description="Upload finished audio and icons, create a Yoto playlist, and verify it.")
        publish_parser.add_argument("manifest")
        publish_parser.add_argument("--profile",
                                    help="profile name or JSON path for another Yoto app/account")
        publish_parser.add_argument("--login", action="store_true",
                                    help="force a fresh browser login for the selected profile")
        options = publish_parser.parse_args(argv[1:])
        from yoto_publish import run as publish
        try:
            publish(options.manifest, profile=options.profile, force_login=options.login)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted; publishing state has been preserved.", file=sys.stderr)
            return 130
        return 0
    if argv and argv[0] == "serve":
        server_parser = argparse.ArgumentParser(prog="yoto-prep serve", description="Review prepared books from your phone.")
        server_parser.add_argument("--host", default="0.0.0.0")
        server_parser.add_argument("--port", type=int, default=8765)
        options = server_parser.parse_args(argv[1:])
        from yoto_review import serve
        try:
            serve(options.host, options.port)
        except KeyboardInterrupt:
            print("Review server stopped.")
        except (OSError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        return 0
    parser = argparse.ArgumentParser(description="Review and crop a single YouTube audiobook. Use 'yoto-prep serve' for phone review.")
    parser.add_argument("--auto", action="store_true", help="crop immediately if both boundaries are confident")
    parser.add_argument("target", help="YouTube URL, or 'crop'")
    parser.add_argument("job_id", nargs="?", help="video ID for crop")
    args = parser.parse_args(argv)
    if args.target == "crop":
        if not args.job_id or args.auto:
            parser.error("use: yoto-prep crop <job-id>")
    elif args.job_id:
        parser.error("only one video URL is supported")
    try:
        if args.target == "crop":
            crop(args.job_id)
        else:
            prepare(args.target, args.auto)
    except (ValueError, OSError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; saved job files have been preserved.", file=sys.stderr)
        return 130
    return 0
