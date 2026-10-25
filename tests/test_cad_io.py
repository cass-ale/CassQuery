"""Real public TESTPLATE certificate → native CAD encoders → roundtrip proof."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cadquery as cq
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_Reader
import cad_io
import worker
import workbench


class CadIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cq-cad-io-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'TESTPLATE'
        shutil.copytree(ROOT / 'testpart', self.root)
        (self.root / '__init__.py').write_bytes(b'')
        payload = self.base / 'payloads'; payload.mkdir()
        self.globals = patch.multiple(worker, PAYLOAD_DIR=str(payload), SHOT_DIR=str(self.base / 'shots'), PROJECTS={})
        self.globals.start(); self.addCleanup(self.globals.stop)
        self.project = worker.Project(str(self.root))
        worker.PROJECTS[self.project.name] = self.project
        self.build_calls = patch.object(self.project, 'build', wraps=self.project.build).start()
        self.addCleanup(patch.stopall)
        self.certify()

    def certify(self):
        self.project.preview_dirty = self.project.certified_due = False  # Scheduler consumes these before build.
        self.assertTrue(worker.run_build(self.project, 'certified'), self.project.history)
        self.assertIs(self.project.gates['pass'], True)
        self.assertEqual({c['id'] for c in self.project.gates['checks']}, cad_io.GATES)

    def export(self, filename, fmt, **kwargs):
        with worker.certified_record(self.project.name) as record:
            return cad_io.export_geometry(record, self.base / filename, fmt, **kwargs)

    def test_real_certificate_all_formats_and_empty_source_preserved_without_rebuild(self):
        count = self.build_calls.call_count
        for fmt, suffix in [('STEP', '.step'), ('STL', '.stl'), ('BREP', '.brep'), ('ZIP', '.zip'), ('PY', '.py')]:
            with self.subTest(fmt=fmt):
                receipt = self.export('model' + suffix, fmt)
                file = Path(receipt['path'])
                self.assertEqual(receipt['sha256'], hashlib.sha256(file.read_bytes()).hexdigest())
                self.assertEqual(receipt['bytes'], file.stat().st_size)
                self.assertEqual(receipt['bounds']['extent'], [40.0, 30.0, 32.0])
                self.assertEqual(receipt['solids'], 3)
                self.assertGreater(receipt['volume_mm3'], 8000)
                self.assertEqual(receipt['units'], 'mm')
                self.assertEqual(receipt['certified_hash'], self.project.certified['hash'])
                self.assertEqual(receipt['source_fingerprint'], self.project.certified['source_fingerprint'])
                if fmt in ('STEP', 'BREP'):
                    self.assertEqual(receipt['roundtrip']['solids'], 3)
                    self.assertAlmostEqual(receipt['volume_mm3'], receipt['roundtrip']['volume_mm3'], places=5)
                elif fmt == 'STL':
                    self.assertIs(receipt['roundtrip']['watertight'], True)
                    self.assertGreater(receipt['roundtrip']['triangles'], 100)
                    self.assertLess(abs(receipt['roundtrip']['volume_mm3'] / receipt['volume_mm3'] - 1), .01)
                elif fmt == 'ZIP':
                    with zipfile.ZipFile(file) as archive:
                        self.assertEqual(archive.read('__init__.py'), b'')
                        self.assertEqual(archive.read('part_entry.py'), (self.root / 'part_entry.py').read_bytes())
                        metadata = archive.read('cassquery-export.json')
                        self.assertNotIn(str(self.root).encode(), metadata)
                        self.assertIsNone(archive.testzip())
                else:
                    self.assertEqual(file.read_bytes(), (self.root / 'part_entry.py').read_bytes())
                    self.assertEqual(receipt['roundtrip']['scope'], 'entry-source-only')
                    self.assertIn('__init__.py', receipt['roundtrip']['dependency_paths'])
        self.assertEqual(self.build_calls.call_count, count, 'Export must never rerun build-only Python')

    def test_step_import_copies_asset_and_generated_editable_project_recertifies(self):
        receipt = self.export('original.step', 'STEP')
        original = (self.base / 'original.step').read_bytes()
        with worker.geometry_operation():
            imported = cad_io.import_step(self.base / 'original.step', self.base, 'Imported')
        root = Path(imported['root'])
        self.assertEqual((root / 'assets/imported.step').read_bytes(), original)
        self.assertEqual((self.base / 'original.step').read_bytes(), original)
        self.assertNotIn(str(self.base / 'original.step'), (root / 'part_entry.py').read_text())
        self.assertEqual(imported['receipt']['source_sha256'], receipt['sha256'])
        self.assertIs(imported['receipt']['certified'], False)
        p = worker.Project(str(root)); worker.PROJECTS[p.name] = p
        self.assertTrue(worker.run_build(p, 'certified'), p.history)
        self.assertIs(p.gates['pass'], True)
        with worker.certified_record(p.name) as record:
            result = cad_io.export_geometry(record, self.base / 'reimported.brep', 'BREP')
        self.assertEqual(result['solids'], 3)
        self.assertAlmostEqual(result['volume_mm3'], receipt['volume_mm3'], places=5)

    def test_inch_step_import_is_actual_mm_conversion_and_preserves_static_settings(self):
        STEPControl_Reader()
        source = self.project.certified['variants']['neutral']['Cap'].scale(1 / 16)
        file = self.base / 'inch.stp'
        with cad_io._step_settings():
            source.exportStep(str(file), unit='INCH', outputUnit='INCH')
        Interface_Static.SetCVal_s('xstep.cascade.unit', 'INCH')
        Interface_Static.SetCVal_s('write.step.unit', 'INCH')
        Interface_Static.SetIVal_s('write.surfacecurve.mode', 0)
        Interface_Static.SetIVal_s('write.precision.mode', 1)
        before = self.settings()
        with worker.geometry_operation():
            imported = cad_io.import_step(file, self.base, 'Inches')
        self.assertEqual(self.settings(), before)
        self.assertAlmostEqual(imported['receipt']['bounds']['extent'][0], 25.4, places=6)
        self.assertAlmostEqual(imported['receipt']['bounds']['extent'][2], 6.35, places=6)
        self.export('settings.step', 'STEP')
        self.assertEqual(self.settings(), before)

    def settings(self):
        return (Interface_Static.CVal_s('xstep.cascade.unit'), Interface_Static.CVal_s('write.step.unit'),
                Interface_Static.IVal_s('write.surfacecurve.mode'), Interface_Static.IVal_s('write.precision.mode'))

    def test_selected_parts_and_real_pose_not_display_explosion(self):
        (self.root / 'poses.py').write_text('def shifted(model):\n    return {n:s.translate((100,0,0)) for n,s in model["parts"].items()}\nPOSES={"shifted":shifted}\n')
        manifest = json.loads((self.root / 'viewer.json').read_text()); manifest['poses'] = 'poses.py'
        (self.root / 'viewer.json').write_text(json.dumps(manifest)); self.certify()
        neutral = self.export('cap.step', 'STEP', parts=['Cap'])
        shifted = self.export('pose.step', 'STEP', variant='shifted', parts=['Cap'])
        self.assertEqual(neutral['parts'], ['Cap'])
        self.assertAlmostEqual(neutral['volume_mm3'], 1024, places=6)
        self.assertAlmostEqual(shifted['bounds']['min'][0] - neutral['bounds']['min'][0], 100, places=6)
        self.assertEqual(shifted['bounds']['extent'], neutral['bounds']['extent'])

    def test_exact_brep_summary_and_receipts_ignore_existing_triangulation(self):
        with worker.certified_record(self.project.name) as record:
            base = record['variants']['neutral']['Base']
            native_box = base.BoundingBox()
            self.assertLess(native_box.zmin, 0)  # Actual certified tessellation expands CQ's default box.
            before = cad_io._summary(base)
            base.exportStl(str(self.base / 'meshing-only.stl'), tolerance=.1, angularTolerance=.2,
                           relative=False, parallel=False)
            after = cad_io._summary(base)
            self.assertEqual(before, after)
            self.assertEqual(after['bounds']['min'], [-20.0, -15.0, 0.0])
            self.assertEqual(after['bounds']['max'], [20.0, 15.0, 6.0])
            for fmt, extension in [('STEP','step'), ('BREP','brep'), ('STL','stl')]:
                receipt = cad_io.export_geometry(record, self.base / ('exact-base.' + extension), fmt, parts=['Base'])
                self.assertEqual(receipt['bounds'], before['bounds'])
                if fmt != 'STL': self.assertEqual(receipt['roundtrip']['bounds'], before['bounds'])

    def test_actual_brep_transform_certifies_before_export(self):
        manifest = json.loads((self.root / 'viewer.json').read_text())
        manifest['workbench'] = {'transforms': {'Cap': {'translation': [10,20,30], 'rotation': [0,0,90], 'scale': 1.5}}}
        (self.root / 'viewer.json').write_text(json.dumps(manifest)); self.certify()
        result = self.export('transformed.step', 'STEP', parts=['Cap'])
        self.assertAlmostEqual(result['volume_mm3'], 1024 * 1.5 ** 3, places=5)
        for got, expected in zip(result['bounds']['extent'], [24,24,6]): self.assertAlmostEqual(got, expected, places=6)
        self.assertAlmostEqual(result['bounds']['min'][2], 72, places=6)
        self.assertAlmostEqual(result['roundtrip']['volume_mm3'], result['volume_mm3'], places=5)

    def test_initial_stale_and_during_export_source_changes_leave_original_output(self):
        file = self.base / 'keep.step'; file.write_bytes(b'original output')
        original = cq.Shape.exportStep
        def changed(shape, path, **kwargs):
            status = original(shape, path, **kwargs)
            entry = self.root / 'part_entry.py'; entry.write_text(entry.read_text() + '\n# changed during export\n')
            return status
        with worker.certified_record(self.project.name) as record, patch.object(cq.Shape, 'exportStep', new=changed):
            with self.assertRaises(workbench.WorkbenchError) as error:
                cad_io.export_geometry(record, file, 'STEP', overwrite=True)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(file.read_bytes(), b'original output')
        self.assertFalse(list(self.base.glob('.cassquery-export-*')))
        with self.assertRaises(workbench.WorkbenchError):
            self.export('stale.step', 'STEP')

    def test_preview_or_failed_new_manifest_cannot_export_old_certificate(self):
        old = self.project.certified
        manifest = json.loads((self.root / 'viewer.json').read_text()); manifest['unit_scale'] = 2
        (self.root / 'viewer.json').write_text(json.dumps(manifest))
        self.project.preview_dirty = self.project.certified_due = False
        self.assertFalse(worker.run_build(self.project, 'certified'))
        self.assertIs(self.project.certified, old)
        with self.assertRaises(workbench.WorkbenchError): self.export('failed.step', 'STEP')
        self.assertFalse((self.base / 'failed.step').exists())

    def test_explicit_overwrite_and_output_conflict_are_atomic(self):
        file = self.base / 'overwrite.step'; file.write_bytes(b'old')
        with self.assertRaises(cad_io.CadIOError) as error: self.export(file.name, 'STEP')
        self.assertEqual(error.exception.status, 409); self.assertEqual(file.read_bytes(), b'old')
        self.export(file.name, 'STEP', overwrite=True)
        self.assertTrue(file.read_bytes().startswith(b'ISO-10303-21'))
        original = cq.Shape.exportStep
        def external_edit(shape, path, **kwargs):
            status = original(shape, path, **kwargs); file.write_bytes(b'new external edit'); return status
        with worker.certified_record(self.project.name) as record, patch.object(cq.Shape, 'exportStep', new=external_edit):
            with self.assertRaises(cad_io.CadIOError) as error:
                cad_io.export_geometry(record, file, 'STEP', overwrite=True)
        self.assertEqual(error.exception.code, 'OUTPUT_CHANGED')
        self.assertEqual(file.read_bytes(), b'new external edit')

    def test_final_existing_output_commit_preserves_actual_displaced_revision(self):
        original_exchange = workbench._exchange_files
        for mode in ('inplace-before', 'replace-before', 'fault-after', 'external-after'):
            output = self.base / f'late-{mode}.step'; output.write_bytes(b'initial selected revision')
            external = b'unseen external revision'
            def exchanged(directory, left, right):
                if mode == 'inplace-before': output.write_bytes(external)
                elif mode == 'replace-before':
                    revision = self.base / 'external-revision'; revision.write_bytes(external)
                    os.replace(revision, output)
                original_exchange(directory, left, right)
                if mode == 'fault-after': raise OSError('controlled post-syscall fault')
                if mode == 'external-after': output.write_bytes(external)
            with patch.object(workbench, '_exchange_files', side_effect=exchanged):
                with self.assertRaises(cad_io.CadIOError) as error:
                    self.export(output.name, 'STEP', overwrite=True)
            self.assertEqual(error.exception.status, 409)
            details = error.exception.details
            self.assertIs(details['active_file_changed'], True)
            paths = details['recovery_paths']
            self.assertGreaterEqual(len(paths), 2)
            displaced = Path(paths[0]['path']).read_bytes()
            self.assertEqual(displaced, external if mode.endswith('before') else b'initial selected revision')
            self.assertEqual(paths[0]['sha256'], hashlib.sha256(displaced).hexdigest())
            prepared = Path(paths[1]['path']).read_bytes()
            self.assertTrue(prepared.startswith(b'ISO-10303-21'))
            self.assertEqual(paths[1]['sha256'], hashlib.sha256(prepared).hexdigest())
            if mode == 'external-after': self.assertEqual(output.read_bytes(), external)
            else: self.assertTrue(output.read_bytes().startswith(b'ISO-10303-21'))
        self.assertIs(worker.certificate_current(self.project), True, 'An output conflict must not block source certification')

    def test_unsupported_atomic_existing_output_has_no_replace_fallback(self):
        output = self.base / 'unsupported.step'; output.write_bytes(b'preserve original')
        unavailable = workbench.WorkbenchError(501, 'ATOMIC_SAVE_UNAVAILABLE', 'unsupported filesystem')
        with patch.object(workbench, '_exchange_files', side_effect=unavailable), patch.object(cad_io.os, 'replace') as replaced:
            with self.assertRaises(cad_io.CadIOError) as error: self.export(output.name, 'STEP', overwrite=True)
        self.assertEqual(error.exception.status, 501)
        self.assertEqual(error.exception.code, 'ATOMIC_SAVE_UNAVAILABLE')
        self.assertEqual(output.read_bytes(), b'preserve original')
        replaced.assert_not_called()
        self.assertTrue(all(Path(r['path']).exists() for r in error.exception.details['recovery_paths']))

    def test_new_output_appearing_at_final_commit_is_never_clobbered(self):
        output = self.base / 'race.step'; original = os.link
        def raced(source, destination, **kwargs):
            Path(destination).write_bytes(b'another user file')
            return original(source, destination, **kwargs)
        with patch.object(cad_io.os, 'link', side_effect=raced):
            with self.assertRaises(cad_io.CadIOError) as error: self.export(output.name, 'STEP')
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(output.read_bytes(), b'another user file')

    def test_real_encoding_corruption_cannot_replace_output(self):
        output = self.base / 'encoded.step'; output.write_bytes(b'keep')
        original = cq.Shape.exportStep
        def corrupt(shape, path, **kwargs):
            status = original(shape, path, **kwargs); Path(path).write_bytes(b'not a STEP document'); return status
        with patch.object(cq.Shape, 'exportStep', new=corrupt):
            with self.assertRaises(cad_io.CadIOError): self.export(output.name, 'STEP', overwrite=True)
        self.assertEqual(output.read_bytes(), b'keep')

    def test_binary_stl_quantization_bounds_and_truncation_fail_closed(self):
        result = self.export('mesh.stl', 'STL', parts=['Cap'])
        data = bytearray(Path(result['path']).read_bytes())
        with self.assertRaises(cad_io.CadIOError): cad_io._stl_proof(data[:-1])
        struct.pack_into('<I', data, 80, 0)
        with self.assertRaises(cad_io.CadIOError): cad_io._stl_proof(data)
        with worker.certified_record(self.project.name) as record:
            record['variants']['neutral']['Cap'] = record['variants']['neutral']['Cap'].translate((1e9,1e9,1e9))
            with self.assertRaises(cad_io.CadIOError):
                cad_io.export_geometry(record, self.base / 'collapsed.stl', 'STL', parts=['Cap'])
        self.assertFalse((self.base / 'collapsed.stl').exists())

    def test_import_bad_type_empty_shape_path_size_and_existing_folder(self):
        good = Path(self.export('good.step', 'STEP')['path'])
        bad = self.base / 'bad.step'; bad.write_bytes(b'not step')
        empty = self.base / 'empty.step'; empty.write_bytes(b'')
        oversized = self.base / 'large.step'
        with oversized.open('wb') as file: file.truncate(cad_io.MAX_BYTES + 1)
        wrong = self.base / 'wrong.txt'; wrong.write_bytes(good.read_bytes())
        linked = self.base / 'linked.step'; linked.symlink_to(good)
        for source in (bad, empty, oversized, wrong, linked):
            with self.subTest(source=source.name), self.assertRaises(cad_io.CadIOError):
                cad_io.import_step(source, self.base, 'Rejected')
            self.assertFalse((self.base / 'Rejected').exists())
        existing = self.base / 'Existing'; existing.mkdir(); (existing / 'keep').write_bytes(b'keep')
        with self.assertRaises(cad_io.CadIOError): cad_io.import_step(good, self.base, 'Existing')
        self.assertEqual((existing / 'keep').read_bytes(), b'keep')
        for name in ('../escape', 'bad\nname', '.', 'bad/name'):
            with self.assertRaises(cad_io.CadIOError): cad_io.import_step(good, self.base, name)

    def test_nonsolid_step_is_rejected_with_no_created_project(self):
        wire = cq.Workplane('XY').rect(4,5).wire().val(); file = self.base / 'wire.step'
        with cad_io._step_settings(): wire.exportStep(str(file), unit='MM', outputUnit='MM')
        with self.assertRaises(cad_io.CadIOError): cad_io.import_step(file, self.base, 'Wire')
        self.assertFalse((self.base / 'Wire').exists())

    def test_invalid_selection_format_or_watched_output_has_no_side_effect(self):
        with worker.certified_record(self.project.name) as record:
            for variant, parts in [('missing',None), ('neutral',[]), ('neutral',['missing']), ('neutral',['Cap','Cap'])]:
                with self.subTest(variant=variant, parts=parts), self.assertRaises(cad_io.CadIOError):
                    cad_io.export_geometry(record, self.base / 'bad.step', 'STEP', variant, parts)
            for output, fmt in [(self.root / 'source.step','STEP'), (self.base / 'wrong.txt','STEP'), (self.base / 'bad.obj','OBJ'), (self.base / 'bad\npath.step','STEP')]:
                with self.assertRaises(cad_io.CadIOError): cad_io.export_geometry(record, output, fmt)
        self.assertFalse((self.base / 'bad.step').exists())

    def test_certified_source_assets_are_not_dropped_and_zip_metadata_is_relative(self):
        assets = self.root / 'assets'; assets.mkdir()
        with zipfile.ZipFile(assets / 'source-data.zip', 'w') as archive:
            archive.writestr('data.json', '{"public":true}')
        (assets / 'mesh-input.stl').write_bytes(b'public source asset, not an exported worker payload')
        self.certify()
        with worker.certified_record(self.project.name) as record:
            record['source_files'] = [{**item, 'untrusted_extra_root': str(self.root)} for item in record['source_files']]
            receipt = cad_io.export_geometry(record, self.base / 'sources.zip', 'ZIP')
        with zipfile.ZipFile(receipt['path']) as archive:
            for name in ('assets/source-data.zip', 'assets/mesh-input.stl', '__init__.py'):
                self.assertEqual(archive.read(name), (self.root / name).read_bytes())
            self.assertNotIn(str(self.root).encode(), archive.read('cassquery-export.json'))

    def test_source_zip_rejects_symlink_escape_excluded_and_changed_bytes(self):
        outside = self.base / 'outside.py'; outside.write_bytes(b'public fixture')
        linked = self.root / 'linked.py'; linked.symlink_to(outside)
        with worker.certified_record(self.project.name) as record:
            for name in ('linked.py','../outside.py','payloads/private.json'):
                candidate = dict(record)
                candidate['source_files'] = [*record['source_files'], {'path':name,'bytes':len(outside.read_bytes()),'sha256':hashlib.sha256(outside.read_bytes()).hexdigest()}]
                with self.subTest(name=name), self.assertRaises(cad_io.CadIOError):
                    cad_io.export_geometry(candidate, self.base / 'unsafe.zip', 'ZIP')
        self.assertFalse((self.base / 'unsafe.zip').exists())


if __name__ == '__main__':
    unittest.main()
