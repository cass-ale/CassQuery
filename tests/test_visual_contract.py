import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(a, b):
    x, y = sorted((luminance(a), luminance(b)))
    return (y + 0.05) / (x + 0.05)


class VisualContractTests(unittest.TestCase):
    def test_both_theme_text_and_state_tokens_meet_aa(self):
        css = (ROOT / 'viewer/style.css').read_text()
        roots = re.findall(r':root\s*\{([^}]+)\}', css)
        self.assertEqual(len(roots), 2)
        for root in roots:
            palette = dict(re.findall(r'--([\w-]+):\s*(#[0-9a-fA-F]{6})', root))
            for token in ('ink', 'muted', 'accent', 'bad', 'ok'):
                for surface in ('bg', 'panel', 'panel-2'):
                    with self.subTest(token=token, surface=surface, theme=palette['bg']):
                        self.assertGreaterEqual(contrast(palette[token], palette[surface]), 4.5)

    def test_ui_identity_and_accessibility_contract(self):
        html = (ROOT / 'viewer/index.html').read_text()
        css = (ROOT / 'viewer/style.css').read_text()
        self.assertIn('src="./brand.svg"', html)
        self.assertIn('aria-label="Model controls"', html)
        self.assertIn('id="diag" role="alert"', html)
        for rule in (':focus-visible', 'prefers-reduced-motion', 'prefers-color-scheme: light', 'min-height: 44px'):
            self.assertIn(rule, css)
        self.assertNotIn('http', html.split('<script type="importmap">')[0])

    def test_demo_uses_aspect_aware_framing_not_fixed_camera(self):
        import json
        manifest = json.loads((ROOT / 'testpart/viewer.json').read_text())
        self.assertTrue(all('camera' not in step for step in manifest['steps']))
