"""Lyrics Highlighter desktop app and lyric rendering utilities."""

from .core import (
    ACCENT,
    BG,
    BORDER,
    DEFAULT_RENDER_SETTINGS,
    FONT,
    MUTED,
    PANEL,
    PANEL2,
    PURPLE,
    RHYME_COLORS,
    TEXT,
    ass_color,
    ass_escape,
    ass_time,
    karaoke_text,
    lyric_syllables,
    make_ass,
    paragraph_parts,
    parse_lyrics,
    probe_duration,
    stamp,
)

__all__ = [
    "ACCENT", "BG", "BORDER", "DEFAULT_RENDER_SETTINGS", "FONT", "MUTED",
    "PANEL", "PANEL2", "PURPLE", "RHYME_COLORS", "TEXT", "ass_color",
    "ass_escape", "ass_time", "karaoke_text", "lyric_syllables", "make_ass",
    "paragraph_parts", "parse_lyrics", "probe_duration", "stamp",
]
