"""Locks the visual design system down.

The RelayDesk UI is one hand-authored QSS sheet over one token table, so a
"looks fine on my machine" review catches almost nothing: a tone that drifts
below readable contrast, a token that exists in one theme but not the other, or
a colour smuggled back into widget code all ship silently.

These tests are the review. They need PySide6 only for the sheet-rendering
checks, so they skip cleanly on a bare CI box.

Run: QT_QPA_PLATFORM=offscreen pytest tests/test_gui_theme.py -q
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "flixcli" / "gui.py"

try:
    from flixcli import gui as gui_module
except ImportError:  # pragma: no cover - PySide6 is an optional extra
    gui_module = None

HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
RGBA = re.compile(r"^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$")


def channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def rgb(color: str) -> tuple[float, float, float]:
    match = RGBA.match(color)
    if match:
        return tuple(float(part) for part in match.groups()[:3])  # type: ignore[return-value]
    body = color.lstrip("#")
    if len(body) == 3:
        body = "".join(char * 2 for char in body)
    return tuple(float(int(body[i:i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(color: str) -> float:
    red, green, blue = (channel(part) for part in rgb(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio between two opaque colours."""
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def flatten(color: str, background: str) -> str:
    """Composite a translucent colour over an opaque one."""
    match = RGBA.match(color)
    if not match:
        return color
    alpha = float(match.group(4) or 1.0)
    over = [int(round(part * alpha + base * (1 - alpha)))
            for part, base in zip(rgb(color), rgb(background))]
    return "#%02x%02x%02x" % tuple(over)


def _glyph_ink(widget) -> str:
    """Dominant opaque colour of an icon, as the eye would read it."""
    from PySide6.QtGui import QColor

    image = widget.icon().pixmap(16, 16).toImage() if hasattr(widget, "icon") \
        else widget.pixmap().toImage()
    counts: dict[str, int] = {}
    for y in range(image.height()):
        for x in range(image.width()):
            colour = QColor(image.pixelColor(x, y))
            if colour.alpha() > 200:
                counts[colour.name()] = counts.get(colour.name(), 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _channel_distance(left: str, right: str) -> int:
    """Largest per-channel gap; anti-aliased icons round by a unit or two."""
    a, b = rgb(left), rgb(right)
    return int(max(abs(x - y) for x, y in zip(a, b)))


@unittest.skipIf(gui_module is None, "PySide6 is not installed")
class PaletteTests(unittest.TestCase):
    AA_TEXT = 4.5
    AA_NONTEXT = 3.0

    def test_both_themes_declare_the_same_tokens(self):
        # A token that exists in one theme only is a latent KeyError: the sheet
        # is rendered per theme, so the other one would crash on first flip.
        dark = set(gui_module.PALETTES["dark"])
        light = set(gui_module.PALETTES["light"])
        self.assertEqual(dark, light)

    def test_surfaces_are_distinct(self):
        # Two tokens resolving to the same tone means one of the two planes
        # silently stops existing.
        for theme, tokens in gui_module.PALETTES.items():
            with self.subTest(theme=theme):
                tones = [tokens[name] for name in
                         ("bg_deep", "bg", "bg_rail", "bg_side", "surface",
                          "surface_hi", "surface_2")]
                self.assertEqual(len(set(tones)), len(tones))

    def test_page_fades_to_a_deeper_tone(self):
        for theme, tokens in gui_module.PALETTES.items():
            with self.subTest(theme=theme):
                self.assertLess(luminance(tokens["bg_deep"]),
                                luminance(tokens["bg"]),
                                "the page gradient must darken downwards")

    def test_planes_separate_from_their_neighbours(self):
        for theme, tokens in gui_module.PALETTES.items():
            with self.subTest(theme=theme, edge="card-on-sidebar"):
                # Cards have to read as a raised plane over the sidebar.
                self.assertGreaterEqual(
                    contrast(tokens["surface"], tokens["bg_side"]), 1.06)
            for edge, floor in (("border", 1.15), ("border_hi", 1.40),
                                ("scroll", 1.30)):
                with self.subTest(theme=theme, edge=edge):
                    # The dark theme leans on hairlines for structure, so the
                    # edges themselves have to stay visible on a card.
                    self.assertGreaterEqual(
                        contrast(tokens[edge], tokens["surface"]), floor)
            with self.subTest(theme=theme, edge="scroll-feedback"):
                self.assertGreater(contrast(tokens["scroll_hi"], tokens["surface"]),
                                   contrast(tokens["scroll"], tokens["surface"]))

    def test_text_clears_wcag_aa(self):
        for theme, tokens in gui_module.PALETTES.items():
            for ink in ("text", "text_dim", "muted"):
                for surface in ("bg", "bg_deep", "bg_rail", "bg_side",
                                "surface", "surface_hi", "surface_2"):
                    with self.subTest(theme=theme, ink=ink, surface=surface):
                        self.assertGreaterEqual(
                            contrast(tokens[ink], tokens[surface]), self.AA_TEXT,
                            f"{ink} on {surface} is unreadable")
            with self.subTest(theme=theme, ink="danger"):
                self.assertGreaterEqual(contrast(tokens["danger"], tokens["bg"]),
                                        self.AA_TEXT)

    def test_accent_labels_clear_wcag_aa(self):
        for theme, tokens in gui_module.PALETTES.items():
            # The outgoing bubble is a gradient, so the label has to survive
            # both ends, not just the token called "accent".
            for name in ("accent", "accent_hi", "bubble_end"):
                with self.subTest(theme=theme, end=name):
                    self.assertGreaterEqual(
                        contrast(tokens["on_accent"], tokens[name]), self.AA_TEXT)
            for name in ("accent", "bubble_end"):
                with self.subTest(theme=theme, meta_end=name):
                    meta = flatten(tokens["on_accent_meta"], tokens[name])
                    self.assertGreaterEqual(
                        contrast(meta, tokens[name]), self.AA_TEXT,
                        f"bubble timestamp on {name} is below AA")

    def test_presence_dots_clear_wcag_nontext(self):
        for theme, tokens in gui_module.PALETTES.items():
            for ink in ("online", "offline"):
                for surface in ("bg_side", "surface"):
                    with self.subTest(theme=theme, ink=ink, surface=surface):
                        self.assertGreaterEqual(
                            contrast(tokens[ink], tokens[surface]),
                            self.AA_NONTEXT)

    def test_avatar_glyphs_stay_legible(self):
        for tone in gui_module.AVATAR_TONES:
            with self.subTest(tone=tone):
                self.assertGreaterEqual(contrast("#ffffff", tone), self.AA_TEXT)

    def test_no_colour_enters_the_ui_undeclared(self):
        """Every hex literal in the module must come from a declared table.

        This is what keeps a theme flip honest: the toast card and the title
        bar used to carry their own private greys and stayed dark on light.
        """
        declared = set(gui_module.AVATAR_TONES)
        for tokens in gui_module.PALETTES.values():
            declared.update(tokens.values())
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        literals = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and HEX.match(node.value)
        }
        undeclared = sorted(literals - declared)
        self.assertEqual(undeclared, [],
                         f"undeclared colours in gui.py: {undeclared}")


@unittest.skipIf(gui_module is None, "PySide6 is not installed")
class StylesheetTests(unittest.TestCase):
    AA_TEXT = 4.5
    def test_every_token_resolves(self):
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                sheet = gui_module.styles_for(theme)
                self.assertNotIn("$", sheet, "an unfilled template token leaked")
                self.assertGreater(len(sheet), 4000)

    def test_sheets_differ_per_theme(self):
        dark = gui_module.styles_for("dark")
        light = gui_module.styles_for("light")
        self.assertNotEqual(dark, light)
        self.assertIn(gui_module.PALETTES["dark"]["bg"], dark)
        self.assertIn(gui_module.PALETTES["light"]["bg"], light)

    def test_sheet_parses_in_a_real_widget(self):
        try:
            from PySide6.QtWidgets import QApplication, QCheckBox
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 widgets unavailable")
        app = QApplication.instance() or QApplication([])  # noqa: F841
        for theme in ("dark", "light"):
            checkbox = QCheckBox("提醒")
            checkbox.setStyleSheet(gui_module.styles_for(theme))
            checkbox.ensurePolished()
            self.assertFalse(checkbox.style().pixelMetric(
                checkbox.style().PixelMetric.PM_IndicatorWidth) <= 0)

    def test_radius_and_type_ramps_are_ordered(self):
        scale = gui_module.SCALE
        radii = [float(scale[name][:-2]) for name in
                 ("r_xs", "r_sm", "r_md", "r_lg", "r_xl")]
        self.assertEqual(radii, sorted(radii))
        sizes = [float(scale[name][:-2]) for name in
                 ("fs_xs", "fs_sm", "fs_md", "fs_base", "fs_lg", "fs_display",
                  "fs_xl")]
        self.assertEqual(sizes, sorted(sizes))

    def test_close_button_ink_survives_its_red_hover(self):
        """The title bar close button paints $danger on hover.

        Its glyph is a baked pixmap, so QSS cannot recolour it: if the resting
        grey is left in place it disappears against the red.
        """
        try:
            from PySide6.QtGui import QColor, QEnterEvent
            from PySide6.QtCore import QEvent, QPointF
            from PySide6.QtWidgets import QApplication
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 widgets unavailable")
        app = QApplication.instance() or QApplication([])  # noqa: F841
        window = gui_module.MainWindow()
        button = window.title_bar.btn_close
        point = QPointF(1, 1)

        for theme in ("dark", "light"):
            window.apply_theme(theme)
            tokens = gui_module.PALETTES[theme]
            resting = _glyph_ink(button)
            with self.subTest(theme=theme, state="resting"):
                self.assertEqual(resting, tokens["muted"])
            button.enterEvent(QEnterEvent(point, point, point))
            with self.subTest(theme=theme, state="hover"):
                self.assertEqual(_glyph_ink(button), tokens["on_accent"])
                self.assertGreaterEqual(
                    contrast(tokens["on_accent"], tokens["danger"]), self.AA_TEXT)
            button.leaveEvent(QEvent(QEvent.Leave))
            with self.subTest(theme=theme, state="restored"):
                self.assertEqual(_glyph_ink(button), resting)

    def test_file_glyph_follows_the_theme(self):
        """A mounted file bubble bakes its glyph; a theme flip must recolour it.

        Rebuilding the whole transcript would work too, but it fights the
        scroll anchoring, so _refresh_icons repaints just those labels.
        """
        try:
            from PySide6.QtGui import QColor
            from PySide6.QtWidgets import QApplication, QLabel
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 widgets unavailable")
        app = QApplication.instance() or QApplication([])  # noqa: F841
        window = gui_module.MainWindow()
        window.self_id = "me"
        window.current = {"kind": "device", "key": "peer"}
        window.payload = {"bubbles": [{
            "id": "m1", "from": "peer", "to": "me", "type": 1,
            "time": 1_740_000_000_000, "content": {"name": "a.log",
                                                       "size": 12,
                                                       "path": "/nope/a.log"},
        }]}
        window._timeline_signature = None
        window.apply_theme("dark")
        window.render_timeline()

        glyphs = window._timeline_rows["msg:m1"].findChildren(QLabel, "fileGlyph")
        self.assertEqual(len(glyphs), 1)
        dark = _glyph_ink(glyphs[0])
        window.apply_theme("light")
        light = _glyph_ink(glyphs[0])
        self.assertNotEqual(dark, light, "the glyph kept the old ink")
        self.assertLess(_channel_distance(light, gui_module.PALETTES["light"]["icon"]), 4)

    def test_type_sizes_come_from_the_ramp(self):
        # A stray "font-size: 19px" is how a type scale quietly stops being a
        # scale; the sheet is expected to reference tokens only.
        strays = re.findall(r"font-size:\s*([\d.]+px)", gui_module.STYLES)
        self.assertEqual(strays, [], f"literal font sizes in the sheet: {strays}")


if __name__ == "__main__":
    unittest.main()
