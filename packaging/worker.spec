# Build on the target architecture: native OCP wheels are not universal.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
binaries, datas, hidden = [], [], []
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

a = Analysis([str(root / 'worker.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=['IPython', 'pytest', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='cassquery-worker', console=True)
coll = COLLECT(exe, a.binaries, a.datas, name='cassquery-worker')
