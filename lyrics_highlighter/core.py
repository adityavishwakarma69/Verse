from __future__ import annotations

import re
import subprocess
from pathlib import Path

BG, PANEL, PANEL2, BORDER = '#111216', '#191a20', '#202127', '#303139'
TEXT, MUTED, ACCENT, PURPLE = '#f0eff1', '#858791', '#c8f277', '#b5a1ff'
FONT = 'Segoe UI'
RHYME_COLORS = {'A': '#b42318', 'B': '#175cd3', 'C': '#6941c6', 'D': '#b54708', 'E': '#067647', 'F': '#c11574'}
DEFAULT_RENDER_SETTINGS = {
    'font': 'DejaVu Sans', 'size': 62, 'resolution': '1920x1080',
    'font_color': '#f4f4f0', 'highlight_color': ACCENT, 'visualizer_color': ACCENT,
    'background_color': '#11130f',
    'rhyme_colors': dict(RHYME_COLORS),
}


def ass_color(value):
    value = value.lstrip('#')
    red, green, blue = value[:2], value[2:4], value[4:6]
    return f'&H00{blue}{green}{red}'


def stamp(value):
    value = max(0, value or 0)
    return f'{int(value // 60)}:{int(value % 60):02d}.{int(value * 100) % 100:02d}'


def probe_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def parse_lyrics(text):
    timestamp = re.compile(r'^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]\s*(.*)$')
    section = re.compile(r'^\[([^\]]+)\]$')
    lines = []; pending_label = None
    for raw in text.splitlines():
        row = raw.strip()
        if not row or row.startswith('#'): continue
        if section.fullmatch(row) and not timestamp.match(row):
            pending_label = section.fullmatch(row).group(1).strip()
            continue
        match = timestamp.match(row)
        if match:
            lyric, start = match.group(3).strip(), int(match.group(1)) * 60 + float(match.group(2))
        else:
            lyric, start = row, None
        if not lyric: continue
        lines.append({'text': lyric, 'start': start, 'paragraph_start': not lines or pending_label is not None,
                      'paragraph_label': pending_label})
        pending_label = None
    return lines


def ass_time(seconds):
    cs = max(0, round(seconds * 100))
    h, r = divmod(cs, 360000); m, r = divmod(r, 6000); s, cs = divmod(r, 100)
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


def ass_escape(value):
    return value.replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}').replace('\n', r'\N')


def paragraph_parts(lines, max_lines=6):
    groups = []
    for i, line in enumerate(lines):
        if not groups or line.get('paragraph_start', i == 0): groups.append([])
        groups[-1].append(i)
    return [group[start:start + max_lines] for group in groups for start in range(0, len(group), max_lines)]


def lyric_syllables(text):
    """Split words into syllable spans using vowel groups as nuclei."""
    syllables = []
    for word in re.finditer(r'\S+', text):
        nuclei = list(re.finditer(r'[aeiouy]+', word.group(), re.IGNORECASE))
        if not nuclei:
            continue
        cuts = [word.start()]
        for left, right in zip(nuclei, nuclei[1:]):
            gap_start = word.start() + left.end()
            gap_end = word.start() + right.start()
            cuts.append((gap_start + gap_end) // 2)
        cuts.append(word.end())
        for i, nucleus in enumerate(nuclei):
            syllables.append((cuts[i], cuts[i + 1], nucleus.group().casefold()))
    return syllables


def karaoke_text(text, total, highlight_mode, rhyme_spans=None, rhyme_colors=None, sync_segments=None, line_start=None, line_end=None):
    rhyme_colors = rhyme_colors or RHYME_COLORS
    timings = {}
    if sync_segments and line_start is not None:
        ordered = sorted(sync_segments, key=lambda segment: segment.get('offset', 0))
        ordered = [segment for segment in ordered if segment.get('start') is not None]
        if ordered and ordered[0].get('offset', 0) == 0:
            for index, segment in enumerate(ordered):
                left = max(0, min(len(text), segment.get('offset', 0)))
                right = max(left, min(len(text), ordered[index + 1].get('offset', len(text)) if index + 1 < len(ordered) else len(text)))
                begin = segment['start']
                finish = ordered[index + 1]['start'] if index + 1 < len(ordered) else line_end
                if finish is None or finish <= begin: finish = line_end
                if finish is None or finish <= begin: continue
                duration = max(1, round((finish - begin) * 100))
                if highlight_mode == 'character':
                    positions = [i for i in range(left, right) if not text[i].isspace()]
                elif highlight_mode == 'smooth':
                    positions = [left] if right > left else []
                else:
                    positions = [match.start() for match in re.finditer(r'\S+', text[left:right])]
                    positions = [left + position for position in positions]
                if not positions: continue
                boundaries = [round(i * duration / len(positions)) for i in range(len(positions) + 1)]
                for i, position in enumerate(positions): timings[position] = max(1, boundaries[i + 1] - boundaries[i])
    if highlight_mode == 'character':
        if not timings:
            characters = list(text)
            spoken = sum(not char.isspace() for char in characters) or 1
            spans = [max(1, round(total / spoken)) if not char.isspace() else 0 for char in characters]
            excess = sum(spans) - total
            for i in range(len(spans) - 1, -1, -1):
                reduction = min(max(0, spans[i] - 1), max(0, excess))
                spans[i] -= reduction; excess -= reduction
            timings = {i: centis for i, centis in enumerate(spans) if centis}
    elif highlight_mode == 'smooth':
        if not timings: timings = {0: max(1, total)}
    else:
        if not timings:
            words = list(re.finditer(r'\S+', text)); count = max(1, len(words)); per_word = max(1, total // count)
            for match in words: timings[match.start()] = per_word
    scheme_at = {}
    for span in rhyme_spans or []:
        scheme = span.get('scheme')
        if scheme in rhyme_colors:
            for i in range(max(0, span['start']), min(len(text), span['end'])): scheme_at[i] = scheme
    output = []; active_scheme = None
    for i, char in enumerate(text):
        if i in timings: output.append(f'{{\\{"kf" if highlight_mode == "smooth" else "k"}{timings[i]}}}')
        scheme = scheme_at.get(i)
        if scheme != active_scheme or (scheme and i in timings):
            if scheme:
                hex_color = rhyme_colors[scheme].lstrip('#')
                red, green, blue = hex_color[:2], hex_color[2:4], hex_color[4:6]
                output.append(f'{{\\1c&H00{blue}{green}{red}&\\3c&H00{blue}{green}{red}&\\bord7}}')
            elif active_scheme:
                output.append(f'{{\\1c{ass_color(ACCENT)}&\\3c&H00101010&\\bord2.5}}')
            active_scheme = scheme
        output.append(ass_escape(char))
    if active_scheme: output.append(f'{{\\1c{ass_color(ACCENT)}&\\3c&H00101010&\\bord2.5}}')
    return ''.join(output)


def make_ass(lines, duration, display_mode, highlight_mode, alignment='center', render_settings=None):
    settings = dict(DEFAULT_RENDER_SETTINGS)
    settings.update(render_settings or {})
    settings['rhyme_colors'] = {**RHYME_COLORS, **settings.get('rhyme_colors', {})}
    try: size = max(8, min(300, int(settings['size'])))
    except (TypeError, ValueError): size = DEFAULT_RENDER_SETTINGS['size']
    try: width, height = (int(value) for value in settings['resolution'].lower().split('x', 1))
    except (TypeError, ValueError): width, height = (1920, 1080)
    font = str(settings['font']).replace(',', ' ').strip() or 'DejaVu Sans'
    font_color = ass_color(settings['font_color'])
    highlight_color = ass_color(settings.get('highlight_color', ACCENT))
    header = f'''[Script Info]
Title: Verse Lyrics Video
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyrics,{font},{size},{font_color},{font_color},&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,0,5,120,120,280,1
Style: Karaoke,{font},{size},{highlight_color},{font_color},&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,0,5,120,120,280,1
Style: Song,{font},22,&H009AD9C1,&H0077F2C8,&H00101010,&H80000000,0,0,0,0,100,100,2,0,1,1,0,2,120,120,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    ass_alignment = {'left': 4, 'center': 5, 'right': 6}.get(alignment.lower(), 5)
    header = header.replace('0,5,120,120,280,1', f'0,{ass_alignment},120,120,280,1')
    events = [header]
    if display_mode == 'scroll':
        timed = [(i, line) for i, line in enumerate(lines) if line.get('start') is not None and str(line.get('text', '')).strip()]
        gap = round(size * 1.35)
        center_y = round(height * .55)
        x = 120 if ass_alignment == 4 else width - 120 if ass_alignment == 6 else round(width / 2)
        for timed_index, (line_index, line) in enumerate(timed):
            start = line['start']; text = str(line.get('text', '')).strip()
            next_start = timed[timed_index + 1][1]['start'] if timed_index + 1 < len(timed) else None
            line_end = next_start if next_start is not None and next_start > start else min(duration, start + 5)
            line_end = max(start + .1, line_end)
            future_index = min(len(timed) - 1, timed_index + 5)
            move_end = timed[future_index][1]['start'] if future_index > timed_index else duration
            if move_end is None or move_end <= start: move_end = min(duration, start + 5)
            move_end = max(start + .1, move_end)
            move_ms = max(1, round((move_end - start) * 1000))
            move = f'{{\\an{ass_alignment}\\move({x},{center_y},{x},{center_y - gap * 5},0,{move_ms})}}'
            events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(move_end)},Lyrics,,0,0,0,,{move}{ass_escape(text)}\n')
            total = max(1, round((line_end - start) * 100))
            karaoke = karaoke_text(text, total, highlight_mode, line.get('rhyme_spans'), settings['rhyme_colors'], line.get('sync_segments'), start, line_end)
            events.append(f'Dialogue: 1,{ass_time(start)},{ass_time(line_end)},Karaoke,,0,0,0,,{move}{karaoke}\n')
        return ''.join(events)
    if display_mode == 'paragraph':
        for indices in paragraph_parts(lines):
            group = [(i, lines[i]) for i in indices]
            timed = [(i, line) for i, line in group if line.get('start') is not None and str(line.get('text', '')).strip()]
            if not timed: continue
            begin = timed[0][1]['start']
            last_index, last_line = timed[-1]
            following = lines[last_index + 1].get('start') if last_index + 1 < len(lines) else None
            end = min(duration, following if following is not None and following > last_line['start'] else last_line['start'] + 5)
            end = max(begin + .1, end)
            base_body = r'\N'.join(ass_escape(str(line.get('text', '')).strip()) for _, line in group if str(line.get('text', '')).strip())
            events.append(f'Dialogue: 0,{ass_time(begin)},{ass_time(end)},Lyrics,,0,0,0,,{base_body}\n')
            align_code = {'left': 4, 'center': 5, 'right': 6}.get(alignment.lower(), 5)
            row_count = max(1, len(group))
            row_spacing = round(size * 1.2)
            center_y = round(height / 2)
            for i, line in timed:
                start = line['start']; text = str(line.get('text', '')).strip()
                nxt = lines[i + 1].get('start') if i + 1 < len(lines) else None
                line_end = nxt if nxt is not None and nxt > start else min(duration, start + 5)
                line_end = max(start + .1, line_end)
                total = max(1, round((line_end - start) * 100))
                row = i - indices[0]
                y = center_y + round((row - (row_count - 1) / 2) * row_spacing)
                x = 120 if align_code == 4 else width - 120 if align_code == 6 else round(width / 2)
                position = f'{{\\an{align_code}\\pos({x},{y})}}'
                body = position + karaoke_text(text, total, highlight_mode, line.get('rhyme_spans'), settings['rhyme_colors'], line.get('sync_segments'), start, line_end)
                events.append(f'Dialogue: 1,{ass_time(start)},{ass_time(end)},Karaoke,,0,0,0,,{body}\n')
        return ''.join(events)
    for i, line in enumerate(lines):
        start = line.get('start')
        text = str(line.get('text', '')).strip()
        if start is None or not text:
            continue
        nxt = lines[i + 1].get('start') if i + 1 < len(lines) else None
        end = nxt if nxt is not None and nxt > start else min(duration, start + 5)
        end = max(start + .1, end)
        events.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Lyrics,,0,0,0,,{ass_escape(text)}\n')
        total = max(1, round((end - start) * 100))
        karaoke = karaoke_text(text, total, highlight_mode, line.get('rhyme_spans'), settings['rhyme_colors'], line.get('sync_segments'), start, end)
        events.append(f'Dialogue: 1,{ass_time(start)},{ass_time(end)},Karaoke,,0,0,0,,{karaoke}\n')
    return ''.join(events)



def main():
    from .ui import run
    raise SystemExit(run())


if __name__ == '__main__':
    main()
