# Build on the target architecture: native OCP wheels are not universal.
from pathlib import Path
import json
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
sources = json.loads((root / 'package.json').read_text())['cassquerySources']['python']
assert isinstance(sources, list) and 0 < len(sources) <= 32 and sources[0] == 'worker.py'
assert len(sources) == len(set(sources)) and all(isinstance(name, str) and Path(name).name == name
    and Path(name).suffix == '.py' and (root / name).is_file() and not (root / name).is_symlink() for name in sources)
binaries, datas = [], []
# Explicit lazy local-module closure: these helpers must execute from the PYZ.
hidden = [Path(name).stem for name in sources if name != 'worker.py']
for package in ('cadquery', 'OCP', 'cadquery_ocp_proxy', 'websockets'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h
# SciPy 1.18 vendors array_api_compat under _external. PyInstaller 6.20's
# scipy hook still names _lib; this current package dynamically imports .fft.
# Keep the exact relocated import explicit rather than collecting all of SciPy.
hidden.append('scipy._external.array_api_compat.numpy.fft')
for folder in ('viewer', 'testpart'):
    datas.append((str(root / folder), folder))
# Source evidence copies are not on the frozen import path; compiled helpers stay in PYZ.
for name in sources:
    datas.append((str(root / name), 'source'))

a = Analysis([str(root / 'worker.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=['IPython', 'pytest', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='cassquery-worker', console=True)
coll = COLLECT(exe, a.binaries, a.datas, name='cassquery-worker')
