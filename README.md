# Verse — Lyrics Studio

A local Python desktop app for syncing lyrics to audio and exporting a lyric video.

## Requirements

- Python 3.10+
- PyQt6 (`pip install -r requirements.txt`)
- FFmpeg and ffprobe on your PATH

## Run

```bash
python -m pip install -e .
python -m lyrics_highlighter
```

The app can also be launched with `python main.py`. The importable package exposes lyric parsing and subtitle helpers from `lyrics_highlighter`, while the Qt application lives in `lyrics_highlighter.ui`.

Load an audio track, import `.txt` or `.lrc` lyrics, or add lines manually. Play the song and click **Sync line** (or press `Alt+Enter`) as each line begins. Choose word or character highlighting and export a 1080p MP4 video.

Use **Export rhymesheet** to save all lyric lines as a PNG image, with assigned rhyme spans highlighted in their scheme colors and listed in a legend.

Save projects as `.verse.json`; the project stores lyric text, timestamps, highlight mode, and the audio file path.
