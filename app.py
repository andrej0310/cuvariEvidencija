# app.py
import os
import warnings
import pandas as pd
from datetime import datetime, date, time as dtime, timedelta

import pytds
from pytds.tds_base import TDS74, TDS73, TDS72

import dash
from dash import Dash, dcc, html, dash_table, Input, Output, State

# =========================
# KONFIG
# =========================
DB_HOST     = os.getenv("DB_HOST")
DB_PORT     = int(os.getenv("DB_PORT", "1433"))
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

_env_tds = os.getenv("DB_TDS", "").strip()
if _env_tds == "74":
    PREFERRED_TDS = TDS74
elif _env_tds == "73":
    PREFERRED_TDS = TDS73
elif _env_tds == "72":
    PREFERRED_TDS = TDS72
else:
    PREFERRED_TDS = TDS74

warnings.filterwarnings(
    "ignore",
    message=r"pandas only supports SQLAlchemy connectable.*",
    category=UserWarning,
)

# window za PRIJAVE (dolazak)
WINDOW_BEFORE_MIN = 60   # min prije termina
WINDOW_AFTER_MIN  = 30   # min poslije termina

PAGE_AUTO_IN_BEFORE_MIN = 60  # auto-paging od T-60 do T

# debug info
brojac = 0
LAST_DB_ERROR = ""

# =========================
# DB helper
# =========================
def fetch_data_from_db(query: str, params=None) -> pd.DataFrame | None:
    global brojac, LAST_DB_ERROR, PREFERRED_TDS
    for ver in [PREFERRED_TDS, TDS73, TDS72, None]:
        try:
            kw = dict(port=DB_PORT, login_timeout=5, timeout=10, autocommit=True)
            if ver is not None:
                kw["tds_version"] = ver
            with pytds.connect(DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, **kw) as conn:
                df = pd.read_sql(query, conn, params=params)
            brojac += 1
            LAST_DB_ERROR = ""
            if ver is not None:
                PREFERRED_TDS = ver
            return df
        except Exception as e:
            LAST_DB_ERROR = f"{type(e).__name__}: {e}"
            continue
    print("⛔ Greška u konekciji/SQL:", LAST_DB_ERROR)
    return None

# =========================
# Dohvati podatke
# =========================
def _norm_room(s: pd.Series) -> pd.Series:
    return (s.astype(str).str.strip().str.upper().str.replace(r"\s+", "", regex=True))

def fetch_raspored_for_date(d: date) -> pd.DataFrame:
    q = """
        SELECT termin, [učionica] AS ucionica, [state]
        FROM dbo.ispiti_raspored
        WHERE [state] = 1
          AND CONVERT(date, termin) = CONVERT(date, %s)
    """
    df = fetch_data_from_db(q, params=[d])
    if df is None:
        return pd.DataFrame(columns=["termin","ucionica","state"])
    if df.empty: return df
    df["termin"]   = pd.to_datetime(df["termin"])
    df["ucionica"] = _norm_room(df["ucionica"])
    return df

def fetch_login_log_for_date(d: date) -> pd.DataFrame:
    start = datetime.combine(d, dtime(0,0,0))
    end   = datetime.combine(d, dtime(23,59,59))
    q = """
        SELECT [time], [card_no] AS uid_kartice, [device_name] AS ucionica, [state]
        FROM dbo.acc_monitor_log
        WHERE [state] = 1 AND [time] >= %s AND [time] <= %s
    """
    df = fetch_data_from_db(q, params=[start, end])
    if df is None:
        return pd.DataFrame(columns=["time","uid_kartice","ucionica","state"])
    if df.empty: return df
    df["time"]        = pd.to_datetime(df["time"])
    df["ucionica"]    = _norm_room(df["ucionica"])
    df["uid_kartice"] = df["uid_kartice"].astype(str).str.strip()
    return df

def fetch_logout_log_for_date(d: date) -> pd.DataFrame:
    start = datetime.combine(d, dtime(0,0,0))
    end   = datetime.combine(d, dtime(23,59,59))
    q = """
        SELECT [time], [card_no] AS uid_kartice, [device_name] AS ucionica, [state]
        FROM dbo.acc_monitor_log
        WHERE [state] = 0 AND [time] >= %s AND [time] <= %s
    """
    df = fetch_data_from_db(q, params=[start, end])
    if df is None:
        return pd.DataFrame(columns=["time","uid_kartice","ucionica","state"])
    if df.empty: return df
    df["time"]        = pd.to_datetime(df["time"])
    df["ucionica"]    = _norm_room(df["ucionica"])
    df["uid_kartice"] = df["uid_kartice"].astype(str).str.strip()
    return df

def fetch_kartice() -> pd.DataFrame:
    q = """SELECT [Čuvar] AS cuvar, [UID kartice] AS uid_kartice FROM dbo.cuvari_kartice"""
    df = fetch_data_from_db(q)
    if df is None:
        return pd.DataFrame(columns=["cuvar","uid_kartice"])
    if df.empty: return df
    df["uid_kartice"] = df["uid_kartice"].astype(str).str.strip()
    return df

def fetch_min_date_in_raspored() -> date | None:
    q = "SELECT MIN(CONVERT(date, termin)) AS d FROM dbo.ispiti_raspored WHERE [state]=1"
    df = fetch_data_from_db(q)
    if df is not None and not df.empty and pd.notna(df.iloc[0]["d"]):
        return pd.to_datetime(df.iloc[0]["d"]).date()
    return None

# =========================
# Prozor/Mapiranje — PRIJAVE (dolazak)
# =========================
def build_windows_for_time_login(raspored: pd.DataFrame, hhmm: str) -> pd.DataFrame:
    """[termin - WINDOW_BEFORE_MIN, termin + WINDOW_AFTER_MIN]"""
    if raspored is None or raspored.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])
    r = raspored.copy()
    r["termin"] = pd.to_datetime(r["termin"])
    r = r[r["termin"].dt.strftime("%H:%M") == hhmm].copy()
    if r.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])
    r["window_start"] = r["termin"] - pd.Timedelta(minutes=WINDOW_BEFORE_MIN)
    r["window_end"]   = r["termin"] + pd.Timedelta(minutes=WINDOW_AFTER_MIN)
    r = r.drop_duplicates(subset=["ucionica","termin"])
    return r[["ucionica","termin","window_start","window_end"]]

# =========================
# Prozor/Mapiranje — ODJAVE (odlazak)
# =========================

def build_windows_for_time_logout(raspored: pd.DataFrame, hhmm: str, d: date) -> pd.DataFrame:
    """
    Za odabrani HH:MM radi prozor [T, T_next), gdje je T_next prvi sljedeći
    termin tog dana (globalno). Ako ga nema, do kraja dana.
    Vraća po jedan red za svaku učionicu koja ima termin u T.
    """
    if raspored is None or raspored.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])

    r = raspored.copy()
    r["termin"] = pd.to_datetime(r["termin"])

    # svi termini tog sata (može ih biti više po učionici)
    r_sel = r[r["termin"].dt.strftime("%H:%M") == hhmm].copy()
    if r_sel.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])

    # početak T je najraniji termin s tim HH:MM (ako su sekunde različite)
    start_ts = r_sel["termin"].min()

    # globalni sljedeći termin u danu
    all_times = sorted(r["termin"].unique().tolist())
    next_ts = next((t for t in all_times if t > start_ts), None)

    day_end = datetime.combine(d, dtime(23, 59, 59))
    end_ts = next_ts if next_ts is not None else day_end

    rooms = r_sel[["ucionica"]].drop_duplicates().copy()
    rooms["termin"] = start_ts
    rooms["window_start"] = start_ts
    rooms["window_end"] = end_ts
    return rooms[["ucionica","termin","window_start","window_end"]]


def assign_logs_to_windows(log_df: pd.DataFrame, windows: pd.DataFrame, kartice: pd.DataFrame) -> pd.DataFrame:
    """
    Spajanje po učionici + vremenskom prozoru. Radi i za prijave i za odjave.
    Vraća: ucionica, vrijeme, broj_kartice, cuvar, termin
    """
    if log_df is None or log_df.empty or windows is None or windows.empty:
        return pd.DataFrame(columns=["ucionica","vrijeme","broj_kartice","cuvar","termin"])

    df = log_df.copy()
    k  = kartice.copy()

    df = df.merge(k, on="uid_kartice", how="left")
    j  = df.merge(windows, on="ucionica", how="inner")

    m = (j["time"] >= j["window_start"]) & (j["time"] < j["window_end"])
    j = j[m].copy()

    j["delta"] = (j["time"] - j["termin"]).abs()
    j = j.sort_values(["time","ucionica","delta"]).drop_duplicates(
        subset=["time","ucionica","uid_kartice"], keep="first"
    )

    out = j.rename(columns={"time": "vrijeme", "uid_kartice": "broj_kartice"})[
        ["ucionica","vrijeme","broj_kartice","cuvar","termin"]
    ]
    return out.sort_values(["ucionica","vrijeme"])

# =========================
# Utility – sortiranje, zebra
# =========================
def sort_rooms_natural(df: pd.DataFrame, col: str = "ucionica", extra_order: list[str] | None = None) -> pd.DataFrame:
    if df is None or df.empty or col not in df.columns:
        return df
    work = df.copy()
    pref = work[col].astype(str).str.extract(r"^([A-Za-zČĆŽŠĐ]+)", expand=False).fillna("")
    num  = work[col].astype(str).str.extract(r"(\d+)", expand=False)
    num  = pd.to_numeric(num, errors="coerce").fillna(0).astype(int)
    work["__pref"] = pref; work["__num"] = num
    sort_cols = ["__pref","__num"] + (extra_order or []) + [col]
    work = work.sort_values(sort_cols).drop(columns=["__pref","__num"])
    return work

def make_group_stripes(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    rooms_order = []
    for r in rows:
        room = r.get("ucionica")
        if room and room not in rooms_order:
            rooms_order.append(room)

    colors = ["#F6FAFF", "#FFF8F2"]
    styles = []
    for i, room in enumerate(rooms_order):
        color = colors[i % 2]
        # ✨ pre-escape izvan f-stringa (dopušteno)
        room_escaped = str(room).replace('"', '\\"')

        styles.append({
            "if": {"filter_query": f'{{ucionica}} = "{room_escaped}"'},
            "backgroundColor": color,
        })
    return styles


def _next_page(curr, total):
    if total <= 1:
        return 0
    curr = (curr or 0)
    return (curr + 1) % total

def initial_date():
    # md = fetch_min_date_in_raspored()
    return date.today() # md if md else date.today()

def is_in_login_autopage_window(d: date, hhmm: str) -> bool:
    try:
        h, m = map(int, hhmm.split(":"))
    except Exception:
        return False
    T = datetime.combine(d, dtime(h, m))
    now = datetime.now()  # ako želiš fiksnu TZ: zamijeni s now_local_naive()
    return (T - timedelta(minutes=PAGE_AUTO_IN_BEFORE_MIN) <= now < T)

def is_selected_today(d_iso: str | None) -> bool:
    if not d_iso:
        return False
    return pd.to_datetime(d_iso).date() == date.today()
    # Ako želiš striktno po zoni/bazi, zamijeni date.today() s now_local_naive().date()

# =========================
# DASH APLIKACIJA
# =========================
app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server


# Header
image_id = "1IVYXW6Ye48OeHt6Xo89gJPp7NRySHwFH"
image_url = f"https://lh3.googleusercontent.com/d/{image_id}"

app.layout = html.Div(
    [
        # HEADER
        html.Div(
            [
                html.Img(src=image_url, style={"height": "80px", "marginRight": "20px"}),
                html.H6(
                    "Ispiti - evidencija čuvara",
                    style={"color": "#ffffff", "fontWeight": "bold", "fontSize": "30px"},
                ),
            ],
            style={
                "display": "flex",
                "alignItems": "center",
                "backgroundColor": "#151515",
                "padding": "10px",
                "borderRadius": "6px",
                "height":"60px"
            },
        ),

        # GLAVNI NASLOV
        # html.H1("Evidencija čuvara", className="page-title"),

        # FILTERI
        html.Div(
            [
            html.Div(
                [
                html.Div(
                    [
                        html.Label("Datum:", className="filter-label"),
                        dcc.DatePickerSingle(
                            id="picker-datum",
                            date=initial_date(),
                            display_format="D.M.YYYY.",
                            first_day_of_week=1,
                            className="my-dropdown",
                        ),
                    ],
                    className="filter-item",
                ),
                html.Div(
                    [
                        html.Label("Termin:", className="filter-label"),
                        dcc.Dropdown(
                            id="dropdown-termin",
                            placeholder="Odaberi termin",
                            className="my-dropdown",
                            clearable=False,
                        ),
                    ],
                    className="filter-item",
                ),
                ],
                className="filter-termini",
            ),
            html.Div(
                [
                html.Div(
                    [
                        html.Label("Redaka po stranici:", className="filter-label"),
                        dcc.Dropdown(
                            id="page-size",
                            options=[{"label": str(n), "value": n} for n in (4, 10, 12, 14, 16, 18)],
                            value=12, clearable=False, className="my-dropdown",
                            ),
                    ],
                    className="filter-item",
                ),
                html.Div(
                    [
                        html.Label("Promjena stranice:", className="filter-label"),
                        dcc.Dropdown(
                            id="page-interval",
                            options=[
                                {"label": "5 s",  "value": 5_000},
                                {"label": "30 s", "value": 30_000},
                                {"label": "60 s", "value": 60_000},
                                {"label": "90 s", "value": 90_000},
                            ],
                            value=60_000, clearable=False, className="my-dropdown",
                        ),
                    ],
                    className="filter-item",
                ),
                ],
                className="filter-stranica",
            ),
            ],
            className="filter-bar",
        ),

        html.Div(id="db-status", style={"color": "#b00020", "marginBottom": 8}),

        # TABLICE (Prijave lijevo, Odjave desno)
        html.Div(
            [
                # ----- PRIJAVE -----
                html.Div(
                    [
                        html.Div(
                            [
                                html.Div(  # lijevi dio: naslov + badge
                                    [
                                    html.H3("Prijave čuvara", className="card-title"),
                                    html.Span("", 
                                            id="auto-indicator-in",
                                            className="badge-auto",
                                            title="Automatsko listanje",
                                            style={"display": "none"}),
                                    ],
                                    className="title-with-badge",
                                ),
                                html.Div(
                                    html.Span(
                                        "", id="refresh-indicator-in",
                                        className="badge-auto",
                                        title="Auto-refresh",
                                        style={"display": "none"},
                                    ),
                                    id="refresh-box-in",
                                    className="checkbox-box",
                                ),
                            ],
                            className="card-header",
                        ),
                        dash_table.DataTable(
                            id="tbl-prijave",
                            columns=[
                                {"name": "Učionica",        "id": "ucionica"},
                                {"name": "Vrijeme prijave", "id": "vrijeme_prijave"},
                                {"name": "Broj kartice",    "id": "broj_kartice"},
                                {"name": "Čuvar",           "id": "cuvar"},
                            ],
                            style_cell={"fontFamily": "Inter, system-ui", "padding": "8px", "fontSize": "16px"},
                            style_cell_conditional=[
                                {"if": {"column_id": "ucionica"},        "width": "12%"},
                                {"if": {"column_id": "vrijeme_prijave"}, "width": "33%"},
                                {"if": {"column_id": "broj_kartice"},    "width": "25%"},
                                {"if": {"column_id": "cuvar"},           "width": "30%"},
                            ],
                            style_header={
                                "backgroundColor": "#be1e67",
                                "color": "white",
                                "fontWeight": "bold",
                                "textAlign": "center",
                                "border": "1px solid #ddd",
                            },
                            css=[{"selector": ".dash-spreadsheet", "rule": "border-collapse: collapse !important;"}],
                            sort_action="native",
                            page_action="native",
                            filter_action="none",
                            style_table={"maxHeight": "70vh", "overflowY": "auto", "borderRadius": "6px"},
                            style_data_conditional=[],
                            page_current=0,
                            page_size=12,
                        ),
                    ],
                    className="card",
                ),

                # ----- ODJAVE -----
                html.Div(
                    [
                        html.Div(
                            [
                            html.Div(  # naslov + badge
                                [
                                    html.H3("Odjave čuvara", className="card-title"),
                                    html.Span("", 
                                        id="auto-indicator-out",
                                        className="badge-auto",
                                        title="Automatsko listanje",
                                        style={"display": "none"},
                                    ),
                                ],
                                className="title-with-badge",
                            ),   
                            html.Div(
                                html.Span(
                                    "", id="refresh-indicator-out",
                                    className="badge-auto",
                                    title="Auto-refresh",
                                    style={"display": "none"},
                                ),
                                id="refresh-box-out",
                                className="checkbox-box",
                            ),
                            ],
                            className="card-header",
                        ),
                        dash_table.DataTable(
                            id="tbl-odjave",
                            columns=[
                                {"name": "Učionica",        "id": "ucionica"},
                                {"name": "Vrijeme odjave",  "id": "vrijeme_odjave"},
                                {"name": "Broj kartice",    "id": "broj_kartice"},
                                {"name": "Čuvar",           "id": "cuvar"},
                            ],
                            style_cell={"fontFamily": "Inter, system-ui", "padding": "8px", "fontSize": "16px"},
                            style_cell_conditional=[
                                {"if": {"column_id": "ucionica"},        "width": "12%"},
                                {"if": {"column_id": "vrijeme_odjave"},  "width": "33%"},
                                {"if": {"column_id": "broj_kartice"},    "width": "25%"},
                                {"if": {"column_id": "cuvar"},           "width": "30%"},
                            ],
                            style_header={
                                "backgroundColor": "#be1e67",
                                "color": "white",
                                "fontWeight": "bold",
                                "textAlign": "center",
                                "border": "1px solid #ddd",
                            },
                            css=[{"selector": ".dash-spreadsheet", "rule": "border-collapse: collapse !important;"}],
                            sort_action="native",
                            page_action="native",
                            filter_action="none",
                            style_table={"maxHeight": "70vh", "overflowY": "auto", "borderRadius": "6px"},
                            style_data_conditional=[],
                            page_current=0,
                            page_size=12,
                        ),
                    ],
                    className="card",
                ),
            ],
            className="tables-row",
        ),

        # dva odvojena timera
        dcc.Interval(id="timer-in",  interval=60_000, n_intervals=0),
        dcc.Interval(id="timer-out", interval=60_000, n_intervals=0),

        dcc.Interval(id="pager-in",  interval=60_000, n_intervals=0),  
        dcc.Interval(id="pager-out", interval=60_000, n_intervals=0),

        dcc.Interval(id="pulse",     interval=60_000, n_intervals=0),

    ],
    style={"maxWidth": "1200px", "margin": "15px auto", "padding": "0 10px"},
)

# =========================
# CALLBACKS
# =========================
@app.callback(Output("db-status", "children"),
              Input("timer-in", "n_intervals"),
              Input("timer-out", "n_intervals"))
def show_db_status(_, __):
    return LAST_DB_ERROR

# Dropdown termina (HH:MM)
@app.callback(
    Output("dropdown-termin", "options"),
    Output("dropdown-termin", "value"),
    Input("picker-datum", "date"),
)
def update_termini(datum):
    if not datum:
        return [], None
    d = pd.to_datetime(datum).date()
    raspored = fetch_raspored_for_date(d)
    if raspored is None or raspored.empty:
        return [], None
    times = sorted(raspored["termin"].dt.strftime("%H:%M").unique().tolist())
    options = [{"label": t, "value": t} for t in times]
    value = "18:30" if "18:30" in times else (times[0] if times else None)
    return options, value

# Auto-refresh
@app.callback(
    Output("timer-in",  "disabled"),
    Output("timer-out", "disabled"),
    Output("refresh-indicator-in",  "children"),
    Output("refresh-indicator-in",  "style"),
    Output("refresh-indicator-out", "children"),
    Output("refresh-indicator-out", "style"),
    Output("refresh-box-in",  "style"),   # ružičasti okvir (prijave)
    Output("refresh-box-out", "style"),   # ružičasti okvir (odjave)
    Input("picker-datum", "date"),
    Input("pulse", "n_intervals"),        # ⇦ NOVO: periodična provjera
)
def auto_refresh_by_today(d_iso, _pulse):
    is_today = False
    if d_iso:
        is_today = (pd.to_datetime(d_iso).date() == date.today())

    # timere palimo samo kad je danas
    disabled_in  = not is_today
    disabled_out = not is_today

    # badge tekst + vidljivost
    text        = "AUTO-REFRESH" if is_today else ""
    badge_style = {"display": "inline-flex"} if is_today else {"display": "none"}

    # cijeli rozi okvir: prikaži samo kad je danas
    box_style = {"display": "flex"} if is_today else {"display": "none"}

    return (
        disabled_in, disabled_out,
        text, badge_style,
        text, badge_style,
        box_style, box_style,
    )


# --- PRIJAVE (lijeva tablica) ---
@app.callback(
    Output("tbl-prijave", "data"),
    Output("tbl-prijave", "style_data_conditional"),
    Input("timer-in", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
    prevent_initial_call=False,
)
def refresh_logins(_, datum, hhmm):
    if not datum or not hhmm:
        return [], []
    d = pd.to_datetime(datum).date()

    raspored = fetch_raspored_for_date(d)
    if raspored is None or raspored.empty:
        return [], []

    # sve učionice za taj sat
    rooms = (raspored.assign(termin_hhmm=raspored["termin"].dt.strftime("%H:%M"))
                     .loc[lambda x: x["termin_hhmm"] == hhmm, ["ucionica"]]
                     .drop_duplicates())
    # prozori za prijave
    windows = build_windows_for_time_login(raspored, hhmm)

    logins  = fetch_login_log_for_date(d)
    kartice = fetch_kartice()
    assigned = assign_logs_to_windows(logins, windows, kartice)

    if not assigned.empty:
        assigned = assigned.copy()
        assigned["vrijeme"] = pd.to_datetime(assigned["vrijeme"]).dt.strftime("%d.%m.%Y. %H:%M:%S")
        merged = rooms.merge(
            assigned.rename(columns={"vrijeme":"vrijeme_prijave"})[
                ["ucionica","vrijeme_prijave","broj_kartice","cuvar"]
            ],
            on="ucionica", how="left"
        )
    else:
        merged = rooms.copy()
        merged["vrijeme_prijave"] = None
        merged["broj_kartice"]    = None
        merged["cuvar"]           = None

    merged[["vrijeme_prijave","broj_kartice","cuvar"]] = merged[
        ["vrijeme_prijave","broj_kartice","cuvar"]
    ].fillna("—")

    out_df = sort_rooms_natural(
        merged[["ucionica","vrijeme_prijave","broj_kartice","cuvar"]],
        col="ucionica", extra_order=["vrijeme_prijave"]
    )
    recs = out_df.to_dict("records")
    return recs, make_group_stripes(recs)

# --- ODJAVE (desna tablica) ---
@app.callback(
    Output("tbl-odjave", "data"),
    Output("tbl-odjave", "style_data_conditional"),
    Input("timer-out", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
    prevent_initial_call=False,
)
def refresh_logouts(_n, datum, hhmm):
    # uvijek vrati 2 vrijednosti (data, style)
    if not datum or not hhmm:
        return [], []

    d = pd.to_datetime(datum).date()

    # raspored za dan
    raspored = fetch_raspored_for_date(d)
    if raspored is None or raspored.empty:
        return [], []

    # sve učionice koje imaju termin u odabranom satu (da se prikazuju i bez odjave)
    rooms = (
        raspored.assign(termin_hhmm=raspored["termin"].dt.strftime("%H:%M"))
                .loc[lambda x: x["termin_hhmm"] == hhmm, ["ucionica"]]
                .drop_duplicates()
    )

    # prozori za odjave: [T, prvi sljedeći termin u danu), po SVIM učionicama tog termina
    windows = build_windows_for_time_logout(raspored, hhmm, d)

    # odjave (state = 0) i mapiranje u prozore
    logouts = fetch_logout_log_for_date(d)
    kartice = fetch_kartice()
    assigned = assign_logs_to_windows(logouts, windows, kartice)

    # merge da zadržimo sve učionice; formatiranje vremena
    if assigned is not None and not assigned.empty:
        assigned = assigned.copy()
        assigned["vrijeme"] = pd.to_datetime(assigned["vrijeme"]).dt.strftime("%d.%m.%Y. %H:%M:%S")
        merged = rooms.merge(
            assigned.rename(columns={"vrijeme": "vrijeme_odjave"})[
                ["ucionica", "vrijeme_odjave", "broj_kartice", "cuvar"]
            ],
            on="ucionica", how="left",
        )
    else:
        merged = rooms.copy()
        merged["vrijeme_odjave"] = None
        merged["broj_kartice"]   = None
        merged["cuvar"]          = None

    merged[["vrijeme_odjave", "broj_kartice", "cuvar"]] = (
        merged[["vrijeme_odjave", "broj_kartice", "cuvar"]].fillna("—")
    )

    # prirodni poredak učionica + zebra po učionici
    out_df = sort_rooms_natural(
        merged[["ucionica", "vrijeme_odjave", "broj_kartice", "cuvar"]],
        col="ucionica", extra_order=["vrijeme_odjave"]
    )
    recs = out_df.to_dict("records")
    stripes = make_group_stripes(recs)
    return recs, stripes

@app.callback(
    Output("tbl-prijave", "page_current"),
    Input("pager-in", "n_intervals"),        # tik-tak za paging
    Input("picker-datum", "date"),           # reset na promjenu filtera
    Input("dropdown-termin", "value"),
    Input("page-size", "value"),
    State("tbl-prijave", "data"),
    State("tbl-prijave", "page_size"),
    State("tbl-prijave", "page_current"),
)
def rotate_pages_in(_tick, d_iso, hhmm, page_size_ctrl, data, page_size_tbl, curr):
    ctx = dash.callback_context
    # reset na prvu stranicu kad korisnik promijeni datum/termin/veličinu stranice
    if ctx.triggered:
        src = ctx.triggered[0]["prop_id"].split(".")[0]
        if src in ("picker-datum", "dropdown-termin", "page-size"):
            return 0

    # ako nemamo filtere, ili NISMO u [T-60, T) → ne rotiraj (ostavi trenutačnu stranicu)
    if not d_iso or not hhmm:
        return curr or 0
    d = pd.to_datetime(d_iso).date()
    if not is_in_login_autopage_window(d, hhmm):
        return curr or 0

    # u prozoru rotiraj
    n  = len(data or [])
    ps = page_size_tbl or page_size_ctrl or 12
    total = max(1, (n + ps - 1) // ps)
    return _next_page(curr, total)

@app.callback(
    Output("tbl-odjave", "page_current"),
    Input("pager-out", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
    Input("page-size", "value"),
    State("tbl-odjave", "data"),
    State("tbl-odjave", "page_size"),
    State("tbl-odjave", "page_current"),
)
def rotate_pages_out(_tick, d_iso, hhmm, page_size_ctrl, data, page_size_tbl, curr):
    ctx = dash.callback_context
    # reset na promjenu filtera / veličine stranice
    if ctx.triggered:
        src = ctx.triggered[0]["prop_id"].split(".")[0]
        if src in ("picker-datum", "dropdown-termin", "page-size"):
            return 0

    # auto-rotiraj samo ako je odabran današnji datum
    if not is_selected_today(d_iso):
        return curr or 0

    n  = len(data or [])
    ps = page_size_tbl or page_size_ctrl or 12
    total = max(1, (n + ps - 1) // ps)
    return _next_page(curr, total)

@app.callback(
    Output("tbl-prijave", "page_size"),
    Output("tbl-odjave", "page_size"),
    Input("page-size", "value"),
)
def set_page_size(n):
    return int(n or 12), int(n or 12)

@app.callback(
    Output("pager-in", "interval"),
    Output("pager-out", "interval"),
    Input("page-interval", "value"),
)
def set_pager_interval(ms):
    v = int(ms or 8_000)
    return v, v

@app.callback(
    Output("auto-indicator-in", "children"),
    Output("auto-indicator-in", "style"),
    Input("pager-in", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
)
def show_auto_badge(_tick, d_iso, hhmm):
    if not d_iso or not hhmm:
        return "", {"display": "none"}
    d = pd.to_datetime(d_iso).date()
    on = is_in_login_autopage_window(d, hhmm)
    # tekst "AUTO" + pulsirajuća točkica (definirana u CSS-u ::after)
    return ("AUTO", {"display": "inline-flex"}) if on else ("", {"display": "none"})

@app.callback(
    Output("auto-indicator-out", "children"),
    Output("auto-indicator-out", "style"),
    Input("pager-out", "n_intervals"),
    Input("picker-datum", "date"),
)
def show_auto_badge_out(_tick, d_iso):
    on = is_selected_today(d_iso)
    return ("AUTO", {"display": "inline-flex"}) if on else ("", {"display": "none"})

if __name__ == "__main__":
    import os
    DEBUG = os.getenv("DEBUG", "0") == "1"
    PORT = int(os.getenv("PORT", "8050"))
    app.run_server(host="0.0.0.0", port=PORT, debug=DEBUG)
