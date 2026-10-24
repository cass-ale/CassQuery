"""Counterform regressions: source, semantic layout, containers and drift detection."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("make_brand_assets", ROOT/"tools/make_brand_assets.py")
brand = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(brand)
try:
    import PIL
    HAS_RASTER = PIL.__version__ == brand.PILLOW_VERSION
except ImportError:
    HAS_RASTER = False


class BrandSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = brand.load_source()
        self.manifest = json.loads((ROOT/"assets/brand-manifest.json").read_text())

    def test_manifest_hashes_every_asset_and_sources(self):
        paths = set()
        for item in self.manifest["assets"]:
            path = ROOT/item["path"]
            self.assertNotIn(item["path"],paths)
            paths.add(item["path"])
            self.assertEqual(path.stat().st_size,item["bytes"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),item["sha256"])
        self.assertEqual(hashlib.sha256(brand.SOURCE.read_bytes()).hexdigest(),self.manifest["source_sha256"])
        self.assertEqual(hashlib.sha256((ROOT/self.manifest["labels_source"]).read_bytes()).hexdigest(),self.manifest["labels_sha256"])
        required = {"viewer/brand.svg","branding/readme-dark.svg","branding/readme-light.svg",
                    "branding/wordmark-dark.svg","branding/wordmark-light.svg",
                    "branding/style-guide.svg","branding/social-preview.png",
                    "assets/icons/cassquery.icns","assets/icons/cassquery.ico",
                    "assets/icons/favicon.ico","assets/icons/cassquery-1024.png"}
        self.assertLessEqual(required,paths)

    def test_svg_is_accessible_outlined_and_self_contained(self):
        for item in self.manifest["assets"]:
            if not item["path"].endswith(".svg"):
                continue
            with self.subTest(path=item["path"]):
                root = ElementTree.parse(ROOT/item["path"]).getroot()
                tags = {node.tag.rsplit("}",1)[-1] for node in root.iter()}
                self.assertEqual(root.attrib["role"],"img")
                self.assertTrue({"title","desc","path"}.issubset(tags))
                self.assertFalse(tags.intersection({"text","image","script","foreignObject","style"}))
                self.assertFalse(any("href" in key for node in root.iter() for key in node.attrib))
                allowed = set(self.source["palette"].values())
                for node in root.iter():
                    if "fill" in node.attrib:
                        self.assertIn(node.attrib["fill"],allowed)

    def test_original_cq_math_and_optical_cut(self):
        spec = self.source["mark"]
        self.assertEqual((spec["outer_radius"],spec["inner_radius"]),(32,20))
        self.assertEqual(spec["mouth_degrees"],40)
        normal = brand.mark(self.source)
        small = brand.mark(self.source,optical=True)
        self.assertEqual(len(normal),3)
        self.assertEqual(len(small),2)
        self.assertEqual(brand.icon_scene(self.source,16)[-1]["fill"],self.source["palette"]["bone"])
        self.assertNotIn(self.source["palette"]["coral"],[s["fill"] for s in brand.icon_scene(self.source,32)])
        self.assertEqual((ROOT/"viewer/brand.svg").read_bytes(),(ROOT/"branding/mark-mono.svg").read_bytes())

    def test_testplate_uses_public_fixture_geometry(self):
        spec = self.source["fixture"]
        self.assertEqual(spec["source"],"testpart/part_entry.py")
        self.assertEqual(spec["plate_mm"],[40,30,6])
        self.assertEqual(spec["holes_mm"],[[15,10],[15,-10],[-15,10],[-15,-10]])
        self.assertEqual(spec["hole_diameter_mm"],5)
        self.assertEqual(spec["post_mm"],[8,22])
        self.assertEqual(spec["cap_mm"],[16,16,4])
        # Compare source syntax directly; importing it would require CAD at branding time.
        import ast
        tree = ast.parse((ROOT/spec["source"]).read_text())
        calls = [(node.func.attr,[ast.literal_eval(arg) for arg in node.args])
                 for node in ast.walk(tree) if isinstance(node,ast.Call)
                 and isinstance(node.func,ast.Attribute)
                 and node.func.attr in {"box","hole","circle","extrude","rect"}]
        for call in (("box",[40,30,6]),("hole",[5]),("circle",[4]),("extrude",[22]),("rect",[16,16]),("extrude",[4])):
            self.assertIn(call,calls)

    def test_layout_bounds_and_wordmark_clearance(self):
        for dark in (True,False):
            scene = brand.banner_scene(self.source,dark)
            for shape in scene:
                points = [p for contour in shape["contours"] for p in contour]
                self.assertGreaterEqual(min(p[0] for p in points),24)
                self.assertLessEqual(max(p[0] for p in points),1576)
                self.assertGreaterEqual(min(p[1] for p in points),24)
                self.assertLessEqual(max(p[1] for p in points),536)
            word = scene[3]
            geometry = scene[4:]
            right = max(p[0] for c in word["contours"] for p in c)
            left = min(p[0] for s in geometry for c in s["contours"] for p in c)
            self.assertGreater(left-right,100)

    def test_cylinder_silhouette_reaches_projected_ellipse_extrema(self):
        shapes = brand.fixture_shapes(self.source,1198,416,6,True)
        shaft,top = shapes[7],shapes[8]
        xmin = lambda shape: min(p[0] for c in shape["contours"] for p in c)
        xmax = lambda shape: max(p[0] for c in shape["contours"] for p in c)
        self.assertAlmostEqual(xmin(shaft),xmin(top),places=6)
        self.assertAlmostEqual(xmax(shaft),xmax(top),places=6)
        import math
        self.assertAlmostEqual(xmax(shaft)-xmin(shaft),8*6*math.sqrt(1.5),places=6)

    def test_lockup_has_safe_descender_margins(self):
        for dark in (True,False):
            scene = brand.lockup_scene(self.source,dark)
            points = [p for shape in scene for contour in shape["contours"] for p in contour]
            self.assertGreaterEqual(min(p[0] for p in points),20)
            self.assertGreaterEqual(min(p[1] for p in points),20)
            self.assertLessEqual(max(p[0] for p in points),900)
            self.assertLessEqual(max(p[1] for p in points),180)
            mark_right = max(p[0] for s in scene[:3] for c in s["contours"] for p in c)
            word_left = min(p[0] for c in scene[3]["contours"] for p in c)
            self.assertGreaterEqual(word_left-mark_right,40)

    def test_png_dimensions_iconset_and_ico_headers(self):
        for size in brand.ICON_SIZES:
            data = (ROOT/f"assets/icons/cassquery-{size}.png").read_bytes()
            self.assertEqual(data[:8],b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II",data[16:24]),(size,size))
            self.assertEqual(data[25],6)  # RGBA, transparent outside Mac plate.
        for name,size in brand.ICONSET.items():
            self.assertEqual((ROOT/f"assets/icons/cassquery.iconset/{name}").read_bytes(),
                             (ROOT/f"assets/icons/cassquery-{size}.png").read_bytes())
        for name in ("cassquery","favicon"):
            data = (ROOT/f"assets/icons/{name}.ico").read_bytes()
            reserved,kind,count = struct.unpack("<HHH",data[:6])
            self.assertEqual((reserved,kind,count),(0,1,7))
            offset = 6+count*16
            for i in range(count):
                w,h,_,_,planes,depth,length,start = struct.unpack("<BBBBHHII",data[6+i*16:22+i*16])
                self.assertEqual((planes,depth,start),(1,32,offset))
                self.assertEqual(data[start:start+8],b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II",data[start+16:start+24]),(w or 256,h or 256))
                offset += length
            self.assertEqual(offset,len(data))

    def test_icns_all_native_retina_entries_have_correct_pngs(self):
        data = (ROOT/"assets/icons/cassquery.icns").read_bytes()
        self.assertEqual(data[:4],b"icns")
        self.assertEqual(struct.unpack(">I",data[4:8])[0],len(data))
        offset,found = 8,{}
        while offset<len(data):
            code = data[offset:offset+4]
            length = struct.unpack(">I",data[offset+4:offset+8])[0]
            png = data[offset+8:offset+length]
            if code in (b"ic04",b"ic05"):
                found[code] = 16 if code == b"ic04" else 32
                self.assertEqual(png[:4],b"ARGB")
                self.assertEqual(len(png),4+4*(found[code]**2+found[code]**2//128))
            else:
                found[code]=struct.unpack(">II",png[16:24])[0]
                self.assertEqual(png,(ROOT/f"assets/icons/cassquery-{found[code]}.png").read_bytes())
            offset+=length
        self.assertEqual(offset,len(data))
        self.assertEqual(found,{b"ic04":16,b"ic05":32,b"ic07":128,b"ic08":256,b"ic09":512,b"ic10":1024,
                                b"ic11":32,b"ic12":64,b"ic13":256,b"ic14":512})

    @unittest.skipUnless(HAS_RASTER and sys.platform == "darwin" and shutil.which("iconutil"),
                         "Native Mac pixel roundtrip needs iconutil and pinned Pillow")
    def test_native_mac_icon_pixels_not_merely_container_acceptance(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            decoded = Path(directory)/"decoded.iconset"
            subprocess.run(["iconutil","--convert","iconset",str(ROOT/"assets/icons/cassquery.icns"),
                            "--output",str(decoded)],check=True,capture_output=True,timeout=30)
            for name,size in brand.ICONSET.items():
                actual = Image.open(decoded/name).convert("RGBA")
                expected = Image.open(ROOT/f"assets/icons/cassquery-{size}.png").convert("RGBA")
                self.assertEqual(actual.size,expected.size,name)
                self.assertEqual(actual.getchannel("A").tobytes(),expected.getchannel("A").tobytes(),name)
                self.assertEqual(actual.convert("RGBa").tobytes(),expected.convert("RGBa").tobytes(),name)
                if size >= 64 or "@2x" in name:
                    self.assertEqual(actual.tobytes(),expected.tobytes(),name)

    @unittest.skipUnless(HAS_RASTER,"Integer coverage oracle uses pinned Pillow image buffers")
    def test_integer_coverage_resolver_matches_explicit_pixel_oracle(self):
        from PIL import Image
        image = Image.new("RGBA",(8,4),(0,0,0,0))
        samples = [(238,232,219,255)]*7+[(17,19,22,255)]*3+[(0,0,0,0)]*6
        for i,pixel in enumerate(samples):
            image.putpixel((i%4,i//4),pixel)
        for y in range(4):
            for x in range(4,8):
                image.putpixel((x,y),(240,128,114,255))
        actual = brand.resolve_coverage(image,(2,1))
        sums = [sum(p[channel] for p in samples) for channel in range(4)]
        expected = tuple((sums[c]*255+sums[3]//2)//sums[3] for c in range(3))+((sums[3]+8)//16,)
        self.assertEqual(actual.getpixel((0,0)),expected)
        self.assertEqual(actual.getpixel((1,0)),(240,128,114,255))
        with self.assertRaises(ValueError):
            brand.resolve_coverage(image,(2,1),2)

    @unittest.skipUnless(HAS_RASTER,"Exact polygon oracle uses pinned Pillow mask buffers")
    def test_exact_polygon_scanline_matches_independent_point_in_polygon(self):
        from PIL import Image
        from fractions import Fraction
        cases=[[(1,1),(6,1),(6,6),(1,6)],[(1,1),(6,6),(1,6)],
               [(-2,2),(5,-2),(8,6),(1,7)],[(1,1),(6,1),(6,3),(3,3),(3,6),(1,6)]]
        for points in cases:
            mask=Image.new("L",(8,8),0)
            brand.fill_polygon(mask,points,255)
            for y in range(8):
                for x in range(8):
                    px,py=Fraction(2*x+1,2),Fraction(2*y+1,2)
                    inside=False
                    for a,b in zip(points,points[1:]+points[:1]):
                        if (a[1]>py)!=(b[1]>py):
                            crossing=Fraction(a[0])+Fraction((py-a[1])*(b[0]-a[0]),b[1]-a[1])
                            if px<crossing:
                                inside=not inside
                    self.assertEqual(mask.getpixel((x,y)),255 if inside else 0,(points,x,y))
        mask=Image.new("L",(8,8),0)
        brand.fill_polygon(mask,[(0,0),(8,0),(8,8),(0,8)],255)
        brand.fill_polygon(mask,[(2,2),(6,2),(6,6),(2,6)],0)
        self.assertEqual(sum(mask.tobytes()),(64-16)*255)

    def test_grid_mapping_uses_exact_decimal_rationals_and_negative_floor(self):
        shape={"contours":[[(1.12345,2.5),(-0.0001,-1.0001),(0.9999,3.0)]]}
        self.assertEqual(brand.grid_contours(shape,(100,100),(16,32)),[[(0,3),(-1,-2),(0,3)]])
        self.assertEqual(brand.grid_contours(shape,(100,100),(100,100)),[[(4,10),(-1,-5),(3,12)]])
        for item in brand.fixture_shapes(self.source,1198,416,6,True):
            self.assertTrue(all(isinstance(v,int) for c in brand.grid_contours(item,(1600,560),(1600,560)) for p in c for v in p))

    def test_raster_coordinates_match_svg_four_decimal_contract(self):
        shape = {"contours":[[(1.000000000000001,2.1234567),(3.999999999999999,4.000000000000001)]]}
        self.assertEqual(brand.quantized_contours(shape),[[(1.0,2.1235),(4.0,4.0)]])
        for item in brand.fixture_shapes(self.source,1198,416,6,True):
            actual = brand.quantized_contours(item)
            expected = [[(float(brand.number(a)),float(brand.number(b))) for a,b in c] for c in item["contours"]]
            self.assertEqual(actual,expected)
            for contour in actual:
                for x,y in contour:
                    self.assertEqual(x,round(x,4))
                    self.assertEqual(y,round(y,4))

    def test_png_serializer_is_fixed_scanline_stored_deflate(self):
        # Independent tiny-image protocol oracle, not a Pillow save roundtrip.
        class TinyImage:
            width,height = 2,1
            def convert(self,mode):
                self.assert_mode = mode
                return self
            def tobytes(self):
                return bytes((17,19,22,255,238,232,219,128))
        image=TinyImage()
        data=brand.png(image)
        self.assertEqual(image.assert_mode,"RGBA")
        offset,parts = 8,{}
        import binascii
        import zlib
        while offset<len(data):
            length=struct.unpack(">I",data[offset:offset+4])[0]
            kind=data[offset+4:offset+8]
            payload=data[offset+8:offset+8+length]
            crc=struct.unpack(">I",data[offset+8+length:offset+12+length])[0]
            self.assertEqual(crc,binascii.crc32(kind+payload)&0xffffffff)
            parts[kind]=payload
            offset+=length+12
        self.assertEqual(offset,len(data))
        self.assertEqual(zlib.decompress(parts[b"IDAT"]),b"\0"+image.tobytes())
        self.assertEqual(parts[b"IDAT"][:7],b"\x78\x01\x01\x09\x00\xf6\xff")

    def test_check_reports_missing_and_stale_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = {"branding/a.svg":b"fresh","assets/b.png":b"png"}
            with patch.object(brand,"ROOT",root),patch.object(brand,"generated_assets",return_value=outputs),patch.object(brand,"load_source",return_value={}):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(brand.main(["--check"]),1)
                self.assertIn("branding/a.svg",errors.getvalue())
                self.assertFalse((root/"branding").exists())
                (root/"branding").mkdir()
                (root/"assets").mkdir()
                (root/"branding/a.svg").write_bytes(b"stale")
                (root/"assets/b.png").write_bytes(b"png")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(brand.main(["--check"]),1)
                self.assertEqual((root/"branding/a.svg").read_bytes(),b"stale")
                (root/"branding/a.svg").write_bytes(b"fresh")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(brand.main(["--check"]),0)

    @unittest.skipUnless(HAS_RASTER,"Exact Pillow 13.0.0 needed for full byte regeneration; stdlib asset checks still run")
    def test_full_regeneration_matches_checked_in_assets(self):
        outputs=brand.generated_assets(self.source)
        for name,data in outputs.items():
            with self.subTest(path=name):
                self.assertEqual((ROOT/name).read_bytes(),data)


if __name__ == "__main__":
    unittest.main()
