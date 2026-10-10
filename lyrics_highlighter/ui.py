"""PyQt6 interface for Verse Lyrics Studio."""
from __future__ import annotations

import json
import copy
import math
import sys
from array import array
from difflib import SequenceMatcher
import re
import shutil
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QUrl, QRect, QRectF, QPointF, QEvent
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut, QTextCursor, QImage, QFontDatabase
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QCompleter, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget, QFormLayout, QDialogButtonBox,
)

from .core import ACCENT, BG, BORDER, DEFAULT_RENDER_SETTINGS, FONT, MUTED, PANEL, PANEL2, PURPLE, RHYME_COLORS, TEXT, parse_lyrics, probe_duration, stamp

class SignalBridge(QObject):
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)


def paint_verse_frame(p, state, w, h, position, reference_height, show_preview_chrome=True):
        display_mode = str(state.display_mode).lower()
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(QRect(0, 0, w, h), QColor(state.render_settings['background_color']))
        scale = h / max(1, reference_height)
        if show_preview_chrome:
            p.setPen(QColor('#1b1d18'))
            p.drawLine(w // 2, 0, w // 2, h)
            p.drawLine(0, h // 2, w, h // 2)
            p.setPen(QColor(ACCENT)); p.setFont(QFont('Consolas', max(1, round(9 * scale))))
            p.drawText(14, 23, '●  LIVE PREVIEW')
            p.setPen(QColor('#62655d'))
            p.drawText(QRect(0, 0, w - 14, 35), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, state.render_settings['resolution'])
        _draw_audio_visualizer(p, state.visualizer, w, h, position, state.render_settings.get('visualizer_color', ACCENT))
        idx = -1
        for line_index, line in enumerate(state.lines):
            if line.get('start') is not None and line['start'] <= position: idx = line_index
        if idx < 0 and state.lines: idx = 0
        timed_indices = [i for i, line in enumerate(state.lines) if line.get('start') is not None]
        timed_rank = {line_index: rank for rank, line_index in enumerate(timed_indices)}
        if idx < 0:
            lyric = 'Your lyrics will appear here'
            progress = 0.0
        else:
            line = state.lines[idx]
            lyric = line['text']
            if display_mode == 'scroll' and timed_indices:
                previous = [line_index for line_index in timed_indices if state.lines[line_index]['start'] <= position]
                idx = previous[-1] if previous else timed_indices[0]
                line = state.lines[idx]; lyric = line['text']
                rank = timed_rank[idx]
                nxt = state.lines[timed_indices[rank + 1]]['start'] if rank + 1 < len(timed_indices) else None
            else:
                nxt = state.lines[idx + 1].get('start') if idx + 1 < len(state.lines) else None
            end = nxt if nxt is not None and nxt > (line.get('start') or 0) else (line.get('start') or 0) + 4
            progress = max(0, min(1, (position - (line.get('start') or 0)) / max(.5, end - (line.get('start') or 0))))
        base_size = max(4, round(state.render_settings['size'] * h / 1080 * .75))
        lines_to_draw = [(idx, lyric)]
        paragraph_line_count = 1
        scroll_mode = idx >= 0 and display_mode == 'scroll'
        if scroll_mode:
            rank = timed_rank[idx]
            lines_to_draw = [(line_index, state.lines[line_index]['text']) for line_index in timed_indices
                             if abs(timed_rank[line_index] - rank) <= 5]
            paragraph_line_count = 5
        if idx >= 0 and display_mode == 'paragraph':
            start = idx
            while start > 0 and not state.lines[start].get('paragraph_start', False): start -= 1
            end = idx + 1
            while end < len(state.lines) and not state.lines[end].get('paragraph_start', False): end += 1
            paragraph = list(range(start, end))
            chunk = (idx - start) // 6
            paragraph = paragraph[chunk * 6:(chunk + 1) * 6]
            paragraph_line_count = len(paragraph)
            active_row = paragraph.index(idx)
            visible_end = min(paragraph_line_count, active_row + 1 + (1 if progress > 0 else 0))
            lines_to_draw = [(line_index, state.lines[line_index]['text']) for line_index in paragraph[:visible_end]]
            base_size = max(4, round(base_size * min(1.0, 4.0 / max(4, paragraph_line_count))))
        preview_font = QFont(state.render_settings['font'], base_size, QFont.Weight.Bold)
        if scroll_mode:
            preview_font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
            text_layer = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            text_layer.fill(Qt.GlobalColor.transparent)
            text_painter = QPainter(text_layer)
            text_painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        else:
            text_layer = None
            text_painter = p
        text_painter.setFont(preview_font)
        fm = text_painter.fontMetrics(); line_gap = max(fm.lineSpacing() + 5, min(fm.lineSpacing() * 1.35, h * .55 / max(1, paragraph_line_count)))
        if scroll_mode: line_gap = round(line_gap)
        y = h * .55 - (paragraph_line_count - 1) * line_gap / 2
        if display_mode == 'line': y = (h + fm.ascent() - fm.descent()) / 2
        paragraph_start = idx
        while paragraph_start > 0 and not state.lines[paragraph_start].get('paragraph_start', False): paragraph_start -= 1
        for line_index, row_text in lines_to_draw:
            row_offset = line_index - paragraph_start
            if scroll_mode:
                row_offset = timed_rank[line_index] - timed_rank[idx]
            elif display_mode == 'line':
                row_offset = 0
            elif paragraph_line_count > 1:
                row_offset %= 6
            baseline = (h * .55 if scroll_mode else y) + row_offset * line_gap
            if not scroll_mode: baseline = int(round(baseline))
            width = fm.horizontalAdvance(row_text)
            alignment = state.alignment.lower()
            x = int(w * .16) if alignment == 'left' else int(w * .84 - width) if alignment == 'right' else int((w - width) / 2)
            line_start = state.lines[line_index].get('start') if line_index >= 0 else None
            completed = line_index != idx and line_start is not None and line_start <= position
            row_progress = 1.0 if completed else (progress if line_index == idx else 0.0)
            count = 0
            if line_index == idx or completed:
                if completed:
                    count = len(row_text)
                elif state.highlight_mode in ('character', 'smooth'):
                    count = int(len(row_text) * progress)
                else:
                    count = len(''.join(re.findall(r'\S+\s*', row_text)[:int(progress * max(1, len(re.findall(r'\S+\s*', row_text))))]))
                for span in state.lines[line_index].get('rhyme_spans', []):
                    scheme_color = state.render_settings.get('rhyme_colors', {}).get(span.get('scheme'))
                    span_start = max(0, min(len(row_text), span.get('start', 0)))
                    span_end = max(span_start, min(len(row_text), span.get('end', 0)))
                    if not scheme_color or span_end <= span_start: continue
                    if state.highlight_mode == 'smooth':
                        left = x + fm.horizontalAdvance(row_text[:span_start])
                        right = min(x + fm.horizontalAdvance(row_text[:span_end]), x + fm.horizontalAdvance(row_text) * row_progress)
                        if right > left:
                            text_painter.fillRect(QRectF(left, int(baseline) - fm.ascent(), right - left, fm.height()), QColor(scheme_color))
                    else:
                        span_end = min(span_end, count)
                        if span_end > span_start:
                            left = x + fm.horizontalAdvance(row_text[:span_start])
                            span_width = fm.horizontalAdvance(row_text[span_start:span_end])
                            text_painter.fillRect(QRect(left, int(baseline) - fm.ascent(), span_width, fm.height()), QColor(scheme_color))
            # Draw the complete string with identical metrics every frame, then
            # clip a second pass to color the progressed portion. Splitting the
            # string into two drawText calls changed kerning at the moving edge.
            text_painter.setPen(QColor(state.render_settings['font_color']))
            text_painter.drawText(x, int(baseline), row_text)
            if row_progress > 0:
                highlight_width = (fm.horizontalAdvance(row_text) * row_progress if state.highlight_mode == 'smooth'
                                   else fm.horizontalAdvance(row_text[:count]))
                text_painter.save()
                clip_rect = QRectF(x, int(baseline) - fm.ascent(), highlight_width, fm.height()) if state.highlight_mode == 'smooth' else QRect(x, int(baseline) - fm.ascent(), highlight_width, fm.height())
                text_painter.setClipRect(clip_rect)
                text_painter.setPen(QColor(state.render_settings.get('highlight_color', ACCENT)))
                text_painter.drawText(x, int(baseline), row_text)
                text_painter.restore()
        if scroll_mode:
            text_painter.end()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QPointF(0, -progress * line_gap), text_layer)
        if state.duration and show_preview_chrome:
            p.fillRect(0, h - 3, int(w * position / state.duration), 3, QColor(ACCENT))


def _draw_audio_visualizer(p, values, width, height, position, visualizer_color=ACCENT):
    if not values: return
    # Each time slice contains fixed low-to-high frequency bands. Only their
    # amplitudes change, so the visual reads as a spectrum rather than a
    # waveform moving horizontally.
    frame_rate = 20
    frame_position = max(0.0, min(len(values) - 1, position * frame_rate))
    frame_index = math.floor(frame_position)
    next_index = min(frame_index + 1, len(values) - 1)
    fraction = frame_position - frame_index
    amplitudes = [a * (1 - fraction) + b * fraction for a, b in zip(values[frame_index], values[next_index])]
    count = len(amplitudes)
    baseline = height - 6
    slot = width / count
    color = QColor(visualizer_color); color.setAlpha(150); p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
    max_bar = max(7, round(height * .09))
    for index in range(count):
        amplitude = max(.06, min(1.0, amplitudes[index]))
        bar_height = max(2, round(max_bar * amplitude))
        bar_width = max(1, round(slot * .47))
        x = round(index * slot + (slot - bar_width) / 2)
        p.drawRoundedRect(QRect(x, baseline - bar_height, bar_width, bar_height), bar_width / 2, bar_width / 2)
    p.setBrush(Qt.BrushStyle.NoBrush)


def make_rhymesheet(lines, settings, title='RHYMESHEET'):
    """Render all lyrics with rhyme spans as highlighted text in a PNG."""
    settings = {**DEFAULT_RENDER_SETTINGS, **settings}
    settings['rhyme_colors'] = {**RHYME_COLORS, **settings.get('rhyme_colors', {})}
    try:
        width = max(900, min(2400, int(settings['resolution'].split('x', 1)[0])))
    except (TypeError, ValueError):
        width = 1920
    margin = 112
    font_size = max(18, min(54, int(settings.get('size', 62) * .55)))
    font = QFont(settings.get('font', 'DejaVu Sans'), font_size, QFont.Weight.DemiBold)
    # Use a temporary painter for device-correct metrics before allocating the canvas.
    probe = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
    probe_painter = QPainter(probe)
    probe_painter.setFont(font)
    fm = probe_painter.fontMetrics()
    available = width - margin * 2
    rows = []
    for index, line in enumerate(lines):
        text = str(line.get('text', ''))
        if not text:
            rows.append((index, text, 0, 0))
            continue
        start = 0
        while start < len(text):
            end = start
            last_space = -1
            while end < len(text) and fm.horizontalAdvance(text[start:end + 1]) <= available:
                if text[end].isspace():
                    last_space = end
                end += 1
            if end == start:
                end += 1
            elif end < len(text) and last_space >= start:
                end = last_space
                while end > start and text[end - 1].isspace():
                    end -= 1
            rows.append((index, text, start, end))
            start = end
            while start < len(text) and text[start].isspace():
                start += 1
    probe_painter.end()
    title_font = QFont(settings.get('font', 'DejaVu Sans'), 26, QFont.Weight.Bold)
    header_height = 190
    row_height = fm.lineSpacing() + 24
    section_gap = 20
    # Keep each lyric column to a readable page height, then continue in columns
    # to the right for long songs.
    column_limit = 2500
    groups = [[]]
    used = header_height + 30
    previous_index = -1
    for row in rows:
        index = row[0]
        gap = section_gap if index != previous_index and lines[index].get('paragraph_start') and previous_index >= 0 else 0
        if groups[-1] and used + gap + row_height > column_limit:
            groups.append([])
            used = header_height + 30
            gap = 0
        groups[-1].append(row)
        used += gap + row_height
        previous_index = index
    column_count = len(groups)
    height = max(350, min(column_limit, header_height + 30 + max(1, max(map(len, groups))) * (row_height + section_gap)))
    image = QImage(width * column_count, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(settings['background_color']))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setPen(QColor(ACCENT)); p.setFont(QFont(settings.get('font', 'DejaVu Sans'), 12, QFont.Weight.Bold))
    p.drawText(margin, 58, 'VERSE  /  RHYME SCHEME')
    p.setPen(QColor(settings['font_color'])); p.setFont(title_font)
    p.drawText(QRect(margin, 76, width * column_count - margin * 2, 48), Qt.AlignmentFlag.AlignVCenter, title)
    p.setPen(QColor(MUTED)); p.setFont(QFont(settings.get('font', 'DejaVu Sans'), 12))
    p.drawText(margin, 151, f'{len(lines)} lyric lines  ·  highlighted rhyme schemes')
    schemes = sorted({span.get('scheme') for line in lines for span in line.get('rhyme_spans', []) if span.get('scheme')})
    lx = margin
    for scheme in schemes:
        color = QColor(settings['rhyme_colors'].get(scheme, ACCENT))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(color); p.drawRoundedRect(lx, 171, 28, 28, 6, 6)
        p.setPen(QColor('#ffffff')); p.setFont(QFont(settings.get('font', 'DejaVu Sans'), 11, QFont.Weight.Bold)); p.drawText(QRect(lx,171,28,28), Qt.AlignmentFlag.AlignCenter, str(scheme))
        lx += 42
    for column, group in enumerate(groups):
        origin = column * width
        baseline = header_height
        previous_index = -1
        for index, text, start, end in group:
            if index != previous_index and lines[index].get('paragraph_start') and previous_index >= 0:
                baseline += section_gap
            if start == 0:
                p.setPen(QColor('#62655d')); p.setFont(QFont(settings.get('font', 'DejaVu Sans'), 11))
                p.drawText(origin + 34, baseline, f'{index + 1:02}')
                p.setFont(font)
            fragment = text[start:end]
            for span in lines[index].get('rhyme_spans', []):
                left = max(start, int(span.get('start', 0)))
                right = min(end, int(span.get('end', 0)))
                if right <= left:
                    continue
                x1 = origin + margin + fm.horizontalAdvance(text[start:left])
                x2 = origin + margin + fm.horizontalAdvance(text[start:right])
                scheme_color = QColor(settings['rhyme_colors'].get(span.get('scheme'), ACCENT))
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(scheme_color)
                p.drawRoundedRect(QRectF(x1 - 3, baseline - fm.ascent() - 3,
                                         x2 - x1 + 6, fm.height() + 6), 5, 5)
            p.setPen(QColor(settings.get('highlight_color', ACCENT)))
            p.drawText(origin + margin, baseline, fragment)
            baseline += row_height
            previous_index = index
    p.end()
    return image


def _fft_magnitudes(frame):
    """Return a real-input FFT magnitude spectrum using an in-place radix-2 FFT."""
    size = len(frame)
    real = list(frame)
    imag = [0.0] * size
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            real[i], real[j] = real[j], real[i]
    length = 2
    while length <= size:
        angle = -2 * math.pi / length
        step_real, step_imag = math.cos(angle), math.sin(angle)
        half = length // 2
        for start in range(0, size, length):
            wr, wi = 1.0, 0.0
            for offset in range(half):
                even = start + offset
                odd = even + half
                tr = wr * real[odd] - wi * imag[odd]
                ti = wr * imag[odd] + wi * real[odd]
                real[odd], imag[odd] = real[even] - tr, imag[even] - ti
                real[even], imag[even] = real[even] + tr, imag[even] + ti
                wr, wi = wr * step_real - wi * step_imag, wr * step_imag + wi * step_real
        length <<= 1
    return [math.hypot(real[i], imag[i]) for i in range(size // 2 + 1)]


def _analyze_frequency_bands(samples, sample_rate, frames_per_second=20, band_count=84):
    fft_size = 512
    hop = max(1, round(sample_rate / frames_per_second))
    min_frequency, max_frequency = 55.0, sample_rate / 2
    edges = [min_frequency * (max_frequency / min_frequency) ** (i / band_count) for i in range(band_count + 1)]
    ranges = [(max(1, math.floor(edges[i] * fft_size / sample_rate)),
               min(fft_size // 2, max(1, math.ceil(edges[i + 1] * fft_size / sample_rate))))
              for i in range(band_count)]
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (fft_size - 1)) for i in range(fft_size)]
    raw_frames = []
    for start in range(0, len(samples), hop):
        frame = [0.0] * fft_size
        available = min(fft_size, len(samples) - start)
        for i in range(available):
            frame[i] = samples[start + i] * window[i]
        spectrum = _fft_magnitudes(frame)
        raw_frames.append([math.sqrt(sum(spectrum[lo:hi]) / max(1, hi - lo)) for lo, hi in ranges])
    if not raw_frames:
        return []
    # Normalize each frequency band against its own 95th percentile. This keeps
    # bass from burying the quieter mid and high-frequency activity.
    normalizers = []
    for band in range(band_count):
        magnitudes = sorted(frame[band] for frame in raw_frames)
        normalizers.append(magnitudes[min(len(magnitudes) - 1, int(len(magnitudes) * .95))] or 1.0)
    return [[min(1.0, magnitude / normalizers[band]) ** .7 for band, magnitude in enumerate(frame)]
            for frame in raw_frames]


class PreviewCanvas(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumSize(480, 270)

    def paintEvent(self, event):
        del event
        p = QPainter(self)
        paint_verse_frame(p, self.app, self.width(), self.height(), self.app.position(), self.height())
        p.end()


class VerseQt(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Verse — Lyrics Studio')
        self.resize(1180, 900)
        self.setMinimumSize(900, 650)
        self.setStyleSheet(f'''
            QWidget {{ background: {BG}; color: {TEXT}; font-family: "{FONT}"; }}
            QLabel {{ background: transparent; }}
            QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 5px; }}
            QPushButton {{ background: #303139; color: {TEXT}; border: 0; border-radius: 4px; padding: 7px 11px; }}
            QPushButton:hover {{ background: #3a3b43; }}
            QPushButton[primary="true"] {{ background: {ACCENT}; color: #151710; font-weight: bold; }}
            QLineEdit, QTextEdit, QListWidget, QComboBox, QSpinBox {{ background: #202127; color: {TEXT}; border: 1px solid #34353c; border-radius: 3px; padding: 5px; selection-background-color: #434a35; }}
            QPushButton, QLineEdit, QComboBox, QSpinBox {{ margin-left: 4px; margin-right: 4px; }}
            QScrollArea {{ border: 0; }} QSlider::groove:horizontal {{ background: #36373e; height: 4px; }}
            QSlider::handle:horizontal {{ background: {ACCENT}; width: 12px; margin: -5px 0; border-radius: 6px; }}
        ''')
        self.audio_path = None; self.duration = 0.0; self.project_path = None
        self.visualizer = []
        self.lines = []; self.display_mode = 'line'; self.highlight_mode = 'word'; self.alignment = 'Center'
        self.playing = False; self.offset = 0.0; self.dragging = False
        self.anchor = 0.0; self.anchor_enabled = False
        self._undo_stack = []; self._redo_stack = []; self._history_suspended = False
        self.zoom_factor = 1.0
        self.render_settings = {**DEFAULT_RENDER_SETTINGS, 'rhyme_colors': dict(RHYME_COLORS)}
        self.editor_collapsed = False; self.rhyme_collapsed = False; self.rows = []
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._player_error)
        self._build()
        self._register_zoom_fonts(self)
        self._shortcuts = []
        for sequence, callback in (
            ('Ctrl+=', lambda: self.zoom(1)), ('Ctrl+Plus', lambda: self.zoom(1)),
            ('Ctrl+-', lambda: self.zoom(-1)), ('Ctrl+S', self.save_project),
            ('Alt+Return', self._sync_active), ('Ctrl+Z', self.undo), ('Ctrl+Y', self.redo),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self); shortcut.activated.connect(callback); self._shortcuts.append(shortcut)
        self.bridge = SignalBridge(); self.bridge.finished.connect(self._export_done); self.bridge.failed.connect(self._export_failed)
        self.exporting = False
        self.settings_dialog_open = False
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(80)
        QTimer.singleShot(0, self._restore_last_project)

    def _build(self):
        central = QWidget(); self.setCentralWidget(central)
        outer = QVBoxLayout(central); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        top = QWidget(); top.setFixedHeight(62); th = QHBoxLayout(top); th.setContentsMargins(18, 8, 18, 8)
        self.project_label = QLabel('UNTITLED SESSION'); th.addWidget(self.project_label)
        th.addStretch()
        self.play_button = QPushButton('▶'); self.play_button.setProperty('primary', True); self.play_button.clicked.connect(self.toggle_play); th.addWidget(self.play_button)
        self.time_label = QLabel('0:00 / 0:00'); self.time_label.setMinimumWidth(100); th.addWidget(self.time_label)
        self.seek = QSlider(Qt.Orientation.Horizontal); self.seek.setRange(0, 1000); self.seek.setFixedWidth(140); self.seek.sliderPressed.connect(lambda: setattr(self, 'dragging', True)); self.seek.sliderReleased.connect(self._seek_release); self.seek.valueChanged.connect(self._seek_changed); th.addWidget(self.seek)
        self.anchor_check = QCheckBox('⚓ Anchor'); self.anchor_check.toggled.connect(self._anchor_changed); th.addWidget(self.anchor_check)
        self.anchor_button = QPushButton('Set'); self.anchor_button.clicked.connect(self._set_anchor); th.addWidget(self.anchor_button)
        open_btn = QPushButton('Open project'); open_btn.clicked.connect(self.open_project); th.addWidget(open_btn)
        save_btn = QPushButton('Save project'); save_btn.clicked.connect(self.save_project); th.addWidget(save_btn)
        outer.addWidget(top)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); outer.addWidget(scroll, 1)
        body = QWidget(); body_layout = QVBoxLayout(body); body_layout.setContentsMargins(35, 22, 35, 18); body_layout.setSpacing(12); scroll.setWidget(body)
        head = QHBoxLayout(); title = QLabel('VERSE  /  LYRICS STUDIO'); title.setStyleSheet(f'color:{MUTED}; font-weight:bold; letter-spacing:2px'); head.addWidget(title); head.addStretch()
        settings = QPushButton('Render settings'); settings.clicked.connect(self.open_render_settings); head.addWidget(settings)
        sheet_btn = QPushButton('Export rhymesheet ↗'); sheet_btn.clicked.connect(self.export_rhymesheet); head.addWidget(sheet_btn)
        preview_btn = QPushButton('Preview ↗'); preview_btn.setProperty('primary', True); preview_btn.clicked.connect(self.open_preview); head.addWidget(preview_btn)
        body_layout.addLayout(head)

        track = self._card(body_layout)
        self._section_title(track, '01', 'Your track', 'Add the audio and lyrics you want to bring to life.')
        row = QHBoxLayout(); row.setContentsMargins(18, 8, 18, 10); row.setSpacing(10)
        audio_btn = QPushButton('♫   Add audio track'); audio_btn.clicked.connect(self.load_audio); row.addWidget(audio_btn)
        lyrics_btn = QPushButton('≡   Import TXT / LRC lyrics'); lyrics_btn.clicked.connect(self.load_lyrics); row.addWidget(lyrics_btn)
        self.track_info = QLabel('No audio added'); row.addWidget(self.track_info, 1); track.layout().addLayout(row)

        self.editor = self._card(body_layout); self.editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_layout.setStretch(body_layout.indexOf(self.editor), 2)
        ehead = QHBoxLayout(); ehead.setContentsMargins(14, 10, 14, 8); ehead.setSpacing(8); self.editor.layout().addLayout(ehead)
        self.editor_arrow = self._arrow(); self.editor_arrow.clicked.connect(self.toggle_editor); ehead.addWidget(self.editor_arrow)
        self._add_title_layout(ehead, '02', 'Sync your lyrics', 'Alt+Enter syncs the next untimed line')
        ehead.addStretch()
        ehead.addWidget(QLabel('Display'))
        self.display_combo = QComboBox(); self.display_combo.addItems(['Line', 'Paragraph', 'Scroll']); self.display_combo.currentTextChanged.connect(lambda value: self.set_display_mode(value.lower())); ehead.addWidget(self.display_combo)
        ehead.addWidget(QLabel('Highlight'))
        self.highlight_combo = QComboBox(); self.highlight_combo.addItems(['Word', 'Character', 'Smooth']); self.highlight_combo.currentTextChanged.connect(lambda value: self.set_highlight_mode(value.lower())); ehead.addWidget(self.highlight_combo)
        ehead.addWidget(QLabel('Alignment'))
        self.align_combo = QComboBox(); self.align_combo.addItems(['Left', 'Center', 'Right']); self.align_combo.setCurrentText(self.alignment); self.align_combo.currentTextChanged.connect(self.set_alignment); ehead.addWidget(self.align_combo)
        add = QPushButton('+ Add line'); add.clicked.connect(self.add_line); ehead.addWidget(add)
        self.lyrics_content = QWidget(); self.lyrics_layout = QVBoxLayout(self.lyrics_content); self.lyrics_layout.setContentsMargins(0, 0, 0, 0); self.lyrics_layout.setSpacing(0); self.editor.layout().addWidget(self.lyrics_content, 1)
        self.rows_scroll = QScrollArea(); self.rows_scroll.setWidgetResizable(True); self.lyrics_layout.addWidget(self.rows_scroll, 1)
        self.rows_host = QWidget(); self.rows_box = QVBoxLayout(self.rows_host); self.rows_box.setContentsMargins(0, 0, 0, 0); self.rows_box.setSpacing(2); self.rows_box.addStretch(); self.rows_scroll.setWidget(self.rows_host)
        self.editor_foot = QLabel('✓  Changes save with your project     ·     Space plays / pauses'); self.editor_foot.setStyleSheet(f'color:{MUTED}; padding:8px 16px; border-top:1px solid {BORDER}')
        self.editor.layout().addWidget(self.editor_foot)

        self.rhyme = self._card(body_layout); self.rhyme.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_layout.setStretch(body_layout.indexOf(self.rhyme), 1)
        rh = QHBoxLayout(); rh.setContentsMargins(14, 10, 14, 8); self.rhyme.layout().addLayout(rh)
        self.rhyme_arrow = self._arrow(); self.rhyme_arrow.clicked.connect(self.toggle_rhyme); rh.addWidget(self.rhyme_arrow)
        self._add_title_layout(rh, '03', 'Rhyme scheme', 'Select text in a line, then assign a matching scheme letter')
        rh.addStretch()
        self.rhyme_content = QWidget(); rv = QVBoxLayout(self.rhyme_content); rv.setContentsMargins(18, 0, 18, 12); self.rhyme.layout().addWidget(self.rhyme_content)
        tools = QHBoxLayout(); rv.addLayout(tools)
        tools.addWidget(QLabel('Line')); self.rhyme_line = QComboBox(); self.rhyme_line.currentIndexChanged.connect(self._load_rhyme_line); tools.addWidget(self.rhyme_line)
        self.rhyme_edit_indices = None
        self.scheme_buttons = {}
        for scheme, color in self.render_settings['rhyme_colors'].items():
            button = QPushButton(scheme); button.setFixedWidth(38); button.setStyleSheet(f'background:{color};color:#111;font-weight:bold'); button.clicked.connect(lambda _=False, s=scheme: self.assign_rhyme(s)); tools.addWidget(button); self.scheme_buttons[scheme] = button
        clear = QPushButton('Clear'); clear.clicked.connect(self.clear_rhyme); tools.addWidget(clear)
        multi = QPushButton('Edit multiple lines'); multi.clicked.connect(self.edit_multiple_lines); tools.addWidget(multi)
        tools.addStretch()
        self.rhyme_text = QTextEdit(); self.rhyme_text.setUndoRedoEnabled(False); self.rhyme_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding); self.rhyme_text.textChanged.connect(self._rhyme_changed); rv.addWidget(self.rhyme_text, 1)
        self.rhyme_context = QLabel('Previous: —     ·     Next: —'); self.rhyme_context.setStyleSheet(f'color:{MUTED}'); rv.addWidget(self.rhyme_context)
        self.rhyme_status = QLabel(''); self.rhyme_status.setStyleSheet(f'color:{MUTED}'); rv.addWidget(self.rhyme_status)
        footer = QLabel('Made for the feeling between the lines.'); footer.setStyleSheet(f'color:{MUTED}; padding:4px'); body_layout.addWidget(footer)
        body_layout.addStretch(0)
        self.set_lines([])
        self.set_display_mode('line'); self.set_highlight_mode('word')

    def _card(self, parent):
        frame = QFrame(); frame.setObjectName('card'); frame.setLayout(QVBoxLayout()); frame.layout().setContentsMargins(0, 0, 0, 0); frame.layout().setSpacing(0); parent.addWidget(frame); return frame

    def _arrow(self):
        b = QPushButton('▾'); b.setFixedSize(34, 34); b.setFont(QFont(FONT, 20, QFont.Weight.Bold)); b.setStyleSheet(f'QPushButton{{border:0;background:transparent;color:{MUTED};padding:0}} QPushButton:hover{{color:{TEXT}}}'); return b

    def _section_title(self, parent, n, title, sub):
        row = QHBoxLayout(); row.setContentsMargins(18, 15, 18, 3); parent.layout().addLayout(row); self._add_title_layout(row, n, title, sub)

    def _add_title_layout(self, row, n, title, sub):
        texts = QVBoxLayout(); name = QLabel(title); name.setFont(QFont(FONT, 12, QFont.Weight.Bold)); desc = QLabel(sub); desc.setFont(QFont(FONT, 9)); desc.setStyleSheet(f'color:{MUTED}'); texts.addWidget(name); texts.addWidget(desc); row.addLayout(texts)

    def toggle_editor(self):
        self.editor_collapsed = not self.editor_collapsed
        self.lyrics_content.setVisible(not self.editor_collapsed); self.editor_foot.setVisible(not self.editor_collapsed)
        self.editor_arrow.setText('▸' if self.editor_collapsed else '▾')
        self.editor.setSizePolicy(QSizePolicy.Policy.Preferred if self.editor_collapsed else QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed if self.editor_collapsed else QSizePolicy.Policy.Expanding)

    def toggle_rhyme(self):
        self.rhyme_collapsed = not self.rhyme_collapsed
        self.rhyme_content.setVisible(not self.rhyme_collapsed)
        self.rhyme.setSizePolicy(QSizePolicy.Policy.Preferred if self.rhyme_collapsed else QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed if self.rhyme_collapsed else QSizePolicy.Policy.Expanding)
        self.rhyme_arrow.setText('▸' if self.rhyme_collapsed else '▾')

    def load_audio(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose an audio track', '', 'Audio (*.mp3 *.wav *.m4a *.ogg *.flac *.aac);;All files (*)')
        if not path: return
        p = Path(path)
        try:
            duration = probe_duration(p)
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(p.resolve())))
        except Exception as exc: QMessageBox.critical(self, 'Could not load audio', f'Check that FFmpeg can read this audio file.\n\n{exc}'); return
        self.visualizer = self._analyze_audio(p)
        self.audio_path = p; self.duration = duration; self.offset = 0; self.playing = False; self.anchor = 0; self.anchor_check.setChecked(False)
        self.track_info.setText(f'♫  {p.name}   ·   {stamp(duration)}'); self.project_label.setText(p.stem.upper()); self.update_preview()

    def _analyze_audio(self, path, samples_per_second=20):
        try:
            result = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'],
                capture_output=True, check=True,
            )
            samples = array('h'); samples.frombytes(result.stdout)
            if sys.byteorder != 'little': samples.byteswap()
            if not samples: return []
            return _analyze_frequency_bands(samples, 8000, samples_per_second)
        except (OSError, subprocess.CalledProcessError):
            return []

    def load_lyrics(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose lyrics', '', 'Lyrics (*.txt *.lrc);;All files (*)')
        if not path: return
        try: self.set_lines(parse_lyrics(Path(path).read_text(encoding='utf-8-sig')))
        except OSError as exc: QMessageBox.critical(self, 'Could not read lyrics', str(exc))

    def set_lines(self, lines):
        self.lines = [{'text': str(x.get('text', '')), 'start': x.get('start'), 'paragraph_start': bool(x.get('paragraph_start', i == 0)), 'paragraph_label': x.get('paragraph_label'), 'rhyme_spans': [dict(s) for s in x.get('rhyme_spans', [])], 'sync_segments': [dict(s) for s in x.get('sync_segments', [])]} for i, x in enumerate(lines)]
        self.render_rows(); self.refresh_rhyme(); self.update_preview()

    def render_rows(self):
        while self.rows_box.count() > 1:
            item = self.rows_box.takeAt(0); widget = item.widget()
            if widget: widget.deleteLater()
        self.rows = []
        if not self.lines:
            label = QLabel('♫\n\nYour lyrics will live here\nImport a text file or add your first line.'); label.setAlignment(Qt.AlignmentFlag.AlignCenter); label.setStyleSheet(f'color:{MUTED};padding:28px'); self.rows_box.insertWidget(0, label); return
        for i, line in enumerate(self.lines):
            if line.get('paragraph_label'):
                section = QWidget(); section_layout = QHBoxLayout(section); section_layout.setContentsMargins(18, 10, 18, 2); section_layout.setSpacing(9)
                left_rule = QFrame(); left_rule.setFrameShape(QFrame.Shape.HLine); left_rule.setStyleSheet(f'color:{BORDER}'); section_layout.addWidget(left_rule, 1)
                tag = QLabel(line['paragraph_label'].upper()); tag.setStyleSheet(f'color:{PURPLE};font-weight:bold; letter-spacing:1px'); section_layout.addWidget(tag)
                right_rule = QFrame(); right_rule.setFrameShape(QFrame.Shape.HLine); right_rule.setStyleSheet(f'color:{BORDER}'); section_layout.addWidget(right_rule, 1)
                self.rows_box.insertWidget(self.rows_box.count()-1, section)
            row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(10, 4, 10, 4); layout.setSpacing(5)
            segments = sorted(line.get('sync_segments', []), key=lambda segment: segment.get('offset', 0))
            num = QLabel(f'{i+1}'); num.setFixedWidth(42); num.setStyleSheet(f'color:{MUTED}'); layout.addWidget(num)
            para = QPushButton('¶' if line.get('paragraph_start') else '·'); para.setFixedWidth(30); para.clicked.connect(lambda _=False, n=i: self.toggle_paragraph(n)); layout.addWidget(para)
            edit = QLineEdit(line['text']); edit.installEventFilter(self); layout.addWidget(edit, 1); edit.textChanged.connect(lambda text, n=i: self._line_changed(n, text))
            sync = QPushButton(); sync.setFixedWidth(94); sync.clicked.connect(lambda _=False, n=i: self.sync_line(n)); layout.addWidget(sync)
            split = QPushButton('Split'); split.clicked.connect(lambda _=False, n=i, e=edit: self.split_line(n, e)); layout.addWidget(split)
            rem = QPushButton('X'); rem.setFixedSize(36, 32); rem.setFont(QFont('Arial', 14, QFont.Weight.Bold)); rem.setStyleSheet('QPushButton { color: white; background-color: #b42318; border: 1px solid #b42318; border-radius: 5px; padding: 0px; } QPushButton:hover { color: white; background-color: #8f1d14; }'); rem.clicked.connect(lambda _=False, n=i: self.remove_line(n)); layout.addWidget(rem)
            self.rows_box.insertWidget(self.rows_box.count()-1, row); self.rows.append({'widget':row,'edit':edit,'sync':sync})
            self._update_sync_button(i)
            self._register_zoom_fonts(row)
            if len(segments) > 1:
                offsets = [max(0, min(len(line['text']), segment.get('offset', 0))) for segment in segments]
                offsets = sorted(set(offsets))
                if not offsets or offsets[0] != 0: offsets.insert(0, 0)
                offsets.append(len(line['text']))
                for segment_index, (start, end) in enumerate(zip(offsets, offsets[1:]), start=1):
                    detail = QWidget(); detail_layout = QHBoxLayout(detail); detail_layout.setContentsMargins(43, 0, 10, 2); detail_layout.setSpacing(5)
                    part_number = QLabel(f'{i+1}.{segment_index}'); part_number.setFixedWidth(42); part_number.setStyleSheet(f'color:{MUTED}'); detail_layout.addWidget(part_number)
                    part_text = QLabel(line['text'][start:end].strip()); part_text.setWordWrap(True); part_text.setStyleSheet(f'color:{MUTED}; padding:3px 6px'); detail_layout.addWidget(part_text, 1)
                    segment = next((item for item in segments if item.get('offset') == start), None)
                    status = segment.get('start') if segment else None
                    detail_sync = QPushButton(stamp(status) if status is not None else f'Sync {segment_index}/{len(segments)}')
                    detail_sync.setFixedWidth(94); detail_sync.clicked.connect(lambda _=False, n=i, o=start: self.sync_segment(n, o)); detail_layout.addWidget(detail_sync)
                    unsplit = QPushButton('X'); unsplit.setFixedSize(36, 32); unsplit.setFont(QFont('Arial', 14, QFont.Weight.Bold)); unsplit.setStyleSheet('QPushButton { color: white; background-color: #b42318; border: 1px solid #b42318; border-radius: 5px; padding: 0px; } QPushButton:hover { color: white; background-color: #8f1d14; }'); unsplit.clicked.connect(lambda _=False, n=i, o=start: self.unsplit_segment(n, o)); detail_layout.addWidget(unsplit)
                    self.rows_box.insertWidget(self.rows_box.count()-1, detail)
                    self._register_zoom_fonts(detail)

    def _line_changed(self, index, text):
        if 0 <= index < len(self.lines) and self.lines[index]['text'] != text:
            self._push_history()
            line = self.lines[index]
            matcher = SequenceMatcher(None, line['text'], text, autojunk=False)
            mapped = []
            for segment in line.get('sync_segments', []):
                old_offset = segment.get('offset', 0)
                new_offset = len(text)
                for tag, a, b, c, d in matcher.get_opcodes():
                    if a <= old_offset <= b:
                        new_offset = c + min(old_offset - a, d - c)
                        break
                mapped.append({**segment, 'offset': max(0, min(len(text), new_offset))})
            if mapped:
                line['sync_segments'] = sorted(mapped, key=lambda segment: segment['offset'])
                unique = {}
                for segment in line['sync_segments']: unique.setdefault(segment['offset'], segment)
                line['sync_segments'] = list(unique.values())
            line['rhyme_spans'] = self._remap_rhyme_spans(line['text'], text, line.get('rhyme_spans', []))
            line['text'] = text
            if self.rhyme_line.currentIndex() == index and not self.rhyme_edit_indices:
                self.rhyme_text.blockSignals(True); self.rhyme_text.setPlainText(text); self.rhyme_text.blockSignals(False); self._apply_rhyme_highlights()
            self.update_preview()

    @staticmethod
    def _remap_rhyme_spans(old_text, new_text, spans):
        if not spans: return []
        matcher = SequenceMatcher(None, old_text, new_text, autojunk=False)
        opcodes = matcher.get_opcodes()

        def map_boundary(position, bias):
            shift = 0
            for tag, old_start, old_end, new_start, new_end in opcodes:
                if tag == 'equal':
                    if old_start <= position <= old_end: return new_start + position - old_start
                    continue
                delta = (new_end - new_start) - (old_end - old_start)
                if position < old_start: return position + shift
                if position > old_end:
                    shift += delta
                    continue
                if tag == 'equal':
                    if position == old_start: return new_start
                    if position == old_end and bias == 'right': continue
                    return new_start + position - old_start
                if old_start == old_end:  # inserted text
                    return new_end if bias == 'right' else new_start
                if position == old_start: return new_start
                if position == old_end: return new_end
                return new_start if bias == 'left' else new_end
            return position + shift

        remapped = []
        for span in spans:
            start = map_boundary(span.get('start', 0), 'right')
            end = map_boundary(span.get('end', 0), 'left')
            start = max(0, min(len(new_text), start)); end = max(start, min(len(new_text), end))
            if end > start:
                remapped.append({**span, 'start': start, 'end': end})
        return remapped

    def add_line(self):
        self._push_history(); self.lines.append({'text':'','start':None,'paragraph_start':not self.lines,'paragraph_label':None,'rhyme_spans':[]}); self.render_rows(); self.rows[-1]['edit'].setFocus()

    def remove_line(self, index):
        if not 0 <= index < len(self.lines): return
        self._push_history()
        if 0 <= index < len(self.lines): del self.lines[index]
        if self.lines: self.lines[0]['paragraph_start'] = True
        self.render_rows(); self.refresh_rhyme(); self.update_preview()

    def toggle_paragraph(self, index):
        if index:
            self._push_history(); self.lines[index]['paragraph_start'] = not self.lines[index].get('paragraph_start', False); self.render_rows()

    def split_line(self, index, edit):
        if not 0 <= index < len(self.lines): return
        text = edit.text(); pos = edit.cursorPosition()
        if not 0 < pos < len(text): return
        line = self.lines[index]
        segments = line.get('sync_segments', [])
        if any(segment.get('offset') == pos for segment in segments): return
        self._push_history()
        if not segments:
            segments = [{'offset': 0, 'start': line.get('start')}]
            line['sync_segments'] = segments
        segments.append({'offset': pos, 'start': None})
        segments.sort(key=lambda segment: segment.get('offset', 0))
        self.render_rows()
        self.rows[index]['edit'].setCursorPosition(pos)
        self.rows[index]['edit'].setFocus()

    def _update_sync_button(self, index):
        line = self.lines[index]
        segments = line.get('sync_segments', [])
        if not segments:
            self.rows[index]['sync'].setText(stamp(line['start']) if line.get('start') is not None else '+ sync')
            return
        first = next((segment for segment in segments if segment.get('offset') == 0), None)
        label = stamp(first['start']) if first and first.get('start') is not None else f'Sync 1/{len(segments)}'
        self.rows[index]['sync'].setText(label)

    def refresh_rhyme(self):
        if not hasattr(self, 'rhyme_line'): return
        previous = self.rhyme_line.currentIndex(); self.rhyme_line.blockSignals(True); self.rhyme_line.clear(); self.rhyme_line.addItems([f'{i+1:02}' for i in range(len(self.lines))]); self.rhyme_line.setCurrentIndex(max(0, min(previous, len(self.lines)-1))); self.rhyme_line.blockSignals(False); self._load_rhyme_line()

    def _load_rhyme_line(self, *_):
        if self.rhyme_edit_indices:
            self.rhyme_edit_indices = None
        for button in self.scheme_buttons.values(): button.setEnabled(True)
        self.rhyme_status.clear()
        idx = self.rhyme_line.currentIndex()
        if idx < 0 or idx >= len(self.lines): self.rhyme_text.blockSignals(True); self.rhyme_text.clear(); self.rhyme_text.blockSignals(False); self.rhyme_text.setExtraSelections([]); self.rhyme_context.setText('Previous: —   ·   Next: —'); return
        self.rhyme_text.blockSignals(True); self.rhyme_text.setPlainText(self.lines[idx]['text']); self.rhyme_text.blockSignals(False)
        prev = self.lines[idx-1]['text'] if idx else '—'; nxt = self.lines[idx+1]['text'] if idx+1 < len(self.lines) else '—'; self.rhyme_context.setText(f'Previous: {prev}   ·   Next: {nxt}')
        self._apply_rhyme_highlights()

    def edit_multiple_lines(self):
        if len(self.lines) < 2: return
        dialog = QDialog(self); dialog.setWindowTitle('Select lyric lines'); dialog.resize(480, 420)
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel('Select the lines to edit together. Keep one editor row per selected line.'))
        choices = QListWidget(); choices.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for index, line in enumerate(self.lines): choices.addItem(f'{index + 1:02}  {line["text"]}')
        current = self.rhyme_line.currentIndex()
        if current >= 0: choices.item(current).setSelected(True)
        layout.addWidget(choices, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        indices = sorted(row.row() for row in choices.selectedIndexes())
        if len(indices) < 2:
            QMessageBox.information(self, 'Select lines', 'Choose at least two lyric lines to edit together.', parent=dialog)
            return
        self.rhyme_edit_indices = indices
        self.rhyme_line.blockSignals(True); self.rhyme_line.setCurrentIndex(indices[0]); self.rhyme_line.blockSignals(False)
        self.rhyme_text.blockSignals(True); self.rhyme_text.setPlainText('\n'.join(self.lines[i]['text'] for i in indices)); self.rhyme_text.blockSignals(False)
        for button in self.scheme_buttons.values(): button.setEnabled(True)
        self.rhyme_context.setText(f'Editing lines {", ".join(str(i + 1) for i in indices)}')
        self.rhyme_status.setText('Keep one row per selected lyric line to apply edits.')
        self._apply_rhyme_highlights()

    def _apply_rhyme_highlights(self):
        selections = []
        if self.rhyme_edit_indices:
            offset = 0
            for index in self.rhyme_edit_indices:
                line = self.lines[index]
                for span in line.get('rhyme_spans', []):
                    selections.append((offset + span['start'], offset + span['end'], span.get('scheme')))
                offset += len(line['text']) + 1
        else:
            index = self.rhyme_line.currentIndex()
            if 0 <= index < len(self.lines):
                selections = [(span['start'], span['end'], span.get('scheme')) for span in self.lines[index].get('rhyme_spans', [])]
        formatted = []
        editor_text = self.rhyme_text.toPlainText()

        def qt_position(character_index):
            # QTextCursor positions count UTF-16 code units; lyric span offsets
            # are Python character offsets. Convert at the editor boundary so
            # selections remain aligned after emoji or other non-BMP text.
            return len(editor_text[:character_index].encode('utf-16-le')) // 2

        for start, end, scheme in selections:
            color = self.render_settings.get('rhyme_colors', {}).get(scheme)
            if not color or end <= start: continue
            cursor = self.rhyme_text.textCursor()
            cursor.setPosition(qt_position(max(0, min(len(editor_text), start))))
            cursor.setPosition(qt_position(max(0, min(len(editor_text), end))), QTextCursor.MoveMode.KeepAnchor)
            extra = QTextEdit.ExtraSelection(); extra.cursor = cursor
            extra.format.setBackground(QColor(color)); extra.format.setForeground(QColor('#111216'))
            formatted.append(extra)
        self.rhyme_text.setExtraSelections(formatted)

    def _rhyme_changed(self):
        idx = self.rhyme_line.currentIndex()
        value = self.rhyme_text.toPlainText()
        indices = self.rhyme_edit_indices or ([idx] if 0 <= idx < len(self.lines) else [])
        if self.rhyme_edit_indices:
            values = value.split('\n')
            if len(values) != len(indices):
                self.rhyme_status.setText(f'Keep exactly {len(indices)} editor rows for the selected lines.')
                return
            self.rhyme_status.setText('')
        else:
            values = [value]
        changed = [(line_index, text) for line_index, text in zip(indices, values) if self.lines[line_index]['text'] != text]
        if changed:
            self._push_history()
            for line_index, text in changed:
                line = self.lines[line_index]
                line['rhyme_spans'] = self._remap_rhyme_spans(line['text'], text, line.get('rhyme_spans', []))
                line['text'] = text
                if line_index < len(self.rows):
                    self.rows[line_index]['edit'].blockSignals(True); self.rows[line_index]['edit'].setText(text); self.rows[line_index]['edit'].blockSignals(False)
            self._apply_rhyme_highlights(); self.update_preview()

    def assign_rhyme(self, scheme):
        if self.rhyme_edit_indices:
            editor_text = self.rhyme_text.toPlainText()
            cursor = self.rhyme_text.textCursor()

            def python_position(qt_index):
                units = 0
                for index, char in enumerate(editor_text):
                    if units >= qt_index: return index
                    units += len(char.encode('utf-16-le')) // 2
                return len(editor_text)

            start, end = python_position(cursor.selectionStart()), python_position(cursor.selectionEnd())
            if end <= start:
                match = re.search(r'\S+$', editor_text[:python_position(cursor.position())])
                if not match: return
                start, end = match.start(), match.end()

            self._push_history()
            line_start = 0
            for line_index in self.rhyme_edit_indices:
                text = self.lines[line_index]['text']
                line_end = line_start + len(text)
                span_start, span_end = max(start, line_start), min(end, line_end)
                if span_end > span_start:
                    local_start, local_end = span_start - line_start, span_end - line_start
                    spans = [span for span in self.lines[line_index].get('rhyme_spans', [])
                             if span.get('end', 0) <= local_start or span.get('start', 0) >= local_end]
                    spans.append({'start': local_start, 'end': local_end, 'scheme': scheme})
                    self.lines[line_index]['rhyme_spans'] = spans
                line_start = line_end + 1
            self._apply_rhyme_highlights(); self.update_preview()
            return
        idx = self.rhyme_line.currentIndex()
        if not 0 <= idx < len(self.lines): return
        cursor = self.rhyme_text.textCursor(); start, end = cursor.selectionStart(), cursor.selectionEnd()
        if end <= start:
            text = self.lines[idx]['text']; match = re.search(r'\S+$', text[:cursor.position()])
            if not match: return
            start, end = match.start(), match.end()
        self._push_history()
        spans = [s for s in self.lines[idx].get('rhyme_spans', []) if s.get('end',0) <= start or s.get('start',0) >= end]
        spans.append({'start':start,'end':end,'scheme':scheme}); self.lines[idx]['rhyme_spans'] = spans
        self._apply_rhyme_highlights(); self.update_preview()

    def clear_rhyme(self):
        if self.rhyme_edit_indices:
            if any(self.lines[index].get('rhyme_spans') for index in self.rhyme_edit_indices):
                self._push_history()
                for index in self.rhyme_edit_indices: self.lines[index]['rhyme_spans'] = []
                self._apply_rhyme_highlights(); self.update_preview()
            return
        idx = self.rhyme_line.currentIndex()
        if 0 <= idx < len(self.lines) and self.lines[idx].get('rhyme_spans'):
            self._push_history(); self.lines[idx]['rhyme_spans'] = []
            self._apply_rhyme_highlights(); self.update_preview()

    def set_display_mode(self, mode):
        mode = str(mode).lower()
        if mode != self.display_mode: self._push_history()
        self.display_mode = mode
        if self.display_combo.currentText().lower() != mode: self.display_combo.setCurrentText(mode.title())
        if hasattr(self, 'timer'): self.timer.setInterval(16 if mode == 'scroll' else 80)
        self.update_preview()
    def set_highlight_mode(self, mode):
        if mode != self.highlight_mode: self._push_history()
        self.highlight_mode = mode
        if self.highlight_combo.currentText().lower() != mode: self.highlight_combo.setCurrentText(mode.title())
        self.update_preview()
    def set_alignment(self, value):
        if value != self.alignment: self._push_history()
        self.alignment = value
        self.update_preview()
    def _capture_history(self):
        return {
            'lines': copy.deepcopy(self.lines), 'display_mode': self.display_mode,
            'highlight_mode': self.highlight_mode, 'alignment': self.alignment,
            'anchor': self.anchor, 'anchor_enabled': self.anchor_enabled,
            'render_settings': copy.deepcopy(self.render_settings),
        }
    def _push_history(self):
        if self._history_suspended: return
        self._undo_stack.append(self._capture_history())
        if len(self._undo_stack) > 200: self._undo_stack.pop(0)
        self._redo_stack.clear()
    def _restore_history(self, state):
        self._history_suspended = True
        try:
            self.lines = copy.deepcopy(state['lines'])
            self.display_mode = state['display_mode']; self.highlight_mode = state['highlight_mode']
            self.alignment = state['alignment']; self.anchor = state['anchor']
            self.anchor_enabled = state['anchor_enabled']; self.render_settings = copy.deepcopy(state['render_settings'])
            for scheme, color in self.render_settings['rhyme_colors'].items(): self.scheme_buttons[scheme].setStyleSheet(f'background:{color};color:#111;font-weight:bold')
            self.align_combo.setCurrentText(self.alignment)
            self.display_combo.setCurrentText(self.display_mode.title())
            self.highlight_combo.setCurrentText(self.highlight_mode.title())
            self.anchor_check.setChecked(self.anchor_enabled)
            self.render_rows(); self.refresh_rhyme(); self._apply_rhyme_highlights(); self.update_preview()
        finally:
            self._history_suspended = False
    def undo(self):
        if not self._undo_stack: return
        self._redo_stack.append(self._capture_history())
        self._restore_history(self._undo_stack.pop())
    def redo(self):
        if not self._redo_stack: return
        self._undo_stack.append(self._capture_history())
        self._restore_history(self._redo_stack.pop())
    def active_index(self):
        pos=self.position(); active=[i for i,x in enumerate(self.lines) if x.get('start') is not None and x['start'] <= pos]; return active[-1] if active else (0 if self.lines else -1)
    def position(self):
        return min(self.duration, self.player.position() / 1000) if self.playing else self.offset
    def sync_line(self, index):
        if 0 <= index < len(self.lines):
            self._push_history(); line = self.lines[index]
            segments = line.get('sync_segments', [])
            first_segment = next((segment for segment in segments if segment.get('offset') == 0), None)
            if first_segment is not None:
                first_segment['start'] = self.position()
                line['start'] = first_segment['start']
            else:
                line['start'] = self.position()
            self._update_sync_button(index); self.update_preview()

    def sync_segment(self, index, segment_offset):
        if not 0 <= index < len(self.lines): return
        segments = self.lines[index].get('sync_segments', [])
        segment = next((item for item in segments if item.get('offset') == segment_offset), None)
        if segment is None: return
        self._push_history()
        segment['start'] = self.position()
        if segment.get('offset') == 0:
            self.lines[index]['start'] = segment['start']
        self.render_rows(); self.update_preview()

    def unsplit_segment(self, index, segment_offset):
        if not 0 <= index < len(self.lines): return
        segments = self.lines[index].get('sync_segments', [])
        if segment_offset <= 0 or not any(item.get('offset') == segment_offset for item in segments): return
        self._push_history()
        self.lines[index]['sync_segments'] = [item for item in segments if item.get('offset') != segment_offset]
        if len(self.lines[index]['sync_segments']) <= 1:
            self.lines[index]['sync_segments'] = []
        self.render_rows(); self.update_preview()
    def _sync_active(self):
        if not self.lines: self.add_line()
        index=next((i for i,x in enumerate(self.lines) if x.get('start') is None),self.active_index())
        if index >= 0:self.sync_line(index)
    def toggle_play(self):
        if not self.audio_path: QMessageBox.information(self,'Add a track','Load an audio file before starting playback.'); return
        if self.playing:
            self.offset = self.position()
            self.player.pause()
        else:
            if self.anchor_enabled: self.offset = self.anchor
            elif self.position() >= self.duration: self.offset = 0
            self.player.setPosition(round(self.offset * 1000))
            self.player.play()
    def _playback_state_changed(self, state):
        self.playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText('Ⅱ' if self.playing else '▶')
    def _player_error(self, _error, _error_string=None):
        if self.player.errorString(): QMessageBox.critical(self, 'Playback error', self.player.errorString())
    def _anchor_changed(self, value): self.anchor_enabled=value
    def _set_anchor(self): self.anchor=self.position(); self.anchor_enabled=True; self.anchor_check.setChecked(True); self.anchor_button.setText(f'Set {stamp(self.anchor)[:-3]}')
    def _seek_changed(self, value):
        if self.dragging and self.duration: self.offset=self.duration*value/1000; self.update_preview()
    def _seek_release(self):
        self.dragging=False
        if self.duration:
            self.offset=self.duration*self.seek.value()/1000
            self.player.setPosition(round(self.offset*1000))
    def tick(self):
        if self.playing and self.duration and self.position() >= self.duration: self.offset=self.duration; self.playing=False; self.play_button.setText('▶')
        if self.duration:
            self.time_label.setText(f'{stamp(self.position())[:-3]} / {stamp(self.duration)[:-3]}')
            if not self.dragging: self.seek.setValue(round(1000*self.position()/self.duration))
        if not self.exporting and not self.settings_dialog_open:
            idx=self.active_index()
            for i,row in enumerate(self.rows): row['widget'].setStyleSheet(f'background:{"#22251d" if i==idx else PANEL}')
            self.update_preview()
    def update_preview(self):
        if getattr(self,'preview_canvas',None): self.preview_canvas.update()
    def open_preview(self):
        if getattr(self, 'preview_dialog', None):
            self.preview_dialog.show()
            self.preview_dialog.raise_()
            self.preview_dialog.activateWindow()
            return
        dlg=QDialog(self); dlg.setWindowTitle('Verse · Preview & export'); dlg.resize(800,500); layout=QVBoxLayout(dlg); top=QHBoxLayout(); top.addWidget(QLabel('LIVE PREVIEW')); top.addStretch(); exp=QPushButton('Export video ↗'); exp.setProperty('primary',True); exp.clicked.connect(self.export_video); top.addWidget(exp); layout.addLayout(top); self.preview_canvas=PreviewCanvas(self); layout.addWidget(self.preview_canvas,1); self.preview_dialog=dlg; dlg.show()
    def open_render_settings(self):
        self.settings_dialog_open = True
        if hasattr(self, 'timer'): self.timer.setInterval(100)
        d=QDialog(self); d.setWindowTitle('Render settings'); form=QFormLayout(d)
        font=QComboBox(); font.setEditable(True); font.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        families=sorted(set(QFontDatabase.families()) | {self.render_settings['font']}, key=str.casefold)
        font.addItems(families); font.setCurrentText(self.render_settings['font'])
        font.setPlaceholderText('Search fonts…'); font.setMaxVisibleItems(12)
        completer=QCompleter(families,font); completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains); completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        font.setCompleter(completer)
        size=QSpinBox(); size.setRange(8,300); size.setValue(int(self.render_settings['size']))
        resolution=QComboBox(); resolution.addItems(['1920x1080','1280x720','1080x1080','1080x1920']); resolution.setCurrentText(self.render_settings['resolution'])
        colors = {
            'font': self.render_settings['font_color'],
            'highlight': self.render_settings.get('highlight_color', ACCENT),
            'visualizer': self.render_settings.get('visualizer_color', ACCENT),
            'background': self.render_settings['background_color'],
        }
        rhyme_colors = dict(self.render_settings['rhyme_colors']); color_buttons = {}; color_dialogs = []
        def style_color(button, color): button.setStyleSheet(f'background:{color};color:#111;padding:7px 12px')
        def color_row(key, title, initial):
            row=QWidget(); layout=QHBoxLayout(row); layout.setContentsMargins(0,0,0,0); button=QPushButton(initial); style_color(button,initial)
            def choose():
                picker=QColorDialog(QColor(colors[key] if key in colors else rhyme_colors[key]), d)
                picker.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
                picker.setWindowTitle(f'Choose {title.lower()}')
                color_dialogs.append(picker)
                def apply_color():
                    value=picker.currentColor().name()
                    if key in colors: colors[key]=value
                    else: rhyme_colors[key]=value
                    button.setText(value); style_color(button,value)
                picker.accepted.connect(apply_color)
                picker.finished.connect(lambda _result, item=picker: color_dialogs.remove(item) if item in color_dialogs else None)
                picker.open()
            button.clicked.connect(choose); layout.addWidget(button); layout.addStretch(); color_buttons[key]=button; return row
        form.addRow('Font',font); form.addRow('Font size',size); form.addRow('Resolution',resolution)
        form.addRow('Text color',color_row('font','Text color',colors['font']))
        form.addRow('Highlight color',color_row('highlight','Highlight color',colors['highlight']))
        form.addRow('Visualizer color',color_row('visualizer','Visualizer color',colors['visualizer']))
        form.addRow('Background color',color_row('background','Background color',colors['background']))
        for scheme, color in rhyme_colors.items(): form.addRow(f'Rhyme {scheme}', color_row(scheme, f'Rhyme {scheme}', color))
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(d.accept); buttons.rejected.connect(d.reject); form.addRow(buttons)
        if d.exec():
            self.render_settings.update(font=font.currentText(),size=size.value(),resolution=resolution.currentText(),font_color=colors['font'],highlight_color=colors['highlight'],visualizer_color=colors['visualizer'],background_color=colors['background'],rhyme_colors=rhyme_colors)
            for scheme, color in rhyme_colors.items(): self.scheme_buttons[scheme].setStyleSheet(f'background:{color};color:#111;font-weight:bold')
            self._apply_rhyme_highlights(); self.update_preview()
        self.settings_dialog_open = False
        if hasattr(self, 'timer'): self.timer.setInterval(16 if self.display_mode == 'scroll' else 80)
    def _project_data(self): return {'version':1,'audio_path':str(self.audio_path) if self.audio_path else None,'display_mode':self.display_mode,'highlight_mode':self.highlight_mode,'alignment':self.alignment,'anchor':self.anchor,'anchor_enabled':self.anchor_enabled,'render_settings':self.render_settings,'lines':self.lines}
    def save_project(self):
        if not self.project_path:
            dialog = QFileDialog(self, 'Save Verse project')
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setNameFilters(['Verse project (*.verse.json)', 'JSON (*.json)'])
            dialog.setDefaultSuffix('verse.json')
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            dialog.accepted.connect(lambda d=dialog: self._save_project_path_selected(d))
            dialog.open()
            self.project_file_dialog = dialog
            return
        self._write_project(self.project_path)

    def _save_project_path_selected(self, dialog):
        files = dialog.selectedFiles()
        if files:
            self._write_project(Path(files[0]))
        self.project_file_dialog = None

    def _write_project(self, path):
        try:
            path.write_text(json.dumps(self._project_data(), indent=2), encoding='utf-8')
            self.project_path = path
            self.project_label.setText(path.stem.upper())
            self._remember_project()
        except OSError as exc:QMessageBox.critical(self,'Could not save project',str(exc))
    def open_project(self):
        dialog = QFileDialog(self, 'Open Verse project')
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilters(['Verse project (*.verse.json *.json)', 'All files (*)'])
        dialog.accepted.connect(lambda d=dialog: self._open_project_path_selected(d))
        dialog.open()
        self.project_file_dialog = dialog

    def _open_project_path_selected(self, dialog):
        files = dialog.selectedFiles()
        if files:
            self._load_project(files[0])
        self.project_file_dialog = None
    def _load_project(self,path):
        try:data=json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError) as exc:QMessageBox.critical(self,'Could not open project',str(exc));return
        self.project_path=Path(path); self.render_settings.update(data.get('render_settings',{})); self.set_lines(data.get('lines',[])); self.set_display_mode(data.get('display_mode','line')); self.set_highlight_mode(data.get('highlight_mode','word')); self.alignment=data.get('alignment','Center'); self.align_combo.setCurrentText(self.alignment); self.anchor=float(data.get('anchor',0)); self.anchor_check.setChecked(bool(data.get('anchor_enabled',False)))
        audio=data.get('audio_path')
        if audio and Path(audio).exists():
            try:
                self.audio_path=Path(audio); self.duration=probe_duration(self.audio_path); self.player.setSource(QUrl.fromLocalFile(str(self.audio_path.resolve()))); self.visualizer=self._analyze_audio(self.audio_path); self.track_info.setText(f'♫  {self.audio_path.name} · {stamp(self.duration)}')
            except Exception: QMessageBox.information(self,'Audio file missing','The project opened. Please select its audio file again.')
        self._undo_stack.clear(); self._redo_stack.clear()
        self.project_label.setText(self.project_path.stem.upper()); self.update_preview(); self._remember_project()
    def _last_project_file(self):return Path.home()/'.cache'/'verse'/'last_project.json'
    def _remember_project(self):
        if not self.project_path:return
        try:
            p=self._last_project_file();p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps({'path':str(self.project_path.resolve())}),encoding='utf-8')
        except OSError:pass
    def _restore_last_project(self):
        try:d=json.loads(self._last_project_file().read_text());p=Path(d['path'])
        except (OSError,json.JSONDecodeError,KeyError,TypeError):return
        if p.is_file():self._load_project(p)
    def export_video(self):
        if not self.audio_path:QMessageBox.information(self,'Add a track','Load an audio file before exporting.');return
        if not self.lines:QMessageBox.information(self,'Add lyrics','Add lyric lines before exporting.');return
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):QMessageBox.critical(self,'FFmpeg required','Install FFmpeg and make sure ffmpeg and ffprobe are on PATH.');return
        self.exporting = True
        if hasattr(self, 'timer'): self.timer.setInterval(100)
        dialog = QFileDialog(self, 'Export lyric video', f'{self.audio_path.stem}-lyrics.mp4')
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilters(['MP4 (*.mp4)', 'WebM (*.webm)'])
        dialog.setDefaultSuffix('mp4')
        dialog.accepted.connect(lambda d=dialog: self._export_path_selected(d))
        dialog.rejected.connect(self._cancel_export_selection)
        self.export_file_dialog = dialog
        dialog.open()

    def export_rhymesheet(self):
        if not self.lines:
            QMessageBox.information(self, 'Add lyrics', 'Add lyric lines before exporting a rhymesheet.')
            return
        dialog = QFileDialog(self, 'Export rhymesheet')
        dialog.setDirectory(str(Path.cwd()))
        dialog.selectFile(f'{self.project_label.text().lower().replace(" ", "-")}-rhymesheet.png')
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilters(['PNG image (*.png)'])
        dialog.setDefaultSuffix('png')
        dialog.accepted.connect(lambda d=dialog: self._rhymesheet_path_selected(d))
        dialog.open()

    def _rhymesheet_path_selected(self, dialog):
        files = dialog.selectedFiles()
        if not files:
            return
        output = Path(files[0])
        if output.suffix.lower() != '.png':
            output = output.with_suffix('.png')
        try:
            image = make_rhymesheet(self.lines, self.render_settings, self.project_label.text())
            if not image.save(str(output), 'PNG'):
                raise OSError('Could not write the PNG image.')
            QMessageBox.information(self, 'Rhymesheet exported', f'Your rhymesheet is ready:\n\n{output}')
        except Exception as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def _cancel_export_selection(self):
        self._finish_export_ui()

    def _export_path_selected(self, dialog):
        files = dialog.selectedFiles()
        if not files:
            self._finish_export_ui(); return
        path = Path(files[0])
        if self.playing:self.toggle_play()
        self.export_dialog=QDialog(self);self.export_dialog.setWindowTitle('Rendering video');l=QVBoxLayout(self.export_dialog);l.addWidget(QLabel('Rendering your lyric video…'));bar=QProgressBar();bar.setRange(0,0);l.addWidget(bar);self.export_dialog.show()
        preview_height = self.preview_canvas.height() if getattr(self, 'preview_canvas', None) else 430
        args=(path,self.audio_path,[copy.deepcopy(x) for x in self.lines],self.duration,self.display_mode,self.highlight_mode,self.alignment,copy.deepcopy(self.render_settings),preview_height,list(self.visualizer))
        threading.Thread(target=self._render_worker,args=args,daemon=True).start()
    def _render_worker(self, output,audio,lines,duration,display,highlight,alignment,settings,preview_height,visualizer):
        process = None
        try:
            width, height = (int(value) for value in settings['resolution'].split('x', 1))
            fps = 30
            webm = output.suffix.lower() == '.webm'
            command = [
                'ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24',
                '-video_size', f'{width}x{height}', '-framerate', str(fps), '-i', 'pipe:0',
                '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0',
                '-c:v', 'libvpx-vp9' if webm else 'libx264', '-crf', '30' if webm else '20',
                '-pix_fmt', 'yuv420p', '-c:a', 'libopus' if webm else 'aac', '-shortest',
            ]
            if not webm: command.extend(['-preset', 'ultrafast', '-threads', '2', '-movflags', '+faststart'])
            else: command.extend(['-threads', '2'])
            command.append(str(output))
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            state = type('RenderState', (), {})()
            state.audio_path=audio; state.lines=lines; state.duration=duration
            state.visualizer=visualizer
            state.display_mode=display; state.highlight_mode=highlight; state.alignment=alignment; state.render_settings=settings
            frame_count = max(1, round(duration * fps))
            for frame_number in range(frame_count):
                image = QImage(width, height, QImage.Format.Format_RGB888)
                painter = QPainter(image)
                paint_verse_frame(painter, state, width, height, frame_number / fps, preview_height, show_preview_chrome=False)
                painter.end()
                process.stdin.write(image.bits().asstring(image.sizeInBytes()))
            process.stdin.close()
            stderr = process.stderr.read().decode('utf-8', errors='replace')
            return_code = process.wait()
            if return_code: raise RuntimeError(stderr[-2000:] or f'FFmpeg exited with status {return_code}.')
            self.bridge.finished.emit(str(output))
        except Exception as exc:
            if process and process.poll() is None:
                process.kill(); process.wait()
            self.bridge.failed.emit(str(exc))
    def _finish_export_ui(self):
        self.exporting = False
        if hasattr(self, 'timer'): self.timer.setInterval(16 if self.display_mode == 'scroll' else 80)
        self.update_preview()
        self.export_file_dialog = None
        if getattr(self, 'export_dialog', None): self.export_dialog.close()
    def _export_done(self,path):self._finish_export_ui();QMessageBox.information(self,'Video exported',f'Your lyric video is ready:\n\n{path}')
    def _export_failed(self,error):self._finish_export_ui();QMessageBox.critical(self,'Export failed',error)
    def keyPressEvent(self,event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Equal,Qt.Key.Key_Plus):self.zoom(1);return
            if event.key()==Qt.Key.Key_Minus:self.zoom(-1);return
            if event.key()==Qt.Key.Key_S:self.save_project();return
        if event.key()==Qt.Key.Key_Space and not isinstance(self.focusWidget(),(QLineEdit,QTextEdit)):self.toggle_play();return
        if event.key()==Qt.Key.Key_Return and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            index=next((i for i,x in enumerate(self.lines) if x.get('start') is None),self.active_index())
            if index<0:self.add_line()
            else:self.sync_line(index)
            return
        super().keyPressEvent(event)
    def eventFilter(self, watched, event):
        if isinstance(watched, QLineEdit) and event.type() == QEvent.Type.KeyPress and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Z: self.undo(); return True
            if event.key() == Qt.Key.Key_Y: self.redo(); return True
        return super().eventFilter(watched, event)
    def zoom(self,step):
        self.zoom_steps = max(-3, min(8, getattr(self, 'zoom_steps', 0) + step))
        self.zoom_factor = 1 + self.zoom_steps * .1
        QApplication.setFont(QFont(FONT, max(7, 10 + self.zoom_steps)))
        for widget in QApplication.allWidgets():
            base_size = widget.property('_zoom_base_point_size')
            if base_size is None:
                self._register_zoom_fonts(widget)
                base_size = widget.property('_zoom_base_point_size')
            if base_size:
                font = widget.font()
                font.setPointSizeF(max(6.0, float(base_size) * self.zoom_factor))
                widget.setFont(font)
    def _register_zoom_fonts(self, parent):
        widgets = [parent, *parent.findChildren(QWidget)]
        for widget in widgets:
            if widget.property('_zoom_base_point_size') is None:
                point_size = widget.font().pointSizeF()
                if point_size > 0:
                    widget.setProperty('_zoom_base_point_size', point_size / self.zoom_factor)
    def closeEvent(self,event):
        self.player.stop()
        event.accept()


def run():
    app=QApplication([]);app.setApplicationName('Verse Lyrics Studio');window=VerseQt();window.show();return app.exec()
