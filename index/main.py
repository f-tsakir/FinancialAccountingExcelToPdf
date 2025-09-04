# index/main.py
from pathlib import Path
from io import BytesIO
import io, re, unicodedata
import pandas as pd
from pandas.api.types import is_numeric_dtype

from fastapi import FastAPI, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily


BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"

def register_turkish_fonts() -> tuple[str, str]:
    candidates = [
        ("DejaVuSans", FONTS_DIR / "DejaVuSans.ttf", FONTS_DIR / "DejaVuSans-Bold.ttf"),
        ("Arial", Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
    ]
    for fam, reg, bold in candidates: # fonts
        if reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont(fam, str(reg)))
            pdfmetrics.registerFont(TTFont(f"{fam}-Bold", str(bold)))
            registerFontFamily(fam, normal=fam, bold=f"{fam}-Bold", italic=fam, boldItalic=f"{fam}-Bold")
            return fam, f"{fam}-Bold"
    return "Helvetica", "Helvetica-Bold"

FONT_REG, FONT_BOLD = register_turkish_fonts()

# ============= Text & Number Utils =============
def _norm_text(s: str) -> str:
    if s is None: return ""
    s = unicodedata.normalize("NFKD", str(s)).lower()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ı̇", "i").replace("İ", "i")
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ()\-.,]", " ", s)).strip() #boşlukları siler, türkçe karakterleri düzeltir
    return s

def parse_amount(x):
    if x is None or (isinstance(x, float) and pd.isna(x)): return None
    if isinstance(x, (int, float)): return float(x)
    s = str(x).strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg: s = s[1:-1]
    s = re.sub(r"[^\d.,\-]", "", s)
    if s.count(",") == 1 and s.count(".") >= 1:
        s = s.replace(".", "").replace(",", ".")
    else:
        if s.count(".") > 1 and "," not in s: s = s.replace(".", "")
        if s.count(",") == 1 and "." not in s: s = s.replace(",", ".")
    try: v = float(s)
    except Exception: return None
    return -abs(v) if neg else v # negatif değerlerin, nokta ve virgüllerin yazımıyla alakalı detaylar

def fmt_paren(v: float | None) -> str:
    if v is None: return ""
    x = abs(float(v))
    s = f"{int(round(x)):,}".replace(",", ".")
    return f"({s})" if float(v) < 0 else s

def first_text_col(df: pd.DataFrame): # pandas fonksiyonu ile text içeren ilk column u return eder
    for c in df.columns:
        if df[c].dtype == "O": return c
    return df.columns[0]

def first_num_col(df: pd.DataFrame):
    for c in df.columns:
        if is_numeric_dtype(df[c]): return c
    return df.columns[1] if len(df.columns) > 1 else df.columns[0] # pandas fonksiyonu ile sayı içeren ilk column u return eder
# yukarıdaki 2 fonksiyon kesin bir algoritma değil, heuristic bir metoddur


#canonical formlar
ALIASES = {
    "hasilat": ["hasılat", "net satis", "satış gelirleri", "gelirler", "net satışlar"],
    "satislarin maliyeti": ["satışların maliyeti", "maliyet"],
    "genel yonetim giderleri": ["genel yönetim giderleri"],
    "pazarlama satis ve dagitim giderleri": ["pazarlama, satış ve dağıtım giderleri", "pazarlama giderleri"],
    "esas faaliyetlerden diger faaliyet gelirleri": ["esas faaliyetlerden diğer faaliyet gelirleri", "diger faaliyet gelirleri"],
    "esas faaliyetlerden diger faaliyet giderleri": ["esas faaliyetlerden diğer faaliyet giderleri", "diger faaliyet giderleri"],
    "yatirim faaliyetlerinden gelirler": ["yatırım faaliyetlerinden gelirler", "yatirim gelirleri"],
    "yatirim faaliyetlerinden giderler": ["yatırım faaliyetlerinden giderler", "yatirim giderleri"],
    "ozkaynak paylari": ["özkaynak yöntemiyle değerlenen yatırımların karlarından paylar", "ozkaynak paylari"],
    "finansman gelirleri": ["finansman gelirleri"],
    "finansman giderleri": ["finansman giderleri"],
    "net parasal pozisyon": ["net parasal pozisyon kazanç/(kayıpları)", "parasal pozisyon"],
    "vergi": ["sürdürülen faaliyetler vergi (gideri)/ geliri", "vergi (gideri)/geliri"],
    "vergi_ertelenmis": ["ertelenmiş vergi (gideri)/ geliri", "ertelenmis vergi"],
}


def canonical_key(label: str):
    n = _norm_text(label)
    for key, alts in ALIASES.items():
        if n == key or any(_norm_text(a) in n or n in _norm_text(a) for a in alts):
            return key
    return None # her şeyi canonical form olarak yazmamıza yarar


def extract_from_single_sheet(df: pd.DataFrame) -> dict: # bu ve alttaki fonksiyonda sayfadaki değerleri, objectleri canonical formlar ile eşleştiriyoruz.
    label_col = first_text_col(df)
    value_col = first_num_col(df)
    if not is_numeric_dtype(df[value_col]):
        df[value_col] = df[value_col].map(parse_amount)
    values = {}
    for _, row in df[[label_col, value_col]].dropna().iterrows():
        k = canonical_key(str(row[label_col]))
        if k: values[k] = float(parse_amount(row[value_col]))
    return values

def extract_from_multi_sheet(xls: pd.ExcelFile) -> dict:
    values = {}
    for sh in xls.sheet_names:
        df = xls.parse(sh)
        k = canonical_key(sh) or canonical_key(df.columns[0])
        if not k: continue
        num_col = None
        for c in df.columns:
            if _norm_text(c) == "ham veri" and (is_numeric_dtype(df[c]) or df[c].dtype == "O"):
                num_col = c; break
        if num_col is None:
            for c in df.columns:
                if is_numeric_dtype(df[c]): num_col = c; break
        if num_col is None:
            for c in df.columns:
                conv = df[c].map(parse_amount)
                if conv.notna().sum() >= max(1, len(df)//3):
                    df[c] = conv; num_col = c; break
        if num_col is None: continue
        series = df[num_col]
        if not is_numeric_dtype(series): series = series.map(parse_amount)
        total = float(pd.to_numeric(series, errors="coerce").fillna(0).sum())
        values[k] = total
    return values

# gider/maliyet kalemleri daima negatif;
EXPENSE_KEYS = {
    "satislarin maliyeti",
    "genel yonetim giderleri",
    "pazarlama satis ve dagitim giderleri",
    "esas faaliyetlerden diger faaliyet giderleri",
    "yatirim faaliyetlerinden giderler",
    "finansman giderleri",
}
def enforce_signs(values: dict) -> dict:
    out = dict(values)
    for k in EXPENSE_KEYS:
        if k in out and out[k] is not None:
            out[k] = -abs(float(out[k]))
    return out


def build_pdf(values: dict) -> bytes:
    values = enforce_signs(values)
    v = lambda k: float(values.get(k, 0.0))

    # Subtotals
    hasilat   = v("hasilat")
    sm        = v("satislarin maliyeti")
    brut      = hasilat + sm

    gyg       = v("genel yonetim giderleri")
    paz       = v("pazarlama satis ve dagitim giderleri")
    ef_gelir  = v("esas faaliyetlerden diger faaliyet gelirleri")
    ef_gider  = v("esas faaliyetlerden diger faaliyet giderleri")

    # ESAS FAALİYET ZARARI subtotal AFTER the 4 lines above
    esas_fa   = brut + gyg + paz + ef_gelir + ef_gider

    yat_gel   = v("yatirim faaliyetlerinden gelirler")
    yat_gid   = v("yatirim faaliyetlerinden giderler")
    ozk_pay   = v("ozkaynak paylari")

    faaliyet_oncesi = esas_fa + yat_gel + yat_gid + ozk_pay

    fin_gel   = v("finansman gelirleri")
    fin_gid   = v("finansman giderleri")
    parasal   = v("net parasal pozisyon")

    vergi_once = faaliyet_oncesi + fin_gel + fin_gid + parasal
    vergi_top  = v("vergi")
    vergi_ert  = values.get("vergi_ertelenmis", vergi_top)  # yoksa aynı sayı ile göster

    sur_donem  = vergi_once + vergi_top
    donem_kari = sur_donem

    # canvas helpers
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4) #pdf formatı olması açısından
    W, H = A4
    y = H - 36

    def header_with_total(title: str, total: float | None, heavy_top=True):
        nonlocal y
        data = [[title, "" if total is None else fmt_paren(total)]]
        tbl = Table(data, colWidths=[420, 100])
        st = TableStyle([
            ("FONTNAME", (0,0), (-1,-1), FONT_BOLD),
            ("FONTSIZE", (0,0), (-1,-1), 10),
            ("ALIGN", (1,0), (1,0), "RIGHT"),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ])
        if heavy_top:
            st.add("LINEABOVE", (0,0), (-1,0), 1.2, colors.black)
        tbl.setStyle(st)
        tw, th = tbl.wrapOn(c, W-72, y)
        if y - th < 48: c.showPage(); y = H - 36
        tbl.drawOn(c, 36, y - th)
        y -= th #tasarımlar, çizgiler

    def lines_block(rows, bottom=True, indent_idx=None):
        nonlocal y
        data = [["",""]] + [[lbl, fmt_paren(val)] for lbl, val in rows]
        tbl = Table(data, colWidths=[420, 100])
        st = TableStyle([
            ("FONTNAME", (0,0), (-1,-1), FONT_REG),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("ALIGN", (1,0), (1,-1), "RIGHT"),
            ("LINEABOVE", (0,1), (-1,1), 0.8, colors.black),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ])
        if bottom: st.add("LINEBELOW", (0,-1), (-1,-1), 0.8, colors.black)
        if indent_idx:
            for i in indent_idx:
                r = 1 + i
                st.add("LEFTPADDING", (0, r), (0, r), 18)  # indent label
        tbl.setStyle(st)
        tw, th = tbl.wrapOn(c, W-72, y)
        if y - th < 48: c.showPage(); y = H - 36
        tbl.drawOn(c, 36, y - th)
        y -= th + 6

    # ===== EXACT ORDER (matches the JPG) =====
    c.setFont(FONT_BOLD, 14); c.drawString(36, y, "KAR VEYA ZARAR TABLOSU"); y -= 16

    # 1) KAR VEYA ZARAR KISMI
    header_with_total("KAR VEYA ZARAR KISMI", None, heavy_top=True)
    lines_block([("Hasılat", hasilat), ("Satışların Maliyeti", sm)], bottom=True)

    # 2) BRÜT KAR/(ZARAR)
    header_with_total("BRÜT KAR/(ZARAR)", brut, heavy_top=True)
    lines_block([], bottom=True)  # çerçeveyi kapatmak için ince çizgi

    # 3) (Header yok) — bu dört satır BRÜT'ten sonra gelir, sonra ESAS başlığı
    lines_block([
        ("Genel Yönetim Giderleri", gyg),
        ("Pazarlama, Satış ve Dağıtım Giderleri", paz),
        ("Esas Faaliyetlerden Diğer Faaliyet Gelirleri", ef_gelir),
        ("Esas Faaliyetlerden Diğer Faaliyet Giderleri", ef_gider),
    ], bottom=True)

    # 4) ESAS FAALİYET ZARARI  (toplam sağda)
    header_with_total("ESAS FAALİYET ZARARI", esas_fa, heavy_top=True)
    lines_block([
        ("Yatırım Faaliyetlerinden Gelirler", yat_gel),
        ("Yatırım Faaliyetlerinden Giderler", yat_gid),
        ("Özkaynak Yöntemiyle Değerlenen Yatırımların Karlarından Paylar", ozk_pay),
    ], bottom=True)

    # 5) FİNANSMAN GELİR/(GİDERİ) ÖNCESİ FAALİYET ZARARI
    header_with_total("FİNANSMAN GELİR/(GİDERİ) ÖNCESİ FAALİYET ZARARI", faaliyet_oncesi, heavy_top=True)
    lines_block([
        ("Finansman Gelirleri", fin_gel),
        ("Finansman Giderleri", fin_gid),
        ("Net Parasal Pozisyon Kazanç/(Kayıpları)", parasal),
    ], bottom=True)

    # 6) SÜRDÜRÜLEN FAALİYETLER VERGİ ÖNCESİ KARI/(ZARAR)
    header_with_total("SÜRDÜRÜLEN FAALİYETLER VERGİ ÖNCESİ KARI/(ZARAR)", vergi_once, heavy_top=True)
    lines_block([], bottom=True)

    # 7) Vergi bloğu (ayrı kutu halinde, başlıkta toplam yok)
    header_with_total("SÜRDÜRÜLEN FAALİYETLER VERGİ (GİDERİ)/ GELİRİ", None, heavy_top=True)
    lines_block([
        ("Sürdürülen Faaliyetler Vergi (Gideri)/ Geliri", vergi_top),
        ("- Ertelenmiş Vergi (Gideri)/ Geliri", vergi_ert),
    ], bottom=True, indent_idx=[1])

    # 8) SÜRDÜRÜLEN FAALİYETLER DÖNEM KARI/(ZARARI)
    header_with_total("SÜRDÜRÜLEN FAALİYETLER DÖNEM KARI/(ZARARI)", sur_donem, heavy_top=True)
    lines_block([], bottom=True)

    # 9) DÖNEM KARI/(ZARARI)
    header_with_total("DÖNEM KARI/(ZARARI)", donem_kari, heavy_top=True)
    lines_block([], bottom=True)

    c.showPage(); c.save()
    pdf_bytes = buf.getvalue(); buf.close()
    return pdf_bytes


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"ok": True, "font_regular": FONT_REG, "font_bold": FONT_BOLD} # ping kontrol için get

@app.get("/", response_class=HTMLResponse) # başta tanımladığımız base_dir i yani main page i gönderir
def root():
    f = BASE_DIR / "index.html"
    if not f.exists():
        return HTMLResponse(f"<h3>index.html not found</h3><pre>{f}</pre>", status_code=500)
    return FileResponse(f, media_type="text/html")

app.mount("/static", StaticFiles(directory=str(BASE_DIR), html=False), name="static")

@app.post("/api/process") # dosya yolladığımız için api ' a post ettik
async def process(file: UploadFile = File(...)): # CPU için bekliyoruz dosyanın yüklenmesini

    try:
        raw = await file.read()
        name = (file.filename or "").lower()
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
            values = extract_from_single_sheet(df) # tek ve çok sayfalı olmasına göre ayır
        else:
            xls = pd.ExcelFile(io.BytesIO(raw))
            if len(xls.sheet_names) > 1:
                values = extract_from_multi_sheet(xls)
            else:
                df = xls.parse(xls.sheet_names[0])
                values = extract_from_single_sheet(df)
        pdf = build_pdf(values) #values dictionary si hazır olduğunda pdf oluşturulur, app in çalışması için bunu bekleriz
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="gelir-tablosu.pdf"'}) # bu da pdf dosyamız
    except Exception as e:
        (BASE_DIR / "storage").mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "storage" / "last_error.log").write_text(repr(e), encoding="utf-8")
        return JSONResponse({"error": repr(e)}, status_code=500) #herhangi bir hatada 500 kodunu döndürürüz ve hatanın neyden kaynaklı olduğunu anlarız
