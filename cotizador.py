import os
import streamlit as st
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta, date
import random
import string
import re
import uuid  # Para sufijo único en folios

import unicodedata
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Table,
    TableStyle,
    Spacer,
    Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader  # ✅ NUEVO (para que las imágenes se rendericen bien)

# ----------------------------------------------------
# CONFIG STREAMLIT
# ----------------------------------------------------
st.set_page_config(
    page_title="Cotizador AT&T",
    page_icon="📱",
    layout="wide",
)

# ----------------------------------------------------
# AUTH UTILS (login)
# ----------------------------------------------------
def get_auth_credentials():
    """
    Read username & password from Streamlit secrets or environment variables.
    You must define AUTH_USER and AUTH_PASSWORD in .streamlit/secrets.toml
    (and/or in your hosting platform).
    """
    user = st.secrets.get("AUTH_USER", os.environ.get("AUTH_USER"))
    pwd = st.secrets.get("AUTH_PASSWORD", os.environ.get("AUTH_PASSWORD"))
    return user, pwd


# ----------------------------------------------------
# UTILIDADES
# ----------------------------------------------------
def rerun():
    """Compatibilidad entre st.rerun y st.experimental_rerun."""
    try:
        st.rerun()
    except Exception:
        st.experimental_rerun()


def last_day_of_month(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    first_next = date(d.year, d.month + 1, 1)
    return first_next - timedelta(days=1)


def parse_vigencia_cell(raw) -> date:
    """
    A partir del texto de vigencia de un equipo, regresa la fecha final:

    - Si contiene 'INDEFINIDO' → último día del mes actual.
    - Si tiene fechas dd/mm/aaaa o dd-mm-aaaa → toma la última.
    - Si falla → último día del mes actual.
    """
    today = date.today()

    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return last_day_of_month(today)

    txt = str(raw).strip().upper()

    if "INDEFINIDO" in txt:
        return last_day_of_month(today)

    matches = re.findall(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", txt)
    if matches:
        day, month, year = matches[-1]
        day = int(day)
        month = int(month)
        year = int(year)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return last_day_of_month(today)


def pdf_safe_text(x) -> str:
    """
    Normalize Excel/Unicode text into something ReportLab/Helvetica renders safely.
    Also escapes XML entities for Paragraph.
    """
    s = "" if x is None else str(x)

    # Normalize compatibility characters to common forms
    s = unicodedata.normalize("NFKC", s)

    # Replace hyphen variants that may not exist in Helvetica
    for ch in ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"]:
        s = s.replace(ch, "-")

    # Remove zero-width / control formatting chars
    s = "".join(c for c in s if unicodedata.category(c) not in ("Cf", "Cc"))

    # Escape XML for ReportLab Paragraph
    return escape(s)


@st.cache_data
def get_equipos_df(excel_bytes: bytes) -> pd.DataFrame:
    """
    Lee la hoja 'AT&T Premium' y regresa un DataFrame con:
      - Nombre Completo
      - PrecioLista (numérico, a partir de 'Precio de Contado')
      - VigenciaHasta (date)
      - Todas las columnas de promociones de meses: '24 Meses', '24 Meses2', etc.
    Además:
      - Filtra solo equipos vigentes (VigenciaHasta >= hoy)
      - Filtra solo equipos con promoción real (algún Meses != PrecioLista con tolerancia)
    """
    xio = BytesIO(excel_bytes)
    df = pd.read_excel(xio, sheet_name="AT&T Premium", header=4)

    base_cols = ["Nombre Completo", "Precio de Contado"]
    promo_cols = [c for c in df.columns if "Meses" in str(c)]

    # Nos quedamos con nombre, precio contado, columnas de meses,
    # y luego cualquier columna extra (VIGENCIA, COMENTARIOS, etc.).
    df = df[base_cols + promo_cols + [c for c in df.columns if c not in base_cols + promo_cols]].copy()

    df["Nombre Completo"] = df["Nombre Completo"].astype(str).str.strip()

    # Precio de contado → numérico limpio
    price = df["Precio de Contado"]
    price_str = price.astype(str).str.replace(r"[^\d,.-]", "", regex=True)
    price_str = price_str.str.replace(",", "", regex=False)
    df["PrecioLista"] = pd.to_numeric(price_str, errors="coerce")

    # Vigencia
    vig_cols = [c for c in df.columns if "vigencia" in str(c).lower()]
    if vig_cols:
        df["VigenciaTexto"] = df[vig_cols[0]]
    else:
        df["VigenciaTexto"] = "INDEFINIDO"

    df["VigenciaHasta"] = df["VigenciaTexto"].apply(parse_vigencia_cell)

    df = df.dropna(subset=["Nombre Completo", "PrecioLista"])
    df = df[df["Nombre Completo"].str.len() > 0]

    # ✅ SOLO VIGENTES
    today = date.today()
    df = df[df["VigenciaHasta"] >= today]

    # ✅ SOLO CON PROMOCIÓN REAL
    if promo_cols:
        promo_num = df[promo_cols].apply(pd.to_numeric, errors="coerce")
        diff = promo_num.sub(df["PrecioLista"], axis=0).abs()
        has_real_promo = diff.gt(0.01).any(axis=1)  # > 1 centavo
        df = df[has_real_promo]

    cols_return = ["Nombre Completo", "PrecioLista", "VigenciaHasta"] + promo_cols
    cols_return = [c for c in cols_return if c in df.columns]
    return df[cols_return]


@st.cache_data
def get_plan_options(excel_bytes: bytes):
    """
    Extrae los planes de renta desde la hoja 'AT&T Premium':

      - Fila 3 (index 2): nombres → 'Azul 1 (5GB)', etc.
      - Fila 4 (index 3): precios → 319, 419, 1259, etc.

    Para cada plan se calcula un 'suffix' que mapea a las columnas de promo:
      Azul 1  -> suffix ''   -> 24 Meses, 30 Meses, 36 Meses
      Azul 2  -> suffix '2'  -> 24 Meses2, 30 Meses2, ...
      ...
      Diamante -> suffix '8' -> 24 Meses8, 30 Meses8, 36 Meses8
    """
    df0 = pd.read_excel(BytesIO(excel_bytes), sheet_name="AT&T Premium", header=None)

    names_row = df0.iloc[2]
    price_row = df0.iloc[3]

    plan_suffixes = ["", "2", "3", "4", "5", "6", "7", "8"]
    plan_idx = -1

    options = []
    for name, price in zip(names_row, price_row):
        if pd.isna(name) or pd.isna(price):
            continue

        label = str(name).strip()
        if not label or "GB" not in label.upper():
            continue

        try:
            p = float(price)
        except (TypeError, ValueError):
            continue

        if p < 10 or p > 5000:
            continue

        gb = ""
        m = re.search(r"\(([^)]*)\)", label)
        if m:
            gb = m.group(1).strip()

        plan_idx += 1
        suffix = plan_suffixes[plan_idx] if plan_idx < len(plan_suffixes) else ""

        options.append(dict(plan=label, costo=p, gb=gb, suffix=suffix))

    return options


def obtener_precio_promocional_equipo(row_equipo: pd.Series, plazo: int, plan_suffix: str) -> float:
    """
    Dado un equipo (fila del DataFrame), el plazo y el suffix del plan,
    devuelve el precio promocional de equipo:

    - Si plazo ∈ {24, 30, 36} y existe columna '24 Meses{suffix}' etc con un valor numérico
      (incluyendo 0.0) → usa ese valor (puede ser gratis).
    - Si no existe columna o el valor no es numérico → usa PrecioLista (precio de contado).
    """
    if plazo in (24, 30, 36):
        base = f"{plazo} Meses"
        col_name = base + (plan_suffix if plan_suffix else "")
        if col_name in row_equipo.index:
            val = row_equipo[col_name]
            try:
                val_f = float(val)
                if not pd.isna(val_f):
                    return val_f
            except (TypeError, ValueError):
                pass

    # Sin promo válida → usar precio de contado
    return float(row_equipo["PrecioLista"])


def generar_folio(fecha: datetime) -> str:
    """
    Genera un folio prácticamente único combinando la fecha y un sufijo basado en UUID.

    Formato: yymmdd-XXXXXX
    - yymmdd -> fecha (año, mes, día)
    - XXXXXX -> primeros 6 caracteres de un UUID v4 en hex, en mayúsculas
    """
    base = fecha.strftime("%y%m%d")          # ej. 251212
    unique = uuid.uuid4().hex[:6].upper()    # ej. 'A3F9BC'
    return f"{base}-{unique}"                # ej. '251212-A3F9BC'


# ----------------------------------------------------
# CREACIÓN DEL PDF (ESTÉTICA AT&T)
# ----------------------------------------------------
def crear_pdf_cotizacion(
    ejecutivo,
    attuid,
    cliente,
    cliente_tel,
    cliente_email,
    cliente_dir,
    dias_validez,
    valido_hasta_str,
    equipos,
    planes_incluidos,
    comentarios,
    fichas_tecnicas=None,
) -> bytes:
    """
    Crea un PDF en memoria con formato muy similar al original de AT&T.
    """
    if fichas_tecnicas is None:
        fichas_tecnicas = []

    buffer = BytesIO()

    # Márgenes MUY pequeños
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=8 * mm,
        rightMargin=8 * mm,
        topMargin=8 * mm,
        bottomMargin=15 * mm,
    )

    # Helper para escalar anchos (en mm) al ancho útil del documento
    def scale_widths(base_mm_list):
        total_points = sum(w * mm for w in base_mm_list)
        if total_points == 0:
            return [w * mm for w in base_mm_list]
        scale = doc.width / total_points
        return [w * mm * scale for w in base_mm_list]

    styles = getSampleStyleSheet()
    base_font = "Helvetica"

    styles["Normal"].fontName = base_font
    styles["Normal"].fontSize = 8.5
    styles["Normal"].leading = 10

    styles.add(
        ParagraphStyle(
            name="HeaderBig",
            parent=styles["Normal"],
            fontSize=10,
            leading=12,
            spaceAfter=2,
            spaceBefore=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BlueTitle",
            parent=styles["Normal"],
            textColor=colors.white,
            alignment=1,
            fontSize=9,
            leading=11,
        )
    )
    styles.add(
        ParagraphStyle(
            name="HeaderRight",
            parent=styles["Normal"],
            alignment=2,
            fontSize=7,
            leading=9,
        )
    )
    styles.add(
        ParagraphStyle(
            name="HeaderCenter",
            parent=styles["Normal"],
            alignment=1,
            fontSize=7,
            leading=9,
        )
    )
    styles.add(
        ParagraphStyle(
            name="HeaderSmall",
            parent=styles["Normal"],
            fontSize=7,
            leading=9,
        )
    )

    story = []

    hoy = datetime.now()
    fecha_str = hoy.strftime("%d/%m/%Y")
    valido_hasta_text = valido_hasta_str or "—"
    folio = generar_folio(hoy)

    # ------------------ BARRA CIAN SUPERIOR ------------------
    top_bar = Table([[""]], colWidths=[doc.width])
    top_bar.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#00AEEF")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    story.append(top_bar)
    story.append(Spacer(1, 4))

    # ------------------ ENCABEZADO (LOGO + CLIENTE + FOLIO) ------------------
    logo_path = "att_logo.png"
    logo_flowable = None
    if os.path.exists(logo_path):
        logo_flowable = Image(logo_path, width=30 * mm, height=11 * mm)

    left_header = []
    if logo_flowable:
        left_header.append(logo_flowable)
    left_header.append(Paragraph("Distribuidor Autorizado", styles["HeaderSmall"]))

    header_widths = scale_widths([70, 50, 50])
    left_table = Table(
        [left_header],
        colWidths=[header_widths[0] * 0.45, header_widths[0] * 0.55],
    )
    left_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    cliente_label = "<b>CLIENTE</b>"
    cliente_nombre = cliente or "—"
    tel_str = cliente_tel or "—"
    email_str = cliente_email or "—"
    dir_str = cliente_dir or "—"

    center_html = (
        f"{cliente_label}<br/>{cliente_nombre}<br/>"
        f"Tel: {tel_str}<br/>"
        f"Email: {email_str}<br/>"
        f"Dirección: {dir_str}"
    )
    center_para = Paragraph(center_html, styles["HeaderCenter"])

    header_right_text = (
        f"<b>FOLIO:</b> {folio}<br/>"
        f"<b>Emitido:</b> {fecha_str}<br/>"
        f"<b>Ejecutivo</b><br/>{ejecutivo}<br/>"
        f"<b>ATTUID:</b> {attuid}"
    )
    right_para = Paragraph(header_right_text, styles["HeaderRight"])

    header_table = Table(
        [[left_table, center_para, right_para]],
        colWidths=header_widths,
    )
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(header_table)

    # Línea gris
    line_table = Table([[""]], colWidths=[doc.width])
    line_table.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ]
        )
    )
    story.append(line_table)
    story.append(Spacer(1, 6))

    # ------------------ VÁLIDO HASTA ------------------
    story.append(
        Paragraph(f"Válido hasta: <b>{valido_hasta_text}</b>", styles["Normal"])
    )
    story.append(Spacer(1, 4))

    # ------------------ TARJETAS CENTRALES ------------------
    card_left = Paragraph(
        (
            "<b>Esta cotización tiene validez de:</b><br/><br/>"
            f"<font size=18><b>{dias_validez} días</b></font><br/><br/>"
            f"Emitida el {fecha_str} por {ejecutivo} (Ejecutivo AT&amp;T).<br/>"
            "¡Gracias por su preferencia!"
        ),
        styles["Normal"],
    )

    aviso_texto = (
        "En cumplimiento de la Ley Federal de Protección de Datos Personales en "
        "Posesión de los Particulares y su Reglamento, AT&amp;T y el distribuidor "
        "autorizado tratan los datos personales del cliente conforme a su aviso de "
        "privacidad vigente, mismo que se pone a disposición del titular para "
        "consultarlo en todo momento."
    )
    aviso_para = Paragraph(aviso_texto, styles["Normal"])

    cards_widths = scale_widths([84, 86])
    card_right_table = Table(
        [
            [Paragraph("DISTRIBUIDOR AUTORIZADO AT&amp;T", styles["BlueTitle"])],
            [aviso_para],
        ],
        colWidths=[cards_widths[1]],
    )
    card_right_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#00AEEF")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("LEFTPADDING", (0, 1), (-1, 1), 6),
                ("RIGHTPADDING", (0, 1), (-1, 1), 6),
                ("TOPPADDING", (0, 1), (-1, 1), 6),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    cards = Table(
        [[card_left, card_right_table]],
        colWidths=cards_widths,
    )
    cards.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (0, 0), 0.5, colors.black),
                ("LEFTPADDING", (0, 0), (0, 0), 6),
                ("RIGHTPADDING", (0, 0), (0, 0), 6),
                ("TOPPADDING", (0, 0), (0, 0), 6),
                ("BOTTOMPADDING", (0, 0), (0, 0), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(cards)
    story.append(Spacer(1, 10))

    # ------------------ COMENTARIOS ------------------
    story.append(Paragraph("<b>Comentarios adicionales</b>", styles["HeaderBig"]))
    if comentarios and comentarios.strip():
        comentarios_html = comentarios.replace("\n", "<br/>")
    else:
        comentarios_html = "pendiente validación"
    story.append(Paragraph(comentarios_html, styles["Normal"]))
    story.append(Spacer(1, 8))

    # ------------------ RESUMEN DE EQUIPOS ------------------
    story.append(Paragraph("<b>Resumen de equipos</b>", styles["HeaderBig"]))

    columnas_equipos = [
        "EQUIPO",
        "PRECIO LISTA",
        "PROMOCIÓN",
        "AHORRO",
        "PLAZO",
        "% ENG",
        "ENGANCHE",
        "PLAN",
        "EQUIPO + PLAN",
    ]

    header_row = [Paragraph(col, styles["HeaderSmall"]) for col in columnas_equipos]
    data_equipos = [header_row]

    for item in equipos:
        data_equipos.append(
            [
                Paragraph(pdf_safe_text(item["equipo"]), styles["Normal"]),
                Paragraph(f"${item['precio_lista']:,.2f}", styles["Normal"]),
                Paragraph(f"${item['promocion']:,.2f}", styles["Normal"]),
                Paragraph(f"${item['ahorro']:,.2f}", styles["Normal"]),
                Paragraph(str(item["plazo"]), styles["Normal"]),
                Paragraph(f"{item['porc_eng']:.0f}%", styles["Normal"]),
                Paragraph(f"${item['enganche']:,.2f}", styles["Normal"]),
                Paragraph(pdf_safe_text(item["plan"]), styles["Normal"]),
                Paragraph(f"${item['eq_plan']:,.2f}", styles["Normal"]),
            ]
        )

    col_widths_equipos = scale_widths(
        [53, 20, 20, 17, 12, 12, 18, 15, 17]
    )

    tabla_equipos = Table(
        data_equipos,
        colWidths=col_widths_equipos,
        repeatRows=1,
    )
    tabla_equipos.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5F7FF")),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 1), (0, -1), "LEFT"),
                ("ALIGN", (7, 1), (7, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("WORDWRAP", (0, 0), (-1, -1), "CJK"),
            ]
        )
    )
    story.append(tabla_equipos)
    story.append(Spacer(1, 8))

    # ------------------ PLANES INCLUIDOS ------------------
    if len(planes_incluidos) > 0:
        story.append(Paragraph("<b>Planes incluidos</b>", styles["HeaderBig"]))

        data_planes = [[
            Paragraph("PLAN", styles["HeaderSmall"]),
            Paragraph("COSTO", styles["HeaderSmall"]),
            Paragraph("GB", styles["HeaderSmall"]),
        ]]
        for p in planes_incluidos:
            data_planes.append(
                [
                    Paragraph(pdf_safe_text(p["plan"]), styles["Normal"]),
                    Paragraph(f"${p['costo']:,.2f}", styles["Normal"]),
                    Paragraph(p.get("gb", ""), styles["Normal"]),
                ]
            )

        col_widths_planes = scale_widths([80, 45, 45])

        tabla_planes = Table(
            data_planes,
            colWidths=col_widths_planes,
        )
        tabla_planes.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5F7FF")),
                    ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 1), (0, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(tabla_planes)
        story.append(Spacer(1, 6))

    # ------------------ FICHAS TÉCNICAS / IMÁGENES ------------------
    if fichas_tecnicas and len(fichas_tecnicas) > 0:
        max_slots = min(3, len(fichas_tecnicas))  # 1, 2 o 3 imágenes reales
        slot_widths = [doc.width / max_slots] * max_slots
        slot_height = 45 * mm

        # ✅ Mantener referencias vivas hasta que termine doc.build()
        _img_stream_refs = []

        cells = []
        for i in range(max_slots):
            img_bytes = fichas_tecnicas[i]
            img_stream = BytesIO(img_bytes)
            img_stream.seek(0)
            _img_stream_refs.append(img_stream)

            img = Image(ImageReader(img_stream))
            img._restrictSize(slot_widths[i], slot_height)
            cells.append(img)

        tabla_fichas = Table(
            [cells],
            colWidths=slot_widths,
            rowHeights=[slot_height],
        )
        tabla_fichas.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story.append(tabla_fichas)
        story.append(Spacer(1, 8))

    # ------------------ FOOTER (LOGO + BARRA AL FONDO) ------------------
    def add_footer(canvas, doc_):
        canvas.saveState()
        page_width, page_height = letter

        bar_height = 8 * mm
        y_bar = 6 * mm
        x_bar = doc_.leftMargin
        bar_width = page_width - doc_.leftMargin - doc_.rightMargin

        canvas.setFillColor(colors.HexColor("#00AEEF"))
        canvas.rect(x_bar, y_bar, bar_width, bar_height, fill=1, stroke=0)

        if os.path.exists(logo_path):
            logo_height = 6 * mm
            logo_width = 16 * mm
            y_logo = y_bar + bar_height + 1 * mm
            canvas.drawImage(
                logo_path,
                x_bar,
                y_logo,
                width=logo_width,
                height=logo_height,
                preserveAspectRatio=True,
                mask="auto",
            )
        else:
            canvas.setFont("Helvetica-Bold", 9)
            canvas.setFillColor(colors.black)
            canvas.drawString(x_bar, y_bar + bar_height + 3, "AT&T")

        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(colors.white)
        canvas.drawRightString(
            page_width - doc_.rightMargin - 4 * mm,
            y_bar + bar_height / 2 - 3,
            f"Válido hasta: {valido_hasta_text}",
        )

        canvas.restoreState()

    doc.build(story, onFirstPage=add_footer, onLaterPages=add_footer)
    buffer.seek(0)
    return buffer.getvalue()


# ----------------------------------------------------
# SESSION STATE
# ----------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "equipos_cotizacion" not in st.session_state:
    st.session_state["equipos_cotizacion"] = []
if "cliente" not in st.session_state:
    st.session_state["cliente"] = ""
if "cliente_tel" not in st.session_state:
    st.session_state["cliente_tel"] = ""
if "cliente_email" not in st.session_state:
    st.session_state["cliente_email"] = ""
if "cliente_dir" not in st.session_state:
    st.session_state["cliente_dir"] = ""
if "dias_validez" not in st.session_state:
    st.session_state["dias_validez"] = 7
if "fecha_validez_str" not in st.session_state:
    st.session_state["fecha_validez_str"] = ""
if "comentarios" not in st.session_state:
    st.session_state["comentarios"] = ""
if "fichas_tecnicas" not in st.session_state:
    st.session_state["fichas_tecnicas"] = []


# ----------------------------------------------------
# LOGIN PAGE (protects the whole app)
# ----------------------------------------------------
valid_user, valid_pwd = get_auth_credentials()

if not st.session_state["authenticated"]:
    st.title("🔐 Acceso al cotizador AT&T")

    with st.form("auth_form"):
        input_user = st.text_input("Usuario")
        input_pwd = st.text_input("Contraseña", type="password")
        submit_auth = st.form_submit_button("Ingresar")

    if submit_auth:
        if valid_user is None or valid_pwd is None:
            st.error("Credenciales no configuradas en secrets (AUTH_USER / AUTH_PASSWORD).")
        elif input_user == valid_user and input_pwd == valid_pwd:
            st.session_state["authenticated"] = True
            st.success("Acceso correcto.")
            rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")

    st.stop()


# ----------------------------------------------------
# PANTALLA 1
# ----------------------------------------------------
if not st.session_state["logged_in"]:
    st.title("Cotizador - Inicio")

    with st.form("login"):
        ejecutivo = st.text_input("Nombre del ejecutivo:")
        attuid = st.text_input("ATTUID:")
        archivo = st.file_uploader(
            "Sube la lista de precios (.xlsm / .xlsx / .xls)",
            type=["xlsm", "xlsx", "xls"],
        )
        submitted = st.form_submit_button("Crear cotización")

    if submitted:
        if not ejecutivo or not attuid or not archivo:
            st.error(
                "Por favor captura el nombre del ejecutivo, ATTUID y sube el archivo de precios."
            )
        else:
            st.session_state["ejecutivo"] = ejecutivo
            st.session_state["attuid"] = attuid
            st.session_state["excel_bytes"] = archivo.getvalue()
            st.session_state["logged_in"] = True
            rerun()

    st.stop()


# ----------------------------------------------------
# PANTALLA 2
# ----------------------------------------------------
st.title(
    f"Cotizador - Ejecutivo: {st.session_state['ejecutivo']} "
    f"(ATTUID: {st.session_state['attuid']})"
)

excel_bytes = st.session_state["excel_bytes"]

df_equipos_vista = get_equipos_df(excel_bytes)

# ✅ Si no hay equipos vigentes con promo, detener
if df_equipos_vista.empty:
    st.error("No hay equipos vigentes con promoción en el archivo cargado.")
    st.stop()

lista_equipos = sorted(df_equipos_vista["Nombre Completo"].unique().tolist())
plan_options = get_plan_options(excel_bytes)

col_izq, col_der = st.columns([3, 2])

with col_izq:
    st.subheader("Datos del equipo y plan")

    equipo_sel = st.selectbox("Equipo:", lista_equipos)

    precio_row = df_equipos_vista[
        df_equipos_vista["Nombre Completo"] == equipo_sel
    ].iloc[0]
    precio_lista_default = float(precio_row["PrecioLista"])
    vigencia_hasta_equipo = precio_row["VigenciaHasta"]

    st.text_input(
        "Precio de contado / lista:",
        value=f"{precio_lista_default:,.2f}",
        disabled=True,
    )
    precio_lista = precio_lista_default

    st.text_input(
        "Vigencia del equipo (fecha límite desde Excel):",
        value=vigencia_hasta_equipo.strftime("%d/%m/%Y"),
        disabled=True,
    )

    if plan_options:
        plan_labels = [p["plan"] for p in plan_options]
        plan_label_sel = st.selectbox("Plan (nombre comercial):", plan_labels)
        selected_plan = next(p for p in plan_options if p["plan"] == plan_label_sel)
        plan_sel = selected_plan["plan"]
        plan_costo = float(selected_plan["costo"])
        plan_gb = selected_plan["gb"]
        plan_suffix = selected_plan.get("suffix", "")
    else:
        st.warning(
            "No se encontraron planes en el archivo. Se usará un plan sin costo."
        )
        plan_sel = "Plan sin costo"
        plan_costo = 0.0
        plan_gb = ""
        plan_suffix = ""

    # ✅ Plazos disponibles SOLO desde el Excel (según columnas "XX Meses{suffix}")
    plan_promo_cols = [
        c for c in df_equipos_vista.columns
        if re.match(rf"^(\d+)\s*Meses{re.escape(plan_suffix)}$", str(c))
    ]
    plazos_disponibles = sorted({int(re.match(r"^(\d+)\s*Meses", str(c)).group(1)) for c in plan_promo_cols})

    # Fallback: si por alguna razón no encuentra por suffix, usa todos los "Meses" del archivo
    if not plazos_disponibles:
        all_promo_cols = [c for c in df_equipos_vista.columns if "Meses" in str(c)]
        plazos_disponibles = sorted({int(re.match(r"^(\d+)\s*Meses", str(c)).group(1)) for c in all_promo_cols})

    # Default: 24 si existe
    default_idx = plazos_disponibles.index(24) if 24 in plazos_disponibles else 0

    plazo = st.selectbox("Plazo (meses):", plazos_disponibles, index=default_idx)

    porc_eng = st.number_input(
        "% de enganche:", min_value=0.0, max_value=100.0, value=0.0, step=5.0
    )

    # 👉 AQUÍ se usa SIEMPRE el precio promocional real del Excel
    if st.button("Ingresar", type="primary"):
        promo = obtener_precio_promocional_equipo(precio_row, plazo, plan_suffix)

        ahorro = max(precio_lista - promo, 0.0)
        enganche_mxn = promo * (porc_eng / 100.0)
        if plazo > 0:
            pago_equipo_mensual = (promo - enganche_mxn) / plazo
        else:
            pago_equipo_mensual = 0.0
        equipo_mas_plan = pago_equipo_mensual + float(plan_costo)

        st.session_state["equipos_cotizacion"].append(
            dict(
                equipo=equipo_sel,
                precio_lista=precio_lista,
                promocion=promo,
                ahorro=ahorro,
                plazo=plazo,
                porc_eng=porc_eng,
                enganche=enganche_mxn,
                plan=plan_sel,
                eq_plan=equipo_mas_plan,
                plan_costo=float(plan_costo),
                plan_gb=plan_gb,
                vigencia_hasta=vigencia_hasta_equipo,
                plan_suffix=plan_suffix,
            )
        )
        st.success("Equipo agregado a la cotización.")


with col_der:
    st.subheader("Datos del cliente")
    st.session_state["cliente"] = st.text_input(
        "Nombre del cliente:",
        value=st.session_state["cliente"],
    )
    st.session_state["cliente_tel"] = st.text_input(
        "Teléfono del cliente:",
        value=st.session_state["cliente_tel"],
    )
    st.session_state["cliente_email"] = st.text_input(
        "Correo electrónico del cliente:",
        value=st.session_state["cliente_email"],
    )
    st.session_state["cliente_dir"] = st.text_area(
        "Dirección del cliente:",
        value=st.session_state["cliente_dir"],
        height=60,
    )
    st.session_state["comentarios"] = st.text_area(
        "Comentarios (se incluyen en el PDF):",
        value=st.session_state["comentarios"],
        height=80,
    )

    fichas_files = st.file_uploader(
        "Fichas técnicas (hasta 3 imágenes):",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
    )
    if fichas_files:
        st.session_state["fichas_tecnicas"] = [
            f.getvalue() for f in fichas_files[:3]
        ]

# ----------------------------------------------------
# TABLA DE EQUIPOS
# ----------------------------------------------------
st.subheader("Resumen de equipos en la cotización")

if len(st.session_state["equipos_cotizacion"]) == 0:
    st.info(
        "Aún no has agregado equipos. Usa el botón **Ingresar** después de capturar los datos."
    )
else:
    df_items = pd.DataFrame(st.session_state["equipos_cotizacion"])

    df_mostrar = pd.DataFrame(
        {
            "EQUIPO": df_items["equipo"],
            "PRECIO LISTA": df_items["precio_lista"],
            "PROMOCIÓN": df_items["promocion"],
            "AHORRO": df_items["ahorro"],
            "PLAZO": df_items["plazo"],
            "% ENG": df_items["porc_eng"],
            "ENGANCHE": df_items["enganche"],
            "PLAN": df_items["plan"],
            "EQUIPO + PLAN": df_items["eq_plan"],
        }
    )

    st.dataframe(
        df_mostrar.style.format(
            {
                "PRECIO LISTA": "${:,.2f}",
                "PROMOCIÓN": "${:,.2f}",
                "AHORRO": "${:,.2f}",
                "ENGANCHE": "${:,.2f}",
                "EQUIPO + PLAN": "${:,.2f}",
                "% ENG": "{:.0f}%",
            }
        ),
        width="stretch",
    )

    col_b1, col_b2, col_b3 = st.columns(3)
    with col_b1:
        if st.button("Eliminar último"):
            if len(st.session_state["equipos_cotizacion"]) > 0:
                st.session_state["equipos_cotizacion"].pop()
                st.warning("Se eliminó el último equipo.")
                rerun()
    with col_b2:
        if st.button("Limpiar lista"):
            st.session_state["equipos_cotizacion"] = []
            st.warning("Se limpiaron todos los equipos.")
            rerun()
    with col_b3:
        if st.button("Nueva cotización"):
            st.session_state["equipos_cotizacion"] = []
            st.session_state["cliente"] = ""
            st.session_state["cliente_tel"] = ""
            st.session_state["cliente_email"] = ""
            st.session_state["cliente_dir"] = ""
            st.session_state["dias_validez"] = 7
            st.session_state["fecha_validez_str"] = ""
            st.session_state["comentarios"] = ""
            st.session_state["fichas_tecnicas"] = []
            st.info(
                "Se inició una nueva cotización (se conservarán ejecutivo, ATTUID y archivo)."
            )
            rerun()

# ----------------------------------------------------
# VIGENCIA Y PLANES INCLUIDOS
# ----------------------------------------------------
planes_incluidos = []

if len(st.session_state["equipos_cotizacion"]) > 0:
    df_items = pd.DataFrame(st.session_state["equipos_cotizacion"])

    today = date.today()
    fechas = [
        v for v in df_items["vigencia_hasta"].tolist()
        if isinstance(v, date)
    ]
    if fechas:
        vigencia_global = min(fechas)
    else:
        vigencia_global = last_day_of_month(today)

    dias_restantes = max(1, (vigencia_global - today).days + 1)

    dias_validez_pdf = min(dias_restantes, 7)

    vigencia_efectiva = today + timedelta(days=dias_validez_pdf - 1)

    st.session_state["dias_validez"] = dias_validez_pdf
    st.session_state["fecha_validez_str"] = vigencia_efectiva.strftime("%d/%m/%Y")

    st.markdown(
        f"**Vigencia de la cotización:** hasta "
        f"{st.session_state['fecha_validez_str']} "
        f"({dias_validez_pdf} días)."
    )

    df_planes_incl = (
        df_items[["plan", "plan_costo", "plan_gb"]]
        .drop_duplicates()
        .rename(columns={"plan": "PLAN", "plan_costo": "COSTO", "plan_gb": "GB"})
    )
    st.subheader("Planes incluidos")
    st.dataframe(df_planes_incl, width="stretch")

    for _, row in df_planes_incl.iterrows():
        planes_incluidos.append(
            dict(plan=row["PLAN"], costo=row["COSTO"], gb=row["GB"])
        )
else:
    st.markdown("**Vigencia de la cotización:** pendiente (sin equipos).")

# ----------------------------------------------------
# GENERAR PDF
# ----------------------------------------------------
st.divider()
st.subheader("Generar PDF")

if len(st.session_state["equipos_cotizacion"]) == 0:
    st.info("Agrega al menos un equipo para poder generar el PDF.")
else:
    pdf_bytes = crear_pdf_cotizacion(
        ejecutivo=st.session_state["ejecutivo"],
        attuid=st.session_state["attuid"],
        cliente=st.session_state["cliente"],
        cliente_tel=st.session_state["cliente_tel"],
        cliente_email=st.session_state["cliente_email"],
        cliente_dir=st.session_state["cliente_dir"],
        dias_validez=st.session_state["dias_validez"],
        valido_hasta_str=st.session_state["fecha_validez_str"],
        equipos=st.session_state["equipos_cotizacion"],
        planes_incluidos=planes_incluidos,
        comentarios=st.session_state["comentarios"],
        fichas_tecnicas=st.session_state.get("fichas_tecnicas", []),
    )

    st.download_button(
        label="📄 Descargar cotización en PDF",
        data=pdf_bytes,
        file_name="cotizacion_att.pdf",
        mime="application/pdf",
    )
