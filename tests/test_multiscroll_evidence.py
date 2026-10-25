"""Multi-scroll consumer conformance with physical PROTOCOL files, never native proof."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tests')]
import release_evidence as evidence
import test_release_publish as fixtures


def scroll_row(row):
    """Model the actual 201/831 scroll dimensions with fully visible client bounds."""
    row = copy.deepcopy(row)
    region = {'id': 'ASIDE', 'scroll_top': 0, 'client_height': 201, 'scroll_height': 831,
              'viewport_rect': [250, 400, 450, 601], 'visible_rect': [250, 400, 450, 601]}
    row.update(scroll_regions=[region], scroll_captures=[])
    for index, offset in enumerate((0, 180, 360, 540, 630)):
        image = copy.deepcopy(row['image'])
        if index:
            image['path'] = f"{row['theme']}-{row['width']}-{row['state']}-controls-{index}.png"
        frame = {key: copy.deepcopy(value) for key, value in row.items()
                 if key not in ('image', 'scroll_captures', 'width', 'state', 'status', 'controls_checked', 'realized_theme')}
        frame['scroll_regions'][0]['scroll_top'] = offset
        row['scroll_captures'].append({'id': 'ASIDE', 'scroll_top': offset, 'client_height': 201,
                                      'scroll_height': 831, 'image': image, 'frame': frame})
    return row


class ScrollEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.files = fixtures.candidate_bytes('arm64')
        cls.raw_name = 'evidence/mac-arm64/bundled/renderer.json'
        cls.raw = json.loads(cls.files[cls.raw_name])

    def setUp(self):
        self.report = copy.deepcopy(self.raw)
        self.report['matrix'][0] = scroll_row(self.report['matrix'][0])
        self.row = self.report['matrix'][0]
        self.report['images'] += [capture['image'] for capture in self.row['scroll_captures'][1:]]

    def validate_raw(self, report=None):
        report = report or self.report
        return evidence.validate_native_renderer(report, expected_sha=fixtures.SHA, expected_arch='arm64',
            expected_version=fixtures.VERSION, bundled=True, started=report['launch']['started'], finished=report['launch']['finished'])

    def test_actual_201_831_model_has_complete_ordered_visible_coverage(self):
        self.assertEqual(evidence.scroll_offsets(201, 831), [0, 180, 360, 540, 630])
        self.validate_raw()
        self.assertEqual(len(self.report['images']), len(self.raw['images']) + 4)

    def test_missing_middle_bottom_duplicate_reordered_or_legacy_alias_are_red(self):
        mutations = [lambda row: row['scroll_captures'].pop(2),
                     lambda row: row['scroll_captures'].pop(),
                     lambda row: row['scroll_captures'].__setitem__(2, copy.deepcopy(row['scroll_captures'][1])),
                     lambda row: row['scroll_captures'].reverse(),
                     lambda row: row.update(controls_image=row['image']),
                     lambda row: row.pop('scroll_captures'),
                     lambda row: row['scroll_captures'][1].update(id='FOREIGN')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row = copy.deepcopy(self.row)
                mutation(row)
                with self.assertRaises(ValueError):
                    evidence.validate_scroll_coverage(row)

    def test_each_midframe_has_realized_offset_and_same_certified_geometry(self):
        changes = [{'hash': 'b' * 16}, {'tier': 'preview'}, {'gates_pass': False}, {'clipped': 1},
                   {'nonfinite': 1}, {'degenerate': 1}, {'vertices': 0}, {'overflow': True},
                   {'diag': 'layout error'}, {'errors': ['renderer failure']}, {'theme': 'light'},
                   {'viewport': [450, 699]}, {'step_pressed': ['false', 'false']}, {'dpr': 2}]
        for change in changes:
            row = copy.deepcopy(self.row)
            row['scroll_captures'][2]['frame'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                evidence.validate_scroll_coverage(row)
        row = copy.deepcopy(self.row)
        row['scroll_captures'][2]['frame']['scroll_regions'][0]['scroll_top'] = 0
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)
        # Python bool==1 must not substitute for the producer's strict integer metadata.
        row = copy.deepcopy(self.row)
        row['scroll_regions'][0].update(client_height=1, scroll_height=2,
                                       viewport_rect=[250, 400, 450, 401], visible_rect=[250, 400, 450, 401])
        row['scroll_captures'] = row['scroll_captures'][:2]
        for index, capture in enumerate(row['scroll_captures']):
            capture.update(scroll_top=index, client_height=1, scroll_height=2)
            capture['frame']['scroll_regions'] = [{**row['scroll_regions'][0], 'scroll_top': index}]
        evidence.validate_scroll_coverage(row)
        row['scroll_captures'][1]['client_height'] = True
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)
        row['scroll_captures'][1]['client_height'] = 1
        row['scroll_captures'][1]['frame']['scroll_regions'][0]['client_height'] = True
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)

    def test_missing_baseline_and_frame_hash_or_dpr_cannot_pass_none_equality(self):
        for key in ('hash', 'dpr'):
            report = copy.deepcopy(self.report)
            report['matrix'][0].pop(key)
            for capture in report['matrix'][0]['scroll_captures']:
                capture['frame'].pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate_raw(report)

    def test_offscreen_hidden_or_clipping_ancestor_invalidates_claimed_scroll_span(self):
        for rect in ([250, 400, 450, 500], [250, 400, 250, 601], [250, 400, 450, 701]):
            row = copy.deepcopy(self.row)
            row['scroll_regions'][0]['visible_rect'] = rect
            with self.subTest(rect=rect), self.assertRaises(ValueError):
                evidence.validate_scroll_coverage(row)
        row = copy.deepcopy(self.row)
        for region in [row['scroll_regions'][0], *[capture['frame']['scroll_regions'][0] for capture in row['scroll_captures']]]:
            region.update(viewport_rect=[350, 150, 350.5, 351], visible_rect=[350, 150, 350, 351])
        with self.assertRaisesRegex(ValueError, 'visible geometry'):
            evidence.validate_scroll_coverage(row)  # Even a <=1px difference cannot authorize zero visible area.
        row = copy.deepcopy(self.row)
        row['scroll_regions'][0]['viewport_rect'][3] = 702
        row['scroll_regions'][0]['visible_rect'][3] = 702
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)
        row = copy.deepcopy(self.row)
        row['scroll_captures'][2]['frame']['scroll_regions'][0]['visible_rect'][3] -= 20
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)

    def test_all_supplemental_capture_paths_are_required_and_never_duplicated(self):
        report = copy.deepcopy(self.report)
        report['images'].pop()
        with self.assertRaises(ValueError):
            self.validate_raw(report)
        row = copy.deepcopy(self.row)
        row['scroll_captures'][2]['image'] = copy.deepcopy(row['scroll_captures'][1]['image'])
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)
        row = copy.deepcopy(self.row)
        row['scroll_captures'][1]['image']['height'] = 100
        with self.assertRaises(ValueError):
            evidence.validate_scroll_coverage(row)
        with self.assertRaisesRegex(ValueError, 'bounded capture limit'):
            evidence.scroll_offsets(1, 1000)

    def physical_candidate(self, root):
        files = dict(self.files)
        cold = copy.deepcopy(self.report)
        prefix = Path(self.raw_name).parent.as_posix()
        original = files[prefix + '/' + cold['matrix'][0]['image']['path']]
        for capture in cold['matrix'][0]['scroll_captures'][1:]:
            files[prefix + '/' + capture['image']['path']] = original
        files[self.raw_name] = json.dumps(cold, sort_keys=True).encode()
        outer_name = 'evidence/mac-arm64/renderer.json'
        outer = json.loads(files[outer_name])
        outer['bundled_report'].update(bytes=len(files[self.raw_name]), sha256=hashlib.sha256(files[self.raw_name]).hexdigest())
        outer['images'] = [{**image, 'path': 'bundled/' + image['path']} for image in cold['images']] + [
            image for image in outer['images'] if not image['path'].startswith('bundled/')]
        files[outer_name] = json.dumps(outer, sort_keys=True).encode()
        manifest = json.loads(files['candidate.json'])
        manifest['files'] = [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                             for name, data in sorted(files.items()) if name != 'candidate.json']
        records = {record['path']: record for record in manifest['files']}
        manifest['gates'] = {name: records[template.format(arch='arm64')] for name, template in evidence.GATES.items()}
        manifest['assets'] = [records[record['path']] for record in manifest['assets']]
        files['candidate.json'] = json.dumps(manifest).encode()
        for name, data in files.items():
            file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
        return manifest, files

    def test_physical_supplemental_images_survive_common_admission_and_rehashed_crc_corruption_is_red(self):
        with tempfile.TemporaryDirectory(prefix='cq-scroll-PROTOCOL-') as directory:
            root = Path(directory).resolve()  # Match the helper's canonical Mac /var→/private/var containment contract.
            manifest, files = self.physical_candidate(root)
            kwargs = dict(expected_sha=fixtures.SHA, expected_run_id=fixtures.RUN_ID, expected_arch='arm64', expected_attempt=1, expected_version=fixtures.VERSION)
            evidence.verify_candidate(root, manifest, **kwargs)
            from test_release_evidence import NativeConformanceTests
            case = NativeConformanceTests('test_complete_producer_protocol_and_all_physical_images_pass')
            case.root = root; case.files = files; case.fixtures = fixtures
            changed = root / 'evidence/mac-arm64/bundled/dark-450-assembled-controls-2.png'
            original = changed.read_bytes()
            changed.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
            case.refresh()
            with self.assertRaisesRegex(ValueError, 'PNG checksum mismatch'):
                case.verify()

    def test_matrix_identity_normalizes_every_tile_image_without_dropping_frame_proof(self):
        with tempfile.TemporaryDirectory(prefix='cq-scroll-path-') as directory:
            root = Path(directory); child = root / 'evidence/dmg-arm64/mounted'; child.mkdir(parents=True)
            row = copy.deepcopy(self.row)
            for image in [row['image'], *[capture['image'] for capture in row['scroll_captures']]]:
                file = child / image['path']; file.write_bytes(b'physical path-normalization fixture')
                image['sha256'] = evidence.digest(file); image['bytes'] = file.stat().st_size
            wrapper = copy.deepcopy(row)
            wrapper['image']['path'] = 'mounted/' + row['image']['path']
            for capture in wrapper['scroll_captures']:
                capture['image']['path'] = 'mounted/' + capture['image']['path']
            local = evidence.matrix_identity(root, 'evidence/dmg-arm64/mounted/renderer.json', [row])
            portable = evidence.matrix_identity(root, 'evidence/dmg-arm64/package.json', [wrapper])
            self.assertEqual(local, portable)
            self.assertEqual(len(local[0]['scroll_captures']), 5)
            self.assertEqual(local[0]['scroll_captures'][2]['frame']['scroll_regions'][0]['scroll_top'], 360)


if __name__ == '__main__':
    unittest.main()
