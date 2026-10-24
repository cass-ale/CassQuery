#!/usr/bin/env python3
"""Reproduce Counterform assets from frozen vectors, offline; --check never writes.

Only Pillow 13.0.0 is needed for raster exports. Fonts, CAD, Qt, Electron,
iconutil, network access, and private project payloads are not build inputs.
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import math
from fractions import Fraction
from pathlib import Path
import struct
import sys
import zlib
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "branding/source/counterform.json"
PILLOW_VERSION = "13.0.0"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
ICONSET = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def load_source():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    if source["schema"] != 1 or source["name"] != "Counterform":
        raise ValueError("Unsupported brand source")
    return source


def number(value):
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def point_string(points):
    return " ".join(f"{number(x)},{number(y)}" for x, y in points)


def circle(center, radius, start=0, end=360, steps=144):
    return [(center[0] + radius * math.cos(math.radians(start + (end-start)*i/steps)),
             center[1] + radius * math.sin(math.radians(start + (end-start)*i/steps)))
            for i in range(steps+1)]


def polygon(points, fill):
    return {"contours": [points], "fill": fill}


def transform(points, x, y, scale):
    return [(x + a*scale, y + b*scale) for a, b in points]


def mark(source, x=0, y=0, scale=1, color=None, accent=True, optical=False):
    spec, palette = source["mark"], source["palette"]
    color = color or palette["bone"]
    inner = spec["inner_radius"] - (2 if optical else 0)
    mouth = spec["mouth_degrees"]
    ring = circle(spec["center"], spec["outer_radius"], mouth, 360-mouth)
    ring += circle(spec["center"], inner, 360-mouth, mouth)
    start, end, width = spec["terminal_start"], spec["terminal_end"], spec["terminal_width"]
    dx, dy = end[0]-start[0], end[1]-start[1]
    length = math.hypot(dx, dy)
    nx, ny = -dy/length*width/2, dx/length*width/2
    band = [(start[0]+nx,start[1]+ny), (end[0]+nx,end[1]+ny),
            (end[0]-nx,end[1]-ny), (start[0]-nx,start[1]-ny)]
    shapes = [polygon(transform(ring,x,y,scale),color), polygon(transform(band,x,y,scale),color)]
    if accent and not optical:
        cut = (end[0]-dx*spec["accent_fraction"], end[1]-dy*spec["accent_fraction"])
        tip = [(cut[0]+nx,cut[1]+ny),band[1],band[2],(cut[0]-nx,cut[1]-ny)]
        shapes.append(polygon(transform(tip,x,y,scale),palette["coral"]))
    return shapes


def outlined(spec, x, y, width, fill):
    left, top, right, _ = spec["bounds"]
    scale = width/(right-left)
    tx, ty = x-left*scale, y-top*scale
    return {"contours": [transform(c,tx,ty,scale) for c in spec["contours"]],
            "fill": fill, "path": spec["path"], "matrix": [scale,0,0,scale,tx,ty]}


def wordmark(source, x, y, width, fill):
    return outlined(source["wordmark"],x,y,width,fill)


def plate(source):
    field, inset, exponent = (source["plate"][key] for key in ("field","inset","exponent"))
    radius = field/2-inset
    points = []
    for i in range(256):
        angle = i*math.tau/256
        c, s = math.cos(angle), math.sin(angle)
        points.append((field/2 + radius*math.copysign(abs(c)**(2/exponent),c),
                       field/2 + radius*math.copysign(abs(s)**(2/exponent),s)))
    return polygon(points, source["palette"]["ink"])


def icon_scene(source, size):
    # Optical masters preserve the counter and terminal at 16/32 px.
    scale = 8.8 if size <= 32 else 8
    return [plate(source)] + mark(source,512-48*scale,500-48*scale,scale,
                                 accent=size>32,optical=size<=32)


def fixture_shapes(source, x, y, scale, dark):
    """Exact TESTPLATE silhouettes, projected at 30 degrees; display gaps only."""
    palette = source["palette"]
    face = palette["bone"] if dark else palette["panel"]
    side = palette["mist"]
    bore = palette["ink"] if dark else palette["ivory"]
    project = lambda a,b,z: (x+scale*math.cos(math.pi/6)*(a-b), y+scale*((a+b)/2-z))
    spec = source["fixture"]
    width, depth, thick = spec["plate_mm"]
    def slab(w,d,z,h):
        corners = [(-w/2,-d/2),(w/2,-d/2),(w/2,d/2),(-w/2,d/2)]
        top = [project(a,b,z+h) for a,b in corners]
        low = [project(a,b,z) for a,b in corners]
        return [polygon([top[1],top[2],low[2],low[1]],side),
                polygon([top[2],top[3],low[3],low[2]],side), polygon(top,face)]
    shapes = slab(width,depth,0,thick)
    for hx,hy in spec["holes_mm"]:
        shapes.append(polygon([project(a,b,thick) for a,b in circle((hx,hy),spec["hole_diameter_mm"]/2)],bore))
    diameter, height = spec["post_mm"]
    post_z = thick+10  # 10 mm display separation; not changed fixture geometry.
    lower = [project(a,b,post_z) for a,b in circle((0,0),diameter/2,-45,135)]
    upper = [project(a,b,post_z+height) for a,b in circle((0,0),diameter/2,135,-45)]
    shapes.append(polygon(lower+upper,side))
    shapes.append(polygon([project(a,b,post_z+height) for a,b in circle((0,0),diameter/2)],face))
    cw, cd, ch = spec["cap_mm"]
    shapes += slab(cw,cd,post_z+height+10,ch)
    return shapes


def banner_scene(source, dark=True):
    palette = source["palette"]
    ink = palette["bone"] if dark else palette["ink"]
    shapes = mark(source,76,174,1.65,color=ink)
    shapes.append(wordmark(source,265,206,586,ink))
    shapes += fixture_shapes(source,1198,416,6,dark)
    return shapes


def svg(scene, width, height, title, background=None):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             f'<title id="title">{escape(title)}</title>',
             '<desc id="desc">CassQuery Counterform: an open geometric C with a measured Q incision. Supporting geometry, where present, is the public TESTPLATE fixture.</desc>']
    if background:
        parts.append(f'<rect width="{width}" height="{height}" fill="{background}"/>')
    for shape in scene:
        if "path" in shape:
            matrix = " ".join(number(v) for v in shape["matrix"])
            parts.append(f'<path fill="{shape["fill"]}" d="{shape["path"]}" transform="matrix({matrix})"/>')
        else:
            paths = " ".join("M"+" L".join(f"{number(a)} {number(b)}" for a,b in contour)+" Z" for contour in shape["contours"])
            parts.append(f'<path fill="{shape["fill"]}" d="{paths}"/>')
    parts.append('</svg>')
    return ("\n".join(parts)+"\n").encode()


def pillow():
    try:
        import PIL
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError("Raster generation needs Pillow; install branding/requirements.txt in a scratch venv") from error
    if PIL.__version__ != PILLOW_VERSION:
        raise RuntimeError(f"Deterministic raster generation needs Pillow {PILLOW_VERSION}, got {PIL.__version__}")
    return Image, ImageDraw


def area(points):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1]))/2


def quantized_contours(shape):
    return [[(float(number(a)),float(number(b))) for a,b in contour]
            for contour in shape["contours"]]


def grid_contours(shape,viewbox,size,sample=4):
    """Map the serialized decimal coordinate to integer coverage pixels exactly."""
    def coordinate(value,axis):
        text = number(value)
        negative = text.startswith("-")
        whole,_,fraction = text.lstrip("-").partition(".")
        units = int(whole)*10000+int(fraction.ljust(4,"0") or "0")
        if negative:
            units = -units
        return units*sample*size[axis]//(viewbox[axis]*10000)
    return [[(coordinate(a,0),coordinate(b,1)) for a,b in contour]
            for contour in shape["contours"]]


def fill_polygon(mask,points,fill):
    """Exact even-odd pixel-centre scan conversion; no platform C polygon math."""
    edges = []
    for a,b in zip(points,points[1:]+points[:1]):
        if a[1] == b[1]:
            continue
        if a[1] > b[1]:
            a,b = b,a
        edges.append((a,b))
    if not edges:
        return
    first = max(0,min(a[1] for a,b in edges))
    last = min(mask.height,max(b[1] for a,b in edges))
    for y in range(first,last):
        hits = []
        for a,b in edges:
            if a[1] <= y < b[1]:
                dy,dx = b[1]-a[1],b[0]-a[0]
                hits.append(Fraction(2*a[0]*dy+(2*y+1-2*a[1])*dx,2*dy))
        hits.sort()
        if len(hits)%2:
            raise ValueError("Malformed polygon scanline")
        for left,right in zip(hits[::2],hits[1::2]):
            start = max(0,math.ceil(left-Fraction(1,2)))
            stop = min(mask.width,math.ceil(right-Fraction(1,2)))
            if stop > start:
                mask.paste(fill,(start,y,stop,y+1))


def raster(scene, viewbox, size, background=None):
    Image, ImageDraw = pillow()
    # Four-times coverage sampling. All outlines are frozen and font-free.
    sample = 4
    image = Image.new("RGBA",(size[0]*sample,size[1]*sample),background or (0,0,0,0))
    for shape in scene:
        # No platform floating scan conversion: serialize then scale rationally
        # to the integer coverage grid before passing points to Pillow.
        contours = grid_contours(shape,viewbox,size,sample)
        points = [p for contour in contours for p in contour]
        left = max(0,min(p[0] for p in points)-1)
        top = max(0,min(p[1] for p in points)-1)
        right = min(image.width,max(p[0] for p in points)+2)
        bottom = min(image.height,max(p[1] for p in points)+2)
        if right <= left or bottom <= top:
            continue
        mask = Image.new("L",(right-left,bottom-top),0)
        if len(contours) == 1:
            fill_polygon(mask,[(a-left,b-top) for a,b in contours[0]],255)
        else:
            # Frozen Iowan outlines: positive winding solid, negative winding counter.
            for contour in sorted(contours,key=lambda c: area(c)<0):
                fill_polygon(mask,[(a-left,b-top) for a,b in contour],255 if area(contour)>0 else 0)
        image.paste(shape["fill"],(left,top,right,bottom),mask)
    return resolve_coverage(image,size,sample)


def resolve_coverage(image,size,sample=4):
    """Integer-only 4×4 area resolve, including straight-alpha reconstruction.

    The supersampled scene has binary coverage (all painted fills opaque).
    Packed 16-bit lanes sum RGBA channels without overflow; Python's integer
    arithmetic is identical on ARM/x86. No architecture-dependent filter runs.
    """
    if sample != 4 or image.size != (size[0]*4,size[1]*4):
        raise ValueError("Coverage resolver requires a four-times RGBA image")
    Image, _ = pillow()
    source = image.tobytes()
    stride = image.width*4
    output = bytearray(size[0]*size[1]*4)
    target = 0
    for row in range(size[1]):
        packed = 0
        for dy in range(4):
            start = (row*4+dy)*stride
            lanes = bytearray(stride*2)
            lanes[::2] = source[start:start+stride]
            packed += int.from_bytes(lanes,"little")
        total = packed+(packed>>64)+(packed>>128)+(packed>>192)
        lanes = total.to_bytes(stride*2+2,"little")
        for column in range(size[0]):
            offset = column*32
            red = lanes[offset] | lanes[offset+1]<<8
            green = lanes[offset+2] | lanes[offset+3]<<8
            blue = lanes[offset+4] | lanes[offset+5]<<8
            alpha = lanes[offset+6] | lanes[offset+7]<<8
            if alpha:
                output[target] = (red*255+alpha//2)//alpha
                output[target+1] = (green*255+alpha//2)//alpha
                output[target+2] = (blue*255+alpha//2)//alpha
                output[target+3] = (alpha+8)//16
            target += 4
    return Image.frombytes("RGBA",size,bytes(output))


def png(image):
    """PNG with filter-zero scanlines and stored DEFLATE blocks, host-independent.

    No linked compression library chooses filters, packet boundaries, or bytes.
    zlib is used only for the specified Adler32 checksum, never compression.
    """
    image = image.convert("RGBA")
    pixels = image.tobytes()
    stride = image.width*4
    raw = b"".join(b"\0"+pixels[start:start+stride] for start in range(0,len(pixels),stride))
    deflate = bytearray(b"\x78\x01")
    for start in range(0,len(raw),65535):
        block = raw[start:start+65535]
        deflate.append(1 if start+len(block)==len(raw) else 0)
        deflate.extend(struct.pack("<HH",len(block),len(block)^0xffff))
        deflate.extend(block)
    deflate.extend(struct.pack(">I",zlib.adler32(raw)&0xffffffff))
    def chunk(kind,data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",binascii.crc32(kind+data)&0xffffffff)
    header = struct.pack(">IIBBBBB",image.width,image.height,8,6,0,0,0)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",header)+chunk(b"IDAT",bytes(deflate))+chunk(b"IEND",b"")


def ico(images):
    sizes = [s for s in ICON_SIZES if s<=256]
    offset = 6+16*len(sizes)
    entries, payload = [], []
    for size in sizes:
        data = images[size]
        entries.append(struct.pack("<BBBBHHII",size%256,size%256,0,0,1,32,len(data),offset))
        payload.append(data)
        offset += len(data)
    return struct.pack("<HHH",0,1,len(sizes))+b"".join(entries+payload)


def argb_rle(png_data):
    Image, _ = pillow()
    image = Image.open(io.BytesIO(png_data)).convert("RGBA").convert("RGBa")
    red, green, blue, alpha = image.split()
    payload = bytearray(b"ARGB")
    # ICNS per-channel RLE: 0..127 introduces 1..128 literal bytes.
    # Literal-only packets are simple, deterministic and negligible at 16/32 px.
    for channel in (alpha,red,green,blue):
        data = channel.tobytes()
        for start in range(0,len(data),128):
            packet = data[start:start+128]
            payload.append(len(packet)-1)
            payload.extend(packet)
    return bytes(payload)


def icns(images):
    chunks = []
    for code,size in ((b"ic04",16),(b"ic05",32),(b"ic07",128),
                      (b"ic08",256),(b"ic09",512),(b"ic10",1024),
                      (b"ic11",32),(b"ic12",64),(b"ic13",256),(b"ic14",512)):
        # Native macOS decodes icp4/icp5 PNG payloads incorrectly. Modern ic04/
        # ic05 use explicit ARGB RLE; verify against iconutil's actual pixels.
        data = argb_rle(images[size]) if code in (b"ic04",b"ic05") else images[size]
        chunks.append(code+struct.pack(">I",len(data)+8)+data)
    payload = b"".join(chunks)
    return b"icns"+struct.pack(">I",len(payload)+8)+payload


def lockup_scene(source, dark=True):
    palette = source["palette"]
    color = palette["bone"] if dark else palette["ink"]
    return mark(source,0,0,1.6,color=color)+[wordmark(source,184,32,700,color)]


def guide_scene(source):
    palette = source["palette"]
    labels = json.loads((ROOT/"branding/source/labels.json").read_text(encoding="utf-8"))
    label = lambda key,x,y,w: outlined(labels[key],x,y,w,palette["bone"])
    shapes = [label("title",68,65,610),label("philosophy",72,186,211)]
    shapes += mark(source,952,54,1.8)
    shapes.append(label("colour",72,292,82))
    for i,key in enumerate(("ink","panel","bone","coral","mist","ivory")):
        x = 72+i*179
        shapes.append(polygon([(x-.7,339.3),(x+150.7,339.3),(x+150.7,435.7),(x-.7,435.7)],palette["mist"]))
        shapes.append(polygon([(x,340),(x+150,340),(x+150,435),(x,435)],palette[key]))
        shapes.append(label(key,x,459,117))
    shapes += [label("typography",72,579,145),label("editorial",72,636,660),
               label("sans",75,752,452),label("mono",75,820,340)]
    shapes.append(label("construction",72,936,177))
    shapes += mark(source,73,991,2.5,accent=False)
    # Fine construction rings and axes live only in the guide, never the icon.
    cx,cy,r = 73+48*2.5,991+48*2.5,32*2.5
    shapes.append({"contours":[circle((cx,cy),r),circle((cx,cy),r-0.6)[::-1]],"fill":palette["mist"]})
    shapes.append(polygon([(cx-102,cy-.3),(cx+102,cy-.3),(cx+102,cy+.3),(cx-102,cy+.3)],palette["mist"]))
    shapes.append(polygon([(cx-.3,cy-102),(cx+.3,cy-102),(cx+.3,cy+102),(cx-.3,cy+102)],palette["mist"]))
    shapes += fixture_shapes(source,858,1252,4.6,True)
    return shapes


def generated_assets(source):
    palette = source["palette"]
    outputs = {}
    guide = guide_scene(source)
    outputs["branding/style-guide.svg"] = svg(guide,1200,1360,"CassQuery Counterform style guide",palette["ink"])
    outputs["branding/style-guide.png"] = png(raster(guide,(1200,1360),(1200,1360),palette["ink"]))
    for name,color,accent in (("mark",palette["bone"],True),("mark-dark",palette["ink"],True),
                              ("mark-mono",palette["bone"],False),("mark-mono-dark",palette["ink"],False)):
        outputs[f"branding/{name}.svg"] = svg(mark(source,color=color,accent=accent),100,100,"CassQuery Counterform mark")
    outputs["viewer/brand.svg"] = outputs["branding/mark-mono.svg"]
    outputs["assets/icons/cassquery.svg"] = svg(icon_scene(source,1024),1024,1024,"CassQuery application icon")
    for dark in (True,False):
        theme = "dark" if dark else "light"
        color = palette["bone"] if dark else palette["ink"]
        lockup = lockup_scene(source,dark)
        outputs[f"branding/wordmark-{theme}.svg"] = svg(lockup,920,200,"CassQuery wordmark")
        background = palette["ink"] if dark else palette["ivory"]
        scene = banner_scene(source,dark)
        outputs[f"branding/readme-{theme}.svg"] = svg(scene,1600,560,"CassQuery — public TESTPLATE geometry",background)
        outputs[f"branding/readme-{theme}.png"] = png(raster(scene,(1600,560),(1600,560),background))
    # Match the usual 1200 × 630 open-graph ratio without distorting the master.
    social = []
    social += mark(source,72,184,1.8)
    social.append(wordmark(source,74,362,613,palette["bone"]))
    social += fixture_shapes(source,914,455,5.3,True)
    outputs["branding/social-preview.svg"] = svg(social,1200,630,"CassQuery social preview",palette["ink"])
    outputs["branding/social-preview.png"] = png(raster(social,(1200,630),(1200,630),palette["ink"]))
    images = {size:png(raster(icon_scene(source,size),(1024,1024),(size,size))) for size in ICON_SIZES}
    outputs.update({f"assets/icons/cassquery-{size}.png":data for size,data in images.items()})
    outputs.update({f"assets/icons/cassquery.iconset/{name}":images[size] for name,size in ICONSET.items()})
    outputs["assets/icons/cassquery.ico"] = ico(images)
    outputs["assets/icons/cassquery.icns"] = icns(images)
    # Favicon is the actual open mark, not a microscopic app plate inside a tab.
    favicon_images = {size:png(raster(mark(source,color=palette["bone"],accent=False,optical=size<=32),
                                        (100,100),(size,size),palette["ink"])) for size in ICON_SIZES if size<=256}
    outputs["assets/icons/favicon.svg"] = svg(mark(source,accent=False,optical=True),100,100,"CassQuery favicon",palette["ink"])
    outputs["assets/icons/favicon.ico"] = ico(favicon_images)
    outputs["assets/icons/favicon-16.png"] = favicon_images[16]
    outputs["assets/icons/favicon-32.png"] = favicon_images[32]
    manifest = {"schema":1,"brand":"Counterform","source":"branding/source/counterform.json",
                "source_sha256":hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                "labels_source":"branding/source/labels.json",
                "labels_sha256":hashlib.sha256((ROOT/"branding/source/labels.json").read_bytes()).hexdigest(),
                "generator":"tools/make_brand_assets.py","rasterizer":f"Pillow {PILLOW_VERSION}, 4x supersampling",
                "assets":[{"path":path,"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
                          for path,data in sorted(outputs.items())]}
    outputs["assets/brand-manifest.json"] = (json.dumps(manifest,indent=2)+"\n").encode()
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check",action="store_true",help="Compare every generated byte; do not modify files")
    args = parser.parse_args(argv)
    try:
        outputs = generated_assets(load_source())
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2,f"Brand generation failed: {error}\n")
    if args.check:
        mismatches = [name for name,data in outputs.items() if not (ROOT/name).is_file() or (ROOT/name).read_bytes()!=data]
        if mismatches:
            print("Missing or stale brand assets:\n"+"\n".join(mismatches),file=sys.stderr)
            return 1
        print(f"Counterform: {len(outputs)} outputs match byte-for-byte.")
        return 0
    for name,data in outputs.items():
        path = ROOT/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
    print(f"Counterform: wrote {len(outputs)} outputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
