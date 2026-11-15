#!/usr/bin/env python3
"""
GitHub profil README görsellerini icmsamet.com kaynaklarından üretir.

Kaynak, sitenin reposu (icmsametWebSite). Hero metni, rakamlar, yorumlar,
işler, fontlar, maskot ve imza oradan okunur; README'deki görseller siteyle
aynı kalır. Sitede içerik değişince bu betiği yeniden çalıştır.

    python3 -m pip install fonttools brotli    # bir kez
    brew install webp                          # cwebp: düşünen maskot kareleri
    python3 tools/build-assets.py --site ../icmsametWebSite

Çıktı assets/ altına yazılır. SVG'ler kendi içinde tamdır: font ve görseller
gömülüdür, çünkü README'de <img> ile açılan SVG dışarıdan dosya yükleyemez.
Fontlar yalnızca kullanılan harflerle alt kümelenir, dosyalar küçük kalır.
Animasyonlar CSS ile yazılır ve prefers-reduced-motion açıksa hiç çalışmaz;
o durumda her şey son hâlinde, durağan görünür.
"""
import argparse
import base64
import html
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
except ImportError:
    sys.exit("fontTools gerekli: python3 -m pip install fonttools brotli")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

# Site tokenları: icmsametWebSite/public/assets/site.css :root ile aynı.
C = {
    "bg": "#FDF7F1", "bg2": "#F9EEE4", "card": "#FFFFFF",
    "ink": "#3E2D20", "ink2": "#6B5A4C",
    "bal": "#C69455", "bal2": "#D8AC72", "balDeep": "#A87A38",
    "balInk": "#875D1C", "balSoft": "#EBD3AE", "balTint": "#FBF2E4",
    "kum": "#C4A68B", "espSoft": "#E2D6C8", "line": "#EDE3D8", "onBal": "#1E1A17",
}
EASE = "cubic-bezier(.22,1,.36,1)"
EASE_S = "cubic-bezier(.4,0,.2,1)"
FONT_FILES = {"N": "nunito-var.woff2", "B": "baloo2-var.woff2"}  # N gövde, B başlık

SITE = None  # main() doldurur


# ---------------------------------------------------------------- site
class Site:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.assets = self.root / "public" / "assets"
        page = (self.root / "public" / "index.html").read_text("utf-8")
        m = re.search(r'id="icerik-gomulu">(.*?)</script>', page, re.S)
        if not m:
            sys.exit("index.html içinde gömülü içerik (#icerik-gomulu) bulunamadı")
        self.c = json.loads(m.group(1))

    def image(self, ref):
        """Site içi yol (/assets/...) ya da tam URL; ham baytları döner."""
        if ref.startswith("http"):
            with urllib.request.urlopen(ref, timeout=30) as r:
                return r.read()
        return (self.root / "public" / ref.lstrip("/")).read_bytes()


# ---------------------------------------------------------------- font
_inst = {}


def font(fam, wght):
    key = (fam, wght)
    if key not in _inst:
        f = TTFont(SITE.assets / FONT_FILES[fam])
        ax = next(a for a in f["fvar"].axes if a.axisTag == "wght")
        w = max(ax.minValue, min(ax.maxValue, wght))
        inst = instancer.instantiateVariableFont(f, {"wght": w})
        # Kaydederken head.modified güncellenmesin: içerik aynıysa çıktı da aynı kalsın,
        # yoksa her çalıştırma bütün SVG'leri "değişti" gösterir.
        inst.recalcTimestamp = False
        _inst[key] = inst
    return _inst[key]


def has_glyphs(s, fam, wght):
    cmap = font(fam, wght).getBestCmap()
    missing = sorted({ch for ch in s if ord(ch) not in cmap and not ch.isspace()})
    if missing:
        sys.exit(f"{fam}{wght} fontunda olmayan karakter: {missing!r} — metinde: {s!r}")


def measure(s, fam, wght, size, ls=0.0):
    f = font(fam, wght)
    cmap, hmtx, upm = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
    return sum(hmtx[cmap[ord(ch)]][0] for ch in s) * size / upm + ls * len(s)


def wrap(s, fam, wght, size, maxw):
    lines, cur = [], ""
    for word in s.split():
        t = f"{cur} {word}".strip()
        if cur and measure(t, fam, wght, size) > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def font_face(fam, wght, chars):
    buf = io.BytesIO()
    font(fam, wght).save(buf)
    buf.seek(0)
    f = TTFont(buf, recalcTimestamp=False)
    opt = subset.Options()
    opt.flavor = "woff2"
    sub = subset.Subsetter(opt)
    sub.populate(text="".join(sorted(chars)))
    sub.subset(f)
    out = io.BytesIO()
    f.flavor = "woff2"
    f.save(out)
    b64 = base64.b64encode(out.getvalue()).decode()
    return f'@font-face{{font-family:"{fam}{wght}";src:url(data:font/woff2;base64,{b64}) format("woff2")}}'


# ---------------------------------------------------------------- svg
def esc(s):
    return html.escape(s, quote=True)


def data_uri(b, mime="image/webp"):
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


def webp_size(b):
    kind = b[12:16]
    if kind == b"VP8X":
        return 1 + int.from_bytes(b[24:27], "little"), 1 + int.from_bytes(b[27:30], "little")
    if kind == b"VP8 ":
        return int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
    if kind == b"VP8L":
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    sys.exit("WebP boyutu okunamadı")


def png_to_webp(path, height):
    if not shutil.which("cwebp"):
        sys.exit("cwebp gerekli: brew install webp")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "o.webp"
        subprocess.run(["cwebp", "-quiet", "-q", "82", "-alpha_q", "90", "-resize", "0",
                        str(height), str(path), "-o", str(out)], check=True)
        return out.read_bytes()


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.title = w, h, title
        self.defs, self.css, self.body = [], [], []
        self.chars = defaultdict(set)

    def text(self, x, y, txt, fam, wght, size, fill, anchor="start", ls=0, extra=""):
        has_glyphs(txt, fam, wght)
        self.chars[(fam, wght)].update(txt)
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        l = f' letter-spacing="{ls}"' if ls else ""
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}{wght},sans-serif" '
                f'font-size="{size:.2f}" fill="{fill}"{a}{l}{extra}>{esc(txt)}</text>')

    def render(self):
        faces = "".join(font_face(f, w, ch) for (f, w), ch in sorted(self.chars.items()))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.title)}">'
                f'<title>{esc(self.title)}</title>'
                f'<style><![CDATA[{faces}{"".join(self.css)}]]></style>'
                f'<defs>{"".join(self.defs)}</defs>{"".join(self.body)}</svg>\n')


def motion(rules):
    """Animasyonlar yalnızca hareket tercihi kısıtlı değilse çalışır."""
    return "@media (prefers-reduced-motion:no-preference){" + rules + "}"


SHADOW = ('<filter id="sh" x="-20%" y="-30%" width="140%" height="190%">'
          '<feDropShadow dx="0" dy="1" stdDeviation="1" flood-color="#2a1a0c" flood-opacity=".06"/>'
          '<feDropShadow dx="0" dy="6" stdDeviation="7" flood-color="#2a1a0c" flood-opacity=".09"/>'
          '</filter>')

HONEY = ('<radialGradient id="honey" cx="32%" cy="22%" r="120%">'
         f'<stop offset="0" stop-color="{C["bal2"]}"/><stop offset=".56" stop-color="{C["bal"]}"/>'
         f'<stop offset="1" stop-color="{C["balDeep"]}"/></radialGradient>')


def blob_path(cx, cy, r, k=(1, .94, 1.05, .97, 1.03, .95, 1.02, .98)):
    """Düzensiz, yumuşak kapalı eğri (site hero'sundaki bal lekesi)."""
    n = len(k)
    p = [(cx + r * k[i] * math.cos(2 * math.pi * i / n - math.pi / 2),
          cy + r * k[i] * math.sin(2 * math.pi * i / n - math.pi / 2)) for i in range(n)]
    d = f"M{p[0][0]:.1f} {p[0][1]:.1f}"
    for i in range(n):
        p0, p1, p2, p3 = p[i - 1], p[i], p[(i + 1) % n], p[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]:.1f} {p2[1]:.1f}"
    return d + "Z"


def star_path(cx, cy, r):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * .46
        a = math.pi / 5 * i - math.pi / 2
        pts.append(f"{cx + rr * math.cos(a):.2f} {cy + rr * math.sin(a):.2f}")
    return "M" + "L".join(pts) + "Z"


def stars(svg, x, cy, size, rating, uid):
    """Beş yıldız; kesirli puan bal dolgunun genişliğiyle gösterilir."""
    gap = size * .22
    shapes = "".join(f'<path d="{star_path(x + size / 2 + i * (size + gap), cy, size / 2)}"/>'
                     for i in range(5))
    full = 5 * size + 4 * gap
    filled = x + full * rating / 5 if rating < 5 else x + full
    svg.defs.append(f'<clipPath id="st{uid}"><rect x="{x}" y="{cy - size}" '
                    f'width="{filled - x:.2f}" height="{size * 2}"/></clipPath>')
    return (f'<g fill="{C["balSoft"]}">{shapes}</g>'
            f'<g fill="{C["bal"]}" clip-path="url(#st{uid})">{shapes}</g>'), full


def fill_vars(s, v):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: v[m.group(1)], s)


def stats_vars():
    """content.js'teki sayacHesapla ile aynı hesap."""
    rs = SITE.c["yorumlar"]["ogeler"]
    per = []
    for r in rs:
        d = [float(r[k]) for k in ("iletisim", "hizmet", "zamanlama") if k in r]
        per.append(sum(d) / len(d) if d else 5)
    return {
        "yil": str(date.today().year - 2018),
        "yorum": str(len(rs)),
        "musteri": str(len({r["ad"] for r in rs})),
        "puan": f"{sum(per) / len(per):.2f}",
    }


# ---------------------------------------------------------------- hero
def hero(lang):
    c, v = SITE.c, stats_vars()
    W, H = 1200, 540
    X0, MAXW = 64, 600
    head = c["hero"]["baslik"][lang]
    s = Svg(W, H, f"Samet İçim — {head}")
    b = s.body

    # Başlık: site *yıldız* ile vurguluyor; yoksa virgülden bölüp ikinci satır bal.
    if "*" in head:
        pre, em, post = re.match(r"(.*?)\*(.+?)\*(.*)", head).groups()
        lines = [pre.strip(), (em + post).strip()]
    elif ", " in head:
        i = head.index(", ") + 1
        lines = [head[:i], head[i + 1:]]
    else:
        lines = [head, ""]
    hs = min(58, MAXW / max(measure(t, "B", 800, 1) for t in lines if t))

    s.defs += [
        f'<clipPath id="card"><rect width="{W}" height="{H}" rx="28"/></clipPath>',
        HONEY, SHADOW,
    ]

    # ---- sağ: yalnızca el sallayan maskot. Bal lekesi, kesikli halka ve araç
    # etiketleri sitede kaldırıldı, burada da yok.
    CX = 952
    b.append(f'<g clip-path="url(#card)"><rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    wav = SITE.image("/assets/maskot/poz-el-sallayan.webp")
    mw, mh = webp_size(wav)
    MH = 500
    MW = MH * mw / mh
    mx, my = CX - MW * .55, H - MH + 30  # 30px kart dışına taşar: süzülürken alt kenar açılmasın
    b.append(f'<g class="mr"><g class="fl"><g class="wv"><image href="{data_uri(wav)}" '
             f'x="{mx:.1f}" y="{my:.1f}" width="{MW:.1f}" height="{MH}"/></g></g></g>')

    # ---- sol: imza, üst başlık, başlık, açıklama, rakamlar
    d = re.search(r'\sd="([^"]+)"', (SITE.assets / "imza.svg").read_text("utf-8")).group(1)
    SW = 168
    SH = SW * 381 / 1044
    s.defs.append(f'<clipPath id="sigc"><rect class="sigw" x="{X0 - 12}" y="26" '
                  f'width="{SW + 24}" height="{SH + 24}"/></clipPath>')
    b.append(f'<g clip-path="url(#sigc)"><svg x="{X0 - 6}" y="36" width="{SW}" height="{SH:.1f}" '
             f'viewBox="0 0 1044 381"><path fill="{C["ink"]}" fill-rule="evenodd" d="{d}"/></svg></g>')

    y = 148
    b.append(f'<g class="r" style="--d:80ms"><rect x="{X0}" y="{y - 5}" width="22" height="2" rx="1" '
             f'fill="{C["kum"]}"/>' + s.text(X0 + 34, y, c["hero"]["ustBaslik"][lang], "N", 800, 12.5,
                                             C["balInk"], ls=2.2) + "</g>")

    y1 = y + 24 + hs * .86
    y2 = y1 + hs * 1.06
    w2 = measure(lines[1], "B", 800, hs)
    b.append(f'<g class="r" style="--d:160ms">' + s.text(X0 - 2, y1, lines[0], "B", 800, hs, C["ink"])
             + s.text(X0 - 2, y2, lines[1], "B", 800, hs, C["balInk"])
             + f'<path class="ul" pathLength="1" d="M{X0} {y2 + 13:.1f}Q{X0 + w2 / 2:.1f} {y2 + 19:.1f} '
               f'{X0 + w2 - 4:.1f} {y2 + 10:.1f}" fill="none" stroke="{C["bal"]}" stroke-width="5" '
               f'stroke-linecap="round"/></g>')

    lede = wrap(c["hero"]["aciklama"][lang], "N", 400, 17, MAXW)
    y = y2 + 56
    b.append('<g class="r" style="--d:240ms">'
             + "".join(s.text(X0, y + i * 27, t, "N", 400, 17, C["ink2"]) for i, t in enumerate(lede))
             + "</g>")

    top = y + (len(lede) - 1) * 27 + 34
    gap, ch = 14, 76
    cw = (MAXW - 2 * gap) / 3
    for i, r in enumerate(c["rakamlar"][:3]):
        x = X0 + i * (cw + gap)
        val, lab = fill_vars(r["deger"][lang], v), fill_vars(r["etiket"][lang], v)
        ls = min(13, 13 * (cw - 20) / measure(lab, "N", 700, 13))
        b.append(f'<g class="r" style="--d:{340 + i * 80}ms"><rect x="{x:.1f}" y="{top:.1f}" '
                 f'width="{cw:.1f}" height="{ch}" rx="18" fill="{C["card"]}" stroke="{C["line"]}" '
                 f'filter="url(#sh)"/>'
                 + s.text(x + cw / 2, top + 37, val, "B", 800, 25, C["balInk"], anchor="middle")
                 + s.text(x + cw / 2, top + 59, lab, "N", 700, ls, C["ink2"], anchor="middle")
                 + "</g>")
    if top + ch > H - 30:
        sys.exit(f"hero taşıyor: rakamlar {top + ch:.0f}px'te bitiyor, kart {H}px")

    b.append("</g>")
    b.append(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="28" fill="none" stroke="{C["line"]}"/>')

    s.css.append(
        f".wv{{transform-box:view-box;transform-origin:{CX}px {H + 20}px}}"
        + motion(
            f".r{{animation:rv .7s {EASE} both;animation-delay:var(--d,0ms)}}"
            f".mr{{animation:rise 1s {EASE} .25s both}}"
            ".fl{animation:fl 3.6s ease-in-out infinite alternate}"
            ".wv{animation:wv 6s ease-in-out 1.6s infinite}"
            f".sigw{{animation:yaz 1.6s {EASE_S} .15s both}}"
            f".ul{{stroke-dasharray:1 1;animation:ciz .9s {EASE_S} .8s both}}")
        + "@keyframes rv{from{opacity:0;transform:translateY(14px)}}"
          "@keyframes rise{from{opacity:0;transform:translateY(60px)}}"
          "@keyframes fl{to{transform:translateY(-8px)}}"
          "@keyframes wv{0%,58%,100%{transform:rotate(0)}64%{transform:rotate(-2.4deg)}"
          "70%{transform:rotate(2.2deg)}76%{transform:rotate(-1.8deg)}82%{transform:rotate(1.2deg)}"
          "88%{transform:rotate(0)}}"
          f"@keyframes yaz{{from{{transform:translateX(-{SW + 24}px)}}}}"
          "@keyframes ciz{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}")
    return s.render()


# ---------------------------------------------------------------- yorumlar
REVIEW_TEXT = {
    "en": {"summary": "{yorum} reviews from {musteri} clients on Bionluk",
           "dims": ("Communication", "Service", "Timing")},
    "tr": {"summary": "Bionluk'ta {musteri} müşteriden {yorum} değerlendirme",
           "dims": ("İletişim", "Hizmet", "Zamanlama")},
}


def reviews(lang):
    rs = [r for r in SITE.c["yorumlar"]["ogeler"] if r.get("yorum")]
    v = stats_vars()
    t = REVIEW_TEXT[lang]
    W, CW, CH, G = 1200, 340, 188, 16
    TOP = 104
    H = TOP + CH + 32
    s = Svg(W, H, f'{v["puan"]} / 5 — {t["summary"].format(**v)}')
    s.defs += [f'<clipPath id="card"><rect width="{W}" height="{H}" rx="28"/></clipPath>', SHADOW,
               f'<linearGradient id="fl"><stop offset="0" stop-color="{C["bg"]}"/>'
               f'<stop offset="1" stop-color="{C["bg"]}" stop-opacity="0"/></linearGradient>',
               f'<linearGradient id="fr"><stop offset="0" stop-color="{C["bg"]}" stop-opacity="0"/>'
               f'<stop offset="1" stop-color="{C["bg"]}"/></linearGradient>']
    b = s.body
    b.append(f'<g clip-path="url(#card)"><rect width="{W}" height="{H}" fill="{C["bg"]}"/>')

    # Özet satırı: ortalama, yıldız, kırılım
    nw = measure(v["puan"], "B", 800, 46)
    st, sw = stars(s, 40 + nw + 18, 44, 19, float(v["puan"]), "top")
    b.append(s.text(40, 70, v["puan"], "B", 800, 46, C["balInk"]) + st
             + s.text(40 + nw + 18, 76, t["summary"].format(**v), "N", 700, 14, C["ink2"]))
    dims = []
    for key, name in zip(("iletisim", "hizmet", "zamanlama"), t["dims"]):
        d = [float(r[key]) for r in rs if key in r]
        dims.append((name, f"{sum(d) / len(d):.2f}"))
    x = W - 40
    for name, val in reversed(dims):
        b.append(s.text(x, 64, val, "N", 800, 14, C["balInk"], anchor="end"))
        x -= measure(val, "N", 800, 14) + 6
        b.append(s.text(x, 64, name, "N", 700, 14, C["ink2"], anchor="end"))
        x -= measure(name, "N", 700, 14) + 22

    # Kayan şerit: aynı metin iki kez yazılmışsa bir kez göster
    seen, cards = set(), []
    for r in rs:
        txt = r["yorum"][lang] if isinstance(r["yorum"], dict) else r["yorum"]
        txt = " ".join(txt.split())
        if txt.lower() in seen:
            continue
        seen.add(txt.lower())
        d = [float(r[k]) for k in ("iletisim", "hizmet", "zamanlama") if k in r]
        cards.append((txt, sum(d) / len(d) if d else 5, r["ad"]))

    def card(i, x, txt, rating, ad):
        y = TOP
        lines = wrap(txt, "N", 600, 14.5, CW - 48)
        if len(lines) > 5:
            lines = lines[:5]
            lines[-1] = lines[-1].rstrip(".,;") + "…"
        st, _ = stars(s, x + 24, y + 30, 15, rating, i)
        name = "@" + (ad[:3] if len(ad) > 3 else ad[:1]) + "***"  # sitedeki adMaskele
        return (f'<rect x="{x}" y="{y}" width="{CW}" height="{CH}" rx="18" fill="{C["card"]}" '
                f'stroke="{C["line"]}" filter="url(#sh)"/>' + st
                + s.text(x + CW - 22, y + 50, "”", "B", 800, 56, C["balSoft"], anchor="end")
                + "".join(s.text(x + 24, y + 62 + j * 21.5, ln, "N", 600, 14.5, C["ink"])
                          for j, ln in enumerate(lines))
                + s.text(x + 24, y + CH - 20, name, "N", 800, 13, C["balInk"])
                + s.text(x + CW - 24, y + CH - 20, "Bionluk", "N", 700, 12, C["ink2"], anchor="end"))

    span = len(cards) * (CW + G)
    track = "".join(card(f"{k}{i}", 24 + k * span + i * (CW + G), *cd)
                    for k in (0, 1) for i, cd in enumerate(cards))
    b.append(f'<g class="ray">{track}</g>')
    b.append(f'<rect x="0" y="{TOP - 12}" width="72" height="{CH + 40}" fill="url(#fl)"/>'
             f'<rect x="{W - 72}" y="{TOP - 12}" width="72" height="{CH + 40}" fill="url(#fr)"/>')
    b.append("</g>")
    b.append(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="28" fill="none" stroke="{C["line"]}"/>')
    s.css.append(motion(f".ray{{animation:kay {span / 38:.0f}s linear infinite}}")
                 + f"@keyframes kay{{to{{transform:translateX(-{span}px)}}}}")
    return s.render()


# ---------------------------------------------------------------- düşünen maskot
def thinking():
    W, H = 320, 360
    s = Svg(W, H, "Samet'in maskotu düşünüyor, sonra aklına fikir geliyor")
    s.defs.append(HONEY)
    b = s.body
    cx = 128
    b.append(f'<circle cx="{cx}" cy="232" r="112" fill="url(#honey)" opacity=".9"/>')

    FH = 318
    # 3. kare daha geniş ve yüzü sağa dönük; kafa aynı yerde kalsın diye sola alınır.
    for i, dx in ((1, 0), (2, 0), (3, -14)):
        img = png_to_webp(SITE.root / "maskot" / f"maskot-dusunen-{i}.png", FH * 2)
        w, h = webp_size(img)
        fw = FH * w / h
        b.append(f'<image class="f f{i}" href="{data_uri(img)}" x="{cx - fw / 2 + dx:.1f}" '
                 f'y="{H - FH - 4}" width="{fw:.1f}" height="{FH}"/>')

    # Düşünce balonu: önce dış çizgi, sonra beyaz dolgu üstüne; iç kesişimler görünmez.
    puffs = [(236, 46, 20), (258, 32, 24), (284, 44, 21), (268, 60, 18), (244, 62, 16)]
    b.append('<g class="bal-on">'
             + f'<circle cx="199" cy="97" r="4.5" fill="#fff" stroke="{C["espSoft"]}" stroke-width="2"/>'
             + f'<circle cx="214" cy="77" r="7" fill="#fff" stroke="{C["espSoft"]}" stroke-width="2"/>'
             + "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff" stroke="{C["espSoft"]}" '
                       f'stroke-width="4"/>' for x, y, r in puffs)
             + "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#fff"/>' for x, y, r in puffs)
             + "</g>")
    b.append('<g class="dots">' + "".join(
        f'<circle class="d d{k}" cx="{246 + k * 15}" cy="47" r="4.5" fill="{C["balInk"]}"/>'
        for k in range(3)) + "</g>")
    # Ampul: fikir geldi
    bx, by = 261, 42
    rays = "".join(
        f'<path d="M{bx + 16 * math.cos(a):.1f} {by + 16 * math.sin(a):.1f}'
        f'L{bx + 21 * math.cos(a):.1f} {by + 21 * math.sin(a):.1f}"/>'
        for a in (math.radians(d) for d in (-150, -115, -90, -65, -30)))
    b.append(f'<g class="bulb"><circle cx="{bx}" cy="{by}" r="15" fill="#FBE3B0" opacity=".7"/>'
             f'<circle cx="{bx}" cy="{by}" r="9.5" fill="#F2B544"/>'
             f'<circle cx="{bx - 3}" cy="{by - 3}" r="2.6" fill="#fff" opacity=".8"/>'
             f'<rect x="{bx - 5}" y="{by + 8}" width="10" height="7" rx="2" fill="{C["ink"]}"/>'
             f'<g stroke="{C["balInk"]}" stroke-width="2.4" stroke-linecap="round">{rays}</g></g>')

    s.css.append(
        f".bulb{{transform-box:view-box;transform-origin:{bx}px {by}px;opacity:0}}"
        ".f2,.f3{opacity:0}"
        + motion(
            ".f1{animation:k1 9s infinite}.f2{animation:k2 9s infinite}.f3{animation:k3 9s infinite}"
            ".dots{animation:nok 9s infinite}"
            ".d{transform-box:view-box;animation:zip 1.1s ease-in-out infinite}"
            ".d1{animation-delay:.15s}.d2{animation-delay:.3s}"
            f".bulb{{animation:amp 9s {EASE} infinite}}")
        + "@keyframes k1{0%,31%{opacity:1}33%,98%{opacity:0}100%{opacity:1}}"
          "@keyframes k2{0%,31%{opacity:0}33%,64%{opacity:1}66%,100%{opacity:0}}"
          "@keyframes k3{0%,64%{opacity:0}66%,98%{opacity:1}100%{opacity:0}}"
          "@keyframes nok{0%,64%{opacity:1}67%,97%{opacity:0}100%{opacity:1}}"
          "@keyframes zip{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-5px)}}"
          "@keyframes amp{0%,65%{opacity:0;transform:scale(.4)}70%{opacity:1;transform:scale(1.12)}"
          "74%,95%{opacity:1;transform:scale(1)}98%,100%{opacity:0;transform:scale(.9)}}")
    return s.render()


# ---------------------------------------------------------------- işler
def slug(s):
    s = s.lower().translate(str.maketrans("çğıöşüâî", "cgiosuai"))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def button(label, primary, arrow=False):
    size, pad, h = 15, 24, 46
    has_glyphs(label, "N", 800)
    tw = measure(label, "N", 800, size)
    w = pad + tw + (26 if arrow else 0) + pad
    s = Svg(round(w + 2), h + 2, label)
    fg = C["onBal"] if primary else C["ink"]
    s.body.append(f'<rect x="1" y="1" width="{w:.1f}" height="{h}" rx="{h / 2}" '
                  f'fill="{C["bal"] if primary else C["card"]}" '
                  f'stroke="{C["balDeep"] if primary else C["espSoft"]}" stroke-width="1.2"/>')
    s.body.append(s.text(1 + pad, 1 + h / 2 + 5.3, label, "N", 800, size, fg))
    if arrow:
        ax = 1 + pad + tw + 12
        ay = 1 + h / 2
        s.body.append(f'<path d="M{ax:.1f} {ay}h13m-5-5 5 5-5 5" fill="none" stroke="{fg}" '
                      f'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>')
    return s.render()


# ---------------------------------------------------------------- main
def write(rel, data):
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, "utf-8")
    else:
        p.write_bytes(data)
    print(f"  {p.relative_to(ROOT)}  {p.stat().st_size / 1024:.0f} KB")


def main():
    global SITE
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", default=str(ROOT.parent / "icmsametWebSite"),
                    help="icmsametWebSite reposunun yolu")
    SITE = Site(ap.parse_args().site)

    print("assets/")
    for lang in ("en", "tr"):
        write(f"hero-{lang}.svg", hero(lang))
        write(f"reviews-{lang}.svg", reviews(lang))
    write("mascot-thinking.svg", thinking())

    for src, dst in (("hakkimda", "about"), ("maskot-tam", "full")):
        write(f"mascot/{dst}.webp", SITE.image(f"/assets/maskot/{src}.webp"))

    write("button/start-en.svg", button("Tell us about your project", True, arrow=True))
    write("button/start-tr.svg", button("Projenizi anlatın", True, arrow=True))
    for name in ("icmsamet.com", "LinkedIn", "Instagram"):
        write(f"button/{slug(name)}.svg", button(name, False))

    v = stats_vars()
    print(f"\nrakamlar: {v['yil']} yıl · {v['puan']} / 5 · {v['yorum']} yorum · {v['musteri']} müşteri")


if __name__ == "__main__":
    main()
