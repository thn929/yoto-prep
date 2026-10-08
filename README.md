# yoto-prep

Prepare one YouTube audiobook using your installed `yt-dlp`, `ffmpeg`, `ffprobe`,
and OpenAI `whisper` CLI. Uses Python 3's standard library; no pip dependencies.
The existing cached `small.en` model is used on CPU with word timestamps.

```sh
yoto-prep 'https://www.youtube.com/watch?v=VIDEO_ID'
yoto-prep crop VIDEO_ID
yoto-prep --auto 'https://www.youtube.com/watch?v=VIDEO_ID'
```

The launcher is linked into `~/.local/bin`. You can also run `./yoto-prep` here.
Paths are anchored to this project, regardless of the shell's current directory.

## Publish finished M4As to Yoto

Once the reviewed M4As exist, define the playlist title plus each track's title,
audio file and icon file in an ordered JSON manifest. The included Dr. Seuss
manifest reproduces the playlist built during development:

```sh
yoto-prep publish playlists/dr-seuss.json
```

The command is resumable and idempotent. It performs the one-time browser login if
no usable Yoto token exists, uploads icons and audio, creates the playlist, and
reads it back to verify its title, order, track URLs and icons. Upload IDs and
transcoding results are saved after every remote operation, so an interruption
resumes instead of starting over.

The exact callback URL in the manifest must be registered for the Public Client in
the Yoto developer portal. `YOTO_CLIENT_ID` is read from the environment or
`~/.hermes/.env`; OAuth tokens remain in the manifest's `token_file`. If a different
playlist already uses the requested title, publishing stops rather than overwriting
it. The publisher never downloads, transcribes, reviews, crops or edits audio.

To publish through a different Yoto app and account, copy `profiles/example.json`
to `~/.config/yoto-prep/profiles/friend.json`, fill in that Public Client's ID, and
register the profile's exact callback URL in its developer settings. Then run:

```sh
yoto-prep publish playlists/dr-seuss.json --profile friend --login
```

The browser login grants access to the second Yoto account. Each profile has its
own OAuth token and upload-state files, so media IDs and refresh-token rotation can
never leak between accounts. `--login` forces the account chooser; omit it on later
runs. A profile filename can be passed instead of a name. Native PKCE authentication
uses a Public Client ID and does not use or store a Client Secret.

- `jobs/<video-id>/source.*`: original audio and yt-dlp metadata.
- `jobs/<video-id>/intro.wav`, `outro.wav`: first 60 / last 240 seconds only.
- `intro.json`, `outro.json`: untouched Whisper results with clip-local word times.
- `intro.txt`, `outro.txt`: readable segments with original-audio timestamps.
- `report.txt`: preview with up to 25 seconds after the intro / before the outro.
- `job.json`: source path, edge offsets, matches, exact crop times and output path.
- `*.log`: diagnostic output from installed tools.
- `output/<title> [<video-id>].m4a`: AAC audio, accurately decoded and trimmed.

Review mode never crops. Crop uses only the persisted source and timestamps;
it never invokes yt-dlp or Whisper. Automatic mode prints/saves the same report
and crops without a prompt only when both boundaries pass the checks.
A failed or ambiguous detection exits nonzero, retaining transcripts and a report.
Whisper timing is approximate even with word alignment; review the preview.
If a boundary is near an edge-window limit, context may be shorter than 25 seconds.

Matching ignores case and punctuation, recognizes `to`/`two`/`too` and `is not`/
`isn't`, and allows one small spelling difference. Every phrase word must align,
have sufficient Whisper probability, and form one unique, tightly timed match.
An exact normalized phrase may contain one weaker word (probability at least 0.2)
if its average is at least 0.65 and the word at the cut is at least 0.7.
Fuzzy matches require every word to reach 0.5 and an average of at least 0.7.
Missing words, low confidence, repeated phrases and reversed cuts fail safely.
Thresholds are deliberately conservative and need validation on real examples.

Rerunning review for the same ID reuses audio and edge JSON files. To retry a
transcription, remove the affected `intro.json` or `outro.json`, then rerun review.
Existing final audio is never overwritten; repeating the same crop returns its path.
For a new crop of an already exported job, move the old output aside first.

Development tests (includes actual ffmpeg cropping of generated audio):

```sh
python3 -m unittest discover -s tests -v
```

If “wow” immediately precedes the outro phrase with a gap of at most two seconds,
the cut moves to its start. It must have valid word timing and probability of at
least 0.7; an uncertain nearby “wow” blocks cropping. Distant or nonadjacent
occurrences stay in the book. The preview shows the complete matched outro.

Before review, FFmpeg checks a short window around each cut for quiet gaps below
-40 dB lasting at least 60 ms. It selects the nearest gap midpoint, searching up
to 350 ms toward the book/phrase gap and 150 ms in the other direction, bounded
by neighboring book-word timestamps with a 30 ms margin. No qualifying gap means
the original Whisper cut is retained; background music may prevent refinement.
This is an acoustic heuristic, not a guarantee of the true word boundary.
The report shows adjustments and fallbacks. `job.json` keeps original matches
and `refinements` separately; `keep` contains the final reviewed cuts.
`boundary-analysis.log` retains FFmpeg diagnostics. Crop performs no new analysis.
Existing jobs need review rerun to apply refinement; existing exports stay intact.

## Phone review

Run `yoto-prep serve` on the Mac and open the printed link in Chrome on your
phone on the same Wi-Fi. Leave the command running (or run it in your existing
terminal multiplexer). The link includes a random access token; it changes when
the server restarts. The server listens on your local network on port 8765;
`--host` and `--port` can override this. No external service or package is used.

Choose a prepared book, preview its beginning/end, and nudge either boundary
by 0.1 seconds or type a timestamp. Adjustments clear old previews; replay to hear
the new cut. Tap **Save timings** to persist the definitive cut without encoding, or
**Crop & download** to save and export immediately. CLI `yoto-prep crop <job-id>`
uses those saved timings and creates a revision without overwriting prior exports. The page offers a direct phone
download. Existing exports are preserved. Reset returns to the last saved times.
The transcript context follows the selected timestamps within the saved edges.

Phone review uses the existing source only; it never downloads or transcribes.
`job.json` stores approved times and the latest export, and `report.txt` appends
a PHONE REVIEW decision. CLI crop reuses those approved times. Running review
mode again recomputes the automatic proposal; new detection clears the phone
approval. Preview audio is temporary and removed after each request.

Preview lengths are independently adjustable on the page from 1–60 seconds,
defaulting to 10 seconds for the intro and 5 seconds for the outro. They affect
only preview playback and its transcript context, never the saved crop timings.
