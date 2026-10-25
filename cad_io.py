"""Certified CAD I/O. Worker callers hold their shared GEOMETRY_LOCK throughout.

STEP is boundary geometry, not recovered parametric history. Export consumes the
retained nominal-mm shapes; it never executes a project's Python a second time.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import tempfile
import unicodedata
import zipfile

import cadquery as cq
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_Reader

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 2048
MAX_TRIANGLES = 300_000
GATES = {'units', 'watertight', 'volume', 'thin-extent', 'triangulation',
         'payload-roundtrip', 'budget-tris', 'budget-bytes'}
EXCLUDED = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'payloads',
            'evidence', 'phase0', 'dist', 'build', '.cassquery', 'BUILD-LOG.md'}
FORMATS = {'STEP': 'STEP', 'STP': 'STEP', 'STL': 'STL', 'BREP': 'BREP',
           'ZIP': 'ZIP', 'PY': 'PY', 'PYTHON': 'PY'}
EXTENSIONS = {'STEP': {'.step', '.stp'}, 'STL': {'.stl'}, 'BREP': {'.brep'},
              'ZIP': {'.zip'}, 'PY': {'.py'}}


class CadIOError(ValueError):
    def __init__(self, message, code='CAD_IO_ERROR', status=400, details=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = details or {}


def require(condition, message, code='CAD_IO_ERROR', status=400):
    if not condition:
        raise CadIOError(message, code, status)


def _text(value):
    require(isinstance(value, str) and value and len(value) <= 4096
            and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            'Invalid text or path', 'INVALID_PATH')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise CadIOError('Path is not valid UTF-8', 'INVALID_PATH') from error
    return value


def _path(value):
    try:
        text = _text(os.fspath(value))
    except TypeError as error:
        raise CadIOError('A filesystem path is required', 'INVALID_PATH') from error
    path = Path(text)
    require(path.is_absolute() and '..' not in path.parts, 'Select an absolute, non-traversing path', 'INVALID_PATH')
    require(not path.is_symlink(), 'Symlink paths are not supported', 'INVALID_PATH')
    return path.resolve()


def _relative(value):
    text = _text(value)
    path = PurePosixPath(text)
    require(not path.is_absolute() and '\\' not in text and ':' not in text
            and all(p not in ('', '.', '..') for p in text.split('/')),
            'Source path escapes the project', 'INVALID_PATH')
    require(not any(p in EXCLUDED for p in path.parts) and path.suffix.lower() not in ('.pyc', '.pyo'),
            'Certified source inventory contains excluded output/cache files', 'INVALID_SOURCE')
    return path


def _key(value):
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold())


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_file(path, limit=MAX_BYTES, *, allow_empty=False):
    require(path.parent.resolve() == path.parent, 'File parent was redirected', 'INVALID_PATH')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise CadIOError('Cannot read selected file', 'INVALID_SOURCE') from error
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and (0 <= before.st_size if allow_empty else 0 < before.st_size) and before.st_size <= limit,
                'Missing, nonregular, empty, or oversized input', 'INVALID_SOURCE')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(len(data) <= limit and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            'Input changed while reading', 'SOURCE_CHANGED', 409)
    current = path.lstat()
    require(not stat.S_ISLNK(current.st_mode) and (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
            'Input was replaced while reading', 'SOURCE_CHANGED', 409)
    return data


@contextmanager
def _step_settings():
    # Initialize the STEP registry before saving existing user settings. The
    # worker's shared geometry lock also covers imports before projects exist.
    STEPControl_Reader()
    strings = {k: Interface_Static.CVal_s(k) for k in ('xstep.cascade.unit', 'write.step.unit')}
    integers = {k: Interface_Static.IVal_s(k) for k in ('write.surfacecurve.mode', 'write.precision.mode')}
    try:
        yield
    finally:
        for key, value in strings.items():
            Interface_Static.SetCVal_s(key, value)
        for key, value in integers.items():
            Interface_Static.SetIVal_s(key, value)


def _solid_shape(shape):
    require(isinstance(shape, cq.Shape) and shape.isValid(), 'Invalid BREP geometry', 'INVALID_GEOMETRY')
    kind = shape.ShapeType()
    if kind in ('Compound', 'CompSolid'):
        children = list(shape)
        require(children, 'Empty BREP compound', 'INVALID_GEOMETRY')
        for child in children:
            _solid_shape(child)
    else:
        require(kind == 'Solid', 'Only solid STEP geometry is supported', 'INVALID_GEOMETRY')
    solids = shape.Solids()
    require(solids and all(math.isfinite(s.Volume()) and s.Volume() > 0 for s in solids),
            'Geometry contains no positive-volume solids', 'INVALID_GEOMETRY')
    return shape


def _read_step(path):
    try:
        with _step_settings():
            imported = cq.importers.importStep(str(path), unit='MM')
    except (ValueError, RuntimeError) as error:
        raise CadIOError('STEP file could not be loaded', 'IMPORT_FAILED') from error
    shapes = imported.vals()
    require(shapes, 'STEP contains no shapes', 'INVALID_GEOMETRY')
    return _solid_shape(cq.Compound.makeCompound(shapes))


def _summary(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape.wrapped, box, False, False)
    coordinates = box.Get()
    low, high = list(coordinates[:3]), list(coordinates[3:])
    volume = shape.Volume()
    require(all(math.isfinite(x) for x in low + high) and math.isfinite(volume) and volume > 0,
            'Non-finite or empty geometry', 'INVALID_GEOMETRY')
    return {'bounds': {'min': low, 'max': high, 'extent': [b - a for a, b in zip(low, high)]},
            'volume_mm3': volume, 'solids': len(shape.Solids())}


def _same_geometry(expected, measured, *, mesh=False):
    tolerance = 0.03 if mesh else max(1e-6, max(expected['bounds']['extent']) * 1e-8)
    require(all(abs(a - b) <= tolerance for name in ('min', 'max')
                for a, b in zip(expected['bounds'][name], measured['bounds'][name])),
            'Export roundtrip changed nominal-mm bounds', 'EXPORT_ROUNDTRIP')
    require(math.isclose(expected['volume_mm3'], measured['volume_mm3'], rel_tol=0.01 if mesh else 1e-6, abs_tol=1e-6),
            'Export roundtrip changed volume', 'EXPORT_ROUNDTRIP')
    if not mesh:
        require(expected['solids'] == measured['solids'], 'Export lost solids', 'EXPORT_ROUNDTRIP')


def _stl_proof(data):
    require(len(data) >= 84, 'Truncated binary STL', 'EXPORT_ROUNDTRIP')
    count = struct.unpack_from('<I', data, 80)[0]
    require(0 < count <= MAX_TRIANGLES and len(data) == 84 + count * 50,
            'Invalid or excessive STL triangle inventory', 'EXPORT_ROUNDTRIP')
    vertices, edges, volumes = {}, {}, []
    low, high, origin = [math.inf] * 3, [-math.inf] * 3, None
    for offset in range(84, len(data), 50):
        row = struct.unpack_from('<12fH', data, offset)
        require(all(math.isfinite(v) for v in row[:12]), 'Non-finite STL encoding', 'EXPORT_ROUNDTRIP')
        points = [tuple(row[i:i + 3]) for i in (3, 6, 9)]
        if origin is None:
            origin = points[0]
        ids = []
        for point in points:
            for axis in range(3):
                low[axis], high[axis] = min(low[axis], point[axis]), max(high[axis], point[axis])
            key = tuple(round(v / 1e-4) for v in point)
            ids.append(vertices.setdefault(key, len(vertices)))
        require(len(set(ids)) == 3, 'STL quantization collapsed a triangle', 'EXPORT_ROUNDTRIP')
        a, b, c = [tuple(v - o for v, o in zip(point, origin)) for point in points]
        cross = (b[1] * c[2] - b[2] * c[1], b[2] * c[0] - b[0] * c[2], b[0] * c[1] - b[1] * c[0])
        volumes.append(sum(x * y for x, y in zip(a, cross)) / 6)
        for x, y in zip(ids, ids[1:] + ids[:1]):
            edge = (min(x, y), max(x, y))
            total, winding = edges.get(edge, (0, 0))
            edges[edge] = (total + 1, winding + (1 if x < y else -1))
    require(edges and all(total == 2 and winding == 0 for total, winding in edges.values()),
            'Quantized STL is not a closed oriented mesh', 'EXPORT_ROUNDTRIP')
    volume = math.fsum(volumes)
    require(math.isfinite(volume) and volume > 0, 'STL has no positive enclosed volume', 'EXPORT_ROUNDTRIP')
    return {'bounds': {'min': low, 'max': high, 'extent': [b - a for a, b in zip(low, high)]},
            'volume_mm3': volume, 'triangles': count, 'watertight': True}


def _source_files(record):
    files = record.get('source_files')
    require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, 'Missing bounded certified source inventory', 'INVALID_SOURCE')
    root = _path(record['root'])
    result, seen, total = [], set(), 0
    for item in files:
        require(isinstance(item, dict), 'Invalid source record', 'INVALID_SOURCE')
        relative = _relative(item.get('path'))
        key = _key(relative.as_posix())
        require(key not in seen, 'Duplicate normalized source path', 'INVALID_SOURCE')
        seen.add(key)
        require(type(item.get('bytes')) is int and 0 <= item['bytes'] <= MAX_BYTES, 'Invalid source size', 'INVALID_SOURCE')
        total += item['bytes']
        require(total <= MAX_BYTES, 'Project inputs exceed 128 MiB', 'LIMIT_EXCEEDED')
        path = root.joinpath(*relative.parts)
        cursor = path
        while cursor != root:
            require(not cursor.is_symlink(), 'Project source symlink is forbidden', 'INVALID_PATH')
            cursor = cursor.parent
        require(path.resolve().is_relative_to(root), 'Source escapes project', 'INVALID_PATH')
        result.append((relative.as_posix(), path, item))
    return sorted(result)


def _source_bytes(path, item):
    data = _read_file(path, item['bytes'], allow_empty=True)
    require(len(data) == item['bytes'] and _sha(data) == item.get('sha256'),
            'Certified source bytes changed', 'SOURCE_CHANGED', 409)
    return data


def _outside_app(path):
    require(not any(p.suffix.lower() == '.app' for p in (path, *path.parents)),
            'Application bundle resources are read-only', 'INVALID_PATH')


def _output_revision(directory, name):
    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    result = {'bytes': metadata.st_size, 'sha256': None,
              'identity': (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns),
              'kind': 'regular' if stat.S_ISREG(metadata.st_mode) else 'nonregular'}
    if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_BYTES:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as file:
            before = os.fstat(file.fileno()); data = file.read(MAX_BYTES + 1); after = os.fstat(file.fileno())
        if len(data) <= MAX_BYTES and (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns):
            result['sha256'] = _sha(data)
    return result


def _atomic_output(output, overwrite, guard, writer):
    output = _path(output)
    _outside_app(output)
    parent = output.parent
    require(parent.is_dir() and parent.resolve() == parent, 'Select an existing stable output folder', 'INVALID_PATH')
    require(type(overwrite) is bool, 'Overwrite confirmation must be boolean', 'INVALID_PATH')
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent_identity = os.fstat(directory)
    temporary, keep, data = None, False, None
    def check_parent():
        current = parent.lstat()
        require(stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (parent_identity.st_dev, parent_identity.st_ino)
                and parent.resolve() == parent, 'Output folder was redirected', 'INVALID_PATH')
    def state():
        check_parent()
        try:
            revision = _output_revision(directory, output.name)
        except FileNotFoundError:
            return None
        require(revision['kind'] == 'regular' and revision['sha256'] is not None,
                'Output must be a bounded regular file', 'INVALID_PATH')
        return revision
    try:
        original = state()
        def destination():
            current = state()
            require(current == original, 'Output changed during export', 'OUTPUT_CHANGED', 409)
            require(current is None or overwrite, 'Output exists; explicit overwrite confirmation required', 'OUTPUT_EXISTS', 409)
        destination()
        fd, path = tempfile.mkstemp(prefix='.cassquery-export-', suffix=output.suffix, dir=parent)
        os.close(fd); temporary = Path(path)
        guard(); destination()
        proof = writer(temporary)
        data = _read_file(temporary)
        with temporary.open('rb') as stream: os.fsync(stream.fileno())
        guard(); destination()
        require(_read_file(temporary) == data, 'Staged export changed before publication', 'EXPORT_CHANGED', 409)
        if original is not None:
            # Reuse the qualified descriptor-relative source revision primitive.
            # Keep the entry BEFORE attempting exchange: even an exception after
            # the syscall must never delete the actually displaced revision.
            from workbench import _exchange_files
            keep = True
            _exchange_files(directory, temporary.name, output.name)
            displaced = _output_revision(directory, temporary.name)
            require(displaced == original, 'Output revision changed at commit', 'OUTPUT_CHANGED', 409)
            active = _output_revision(directory, output.name)
            require(active['sha256'] == _sha(data) and active['bytes'] == len(data),
                    'Active output changed after commit', 'OUTPUT_CHANGED', 409)
            guard()
            keep = False
        else:
            try:
                os.link(temporary, output, follow_symlinks=False)
            except FileExistsError as error:
                raise CadIOError('Output appeared during export', 'OUTPUT_EXISTS', 409) from error
            temporary.unlink(); temporary = None
        return {'path': str(output), 'bytes': len(data), 'sha256': _sha(data), 'roundtrip': proof}
    except Exception as error:
        if keep:
            # Never blind-restore over another revision. Expose the preserved
            # displaced entry and an independent copy of the prepared CAD bytes.
            recovery = [{'path': str(temporary), 'bytes': None, 'sha256': None}]
            try:
                revision = _output_revision(directory, temporary.name)
                recovery[0].update({k: revision[k] for k in ('bytes', 'sha256', 'kind')})
            except OSError:
                pass  # Exchange outcome is unknown; preserve its entry regardless.
            if data is not None:
                try:
                    check_parent()
                    fd, prepared = tempfile.mkstemp(prefix='.cassquery-export-prepared-', suffix=output.suffix, dir=parent)
                    with os.fdopen(fd, 'wb') as file:
                        file.write(data); file.flush(); os.fsync(file.fileno())
                    recovery.append({'path': prepared, 'bytes': len(data), 'sha256': _sha(data)})
                except (OSError, CadIOError):
                    pass  # The displaced entry is still retained if storage fails.
            details = {'active_file_changed': True, 'output_path': str(output), 'recovery_paths': recovery}
            status = getattr(error, 'status', 409)
            code = getattr(error, 'code', 'OUTPUT_CHANGED')
            raise CadIOError(f'Export commit needs reconciliation; preserved revision at {temporary}', code, status, details) from error
        if isinstance(error, OSError):
            raise CadIOError('Cannot atomically write selected export', 'IO_ERROR') from error
        raise
    finally:
        if temporary is not None and not keep:
            try:
                os.unlink(temporary.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def export_geometry(record, path, format, variant='neutral', parts=None, overwrite=False):
    """Export a retained current certificate under the worker's geometry lock."""
    require(isinstance(record, dict) and callable(record.get('assert_current')), 'Current certificate required', 'STALE_CERTIFICATE', 409)
    guard = record['assert_current']; guard()
    gates = record.get('gates', {})
    require(isinstance(gates, dict) and isinstance(gates.get('checks'), list)
            and all(isinstance(c, dict) for c in gates['checks']), 'Invalid certificate gates', 'STALE_CERTIFICATE', 409)
    checks = gates['checks']
    require(record.get('units') == 'mm' and gates.get('pass') is True and len(checks) == len(GATES)
            and {c.get('id') for c in checks} == GATES and all(c.get('status') == 'pass' for c in checks),
            'Export requires all certified nominal-mm gates', 'STALE_CERTIFICATE', 409)
    require(isinstance(record.get('hash'), str) and record['hash'] and type(record.get('built_at')) in (int, float)
            and math.isfinite(record['built_at']) and record['built_at'] > 0, 'Missing certificate provenance', 'STALE_CERTIFICATE', 409)
    require(isinstance(record.get('source_fingerprint'), str) and len(record['source_fingerprint']) == 64,
            'Missing certified source fingerprint', 'STALE_CERTIFICATE', 409)
    fmt = FORMATS.get(_text(format).upper())
    require(fmt, 'Unsupported export format', 'UNSUPPORTED_FORMAT')
    output = _path(path)
    require(output.suffix.lower() in EXTENSIONS[fmt], 'Output extension does not match selected format', 'UNSUPPORTED_FORMAT')
    root = _path(record['root'])
    require(not output.is_relative_to(root), 'Choose an export destination outside the watched project', 'INVALID_PATH')
    variants = record.get('variants', {})
    require(isinstance(variant, str) and variant in variants and isinstance(variants[variant], dict), 'Certified variant is missing', 'INVALID_SELECTION')
    available = variants[variant]
    selected = sorted(available) if parts is None else parts
    require(isinstance(selected, list) and selected and all(isinstance(n, str) and n in available for n in selected)
            and len(set(selected)) == len(selected), 'Select existing unique certified parts', 'INVALID_SELECTION')
    shape = _solid_shape(cq.Compound.makeCompound([_solid_shape(available[n]).copy(mesh=False) for n in selected]))
    summary = _summary(shape)
    provenance = {'project': record['project'], 'format': fmt, 'variant': variant, 'parts': selected,
                  'units': 'mm', 'certified_hash': record['hash'], 'built_at': record['built_at'],
                  'source_fingerprint': record['source_fingerprint'], **summary}
    if fmt in ('ZIP', 'PY'):
        sources = _source_files(record)
        def writer(temporary):
            if fmt == 'PY':
                entry = _relative(record['manifest']['entry'].split(':', 1)[0]).as_posix()
                source = next((s for s in sources if s[0] == entry), None)
                require(source, 'Certified entry source is missing', 'INVALID_SOURCE')
                temporary.write_bytes(_source_bytes(source[1], source[2]))
                return {'scope': 'entry-source-only', 'dependency_paths': [s[0] for s in sources if s[0] != entry],
                        'warning': 'Python export contains the entry only; use project ZIP for other project files.'}
            require(_key('cassquery-export.json') not in {_key(s[0]) for s in sources}, 'Source conflicts with ZIP provenance name', 'INVALID_SOURCE')
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, source, item in sources:
                    archive.writestr(relative, _source_bytes(source, item))
                archive.writestr('cassquery-export.json', _json({**provenance, 'source_files':
                    [{key: s[2][key] for key in ('path', 'bytes', 'sha256')} for s in sources]}))
            with zipfile.ZipFile(temporary) as archive:
                require(archive.testzip() is None, 'Project ZIP CRC failure', 'EXPORT_ROUNDTRIP')
                for relative, _, item in sources:
                    require(_sha(archive.read(relative)) == item['sha256'], 'Project ZIP lost source bytes', 'EXPORT_ROUNDTRIP')
            return {'scope': 'certified-project-source', 'files': len(sources), 'bytes': sum(s[2]['bytes'] for s in sources)}
    else:
        def writer(temporary):
            if fmt == 'STEP':
                with _step_settings():
                    require(shape.exportStep(str(temporary), unit='MM', outputUnit='MM') == IFSelect_RetDone, 'STEP exporter failed', 'EXPORT_FAILED')
                decoded = _summary(_read_step(temporary))
            elif fmt == 'BREP':
                require(shape.exportBrep(str(temporary)) is True, 'BREP exporter failed', 'EXPORT_FAILED')
                decoded = _summary(_solid_shape(cq.Shape.importBrep(str(temporary))))
            else:
                require(shape.exportStl(str(temporary), tolerance=0.01, angularTolerance=0.05, ascii=False, relative=False, parallel=False) is True,
                        'STL exporter failed', 'EXPORT_FAILED')
                decoded = _stl_proof(_read_file(temporary))
            _same_geometry(summary, decoded, mesh=fmt == 'STL')
            return decoded
    receipt = _atomic_output(output, overwrite, guard, writer)
    return {**provenance, **receipt}


def import_step(path, parent, name):
    """Copy a STEP asset into a new editable project; never overwrite originals."""
    source, parent = _path(path), _path(parent)
    require(source.suffix.lower() in ('.step', '.stp'), 'Only STEP/STP import is supported', 'UNSUPPORTED_FORMAT')
    _outside_app(parent)
    require(parent.is_dir(), 'Select an existing project parent', 'INVALID_PATH')
    name = _text(name)
    require(name not in ('.', '..') and Path(name).name == name and '/' not in name and '\\' not in name and ':' not in name,
            'Project name must be one safe folder name', 'INVALID_PATH')
    target = parent / name
    require(not os.path.lexists(target), 'Project folder already exists', 'OUTPUT_EXISTS', 409)
    data = _read_file(source)
    with tempfile.TemporaryDirectory(prefix='.cassquery-import-', dir=parent) as staging:
        stage = Path(staging); asset = stage / 'imported.step'; asset.write_bytes(data)
        shape = _read_step(asset); summary = _summary(shape)
        require(parent.resolve() == parent, 'Selected parent was redirected', 'INVALID_PATH')
        # Exclusive mkdir reserves a new project. No rename-over-empty-directory
        # shortcut may silently replace another project created during import.
        try:
            target.mkdir(mode=0o700)
        except FileExistsError as error:
            raise CadIOError('Project folder appeared during import', 'OUTPUT_EXISTS', 409) from error
        created = []
        try:
            assets = target / 'assets'; assets.mkdir(mode=0o700)
            files = {'assets/imported.step': data,
                     'viewer.json': _json({'name': name, 'entry': 'part_entry.py:build', 'live': True, 'unit_scale': 1.0,
                                          'steps': [{'id': 'assembled', 'label': 'Imported', 'variant': 'neutral', 'explode': 0, 'xray': False}]}),
                     'part_entry.py': b'''"""Imported STEP boundary geometry; edit this build entry to add features."""
from pathlib import Path
import cadquery as cq


def build():
    asset = Path(__file__).resolve().parent / "assets" / "imported.step"
    imported = cq.importers.importStep(str(asset), unit="MM")
    shape = cq.Compound.makeCompound(imported.vals())
    if not shape.isValid() or not shape.Solids():
        raise ValueError("Imported asset contains invalid or empty geometry")
    return {"names": ["Imported"], "parts": {"Imported": shape},
            "groups": {"ALL": ["Imported"]}, "axes": {}}
'''}
            for relative, content in files.items():
                require(target.resolve() == target, 'New project was redirected', 'INVALID_PATH')
                destination = target / relative
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                metadata = os.fstat(fd)
                created.append((destination, metadata.st_dev, metadata.st_ino))
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno())
            source_files = [{'path': n, 'bytes': len(b), 'sha256': _sha(b)} for n, b in sorted(files.items())]
            fingerprint = _sha(_json({'files': source_files}))
            return {'project': name, 'root': str(target), 'entry': 'part_entry.py:build', 'files': source_files,
                    'receipt': {'format': 'STEP', 'units': 'mm', 'source_name': source.name, 'source_bytes': len(data),
                                'source_sha256': _sha(data), 'source_fingerprint': fingerprint, 'certified': False, **summary}}
        except BaseException:
            if target.resolve() == target:
                for destination, device, inode in reversed(created):
                    try:
                        metadata = destination.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (device, inode):
                        destination.unlink()
                try:
                    (target / 'assets').rmdir(); target.rmdir()
                except OSError:
                    pass  # Unknown concurrently added files are never recursively removed.
            raise
