# app.py
import os
import pandas as pd
from datetime import datetime, date

import pytds
from pytds.tds_base import TDS74, TDS73, TDS72  # TDS verzije (74 radi kod tebe)
import dash
from dash import Dash, dcc, html, dash_table, Input, Output, State
import warnings

import re
from datetime import datetime, date

# =========================
# KONFIG — prilagodi po potrebi ili preko env varijabli
# =========================
DB_HOST     = os.getenv("DB_HOST")
DB_PORT     = int(os.getenv("DB_PORT", "1433"))
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")  # bez defaulta!

# Ako želiš prisiliti TDS kroz ENV: DB_TDS=74/73/72
_env_tds = os.getenv("DB_TDS", "").strip()
if _env_tds == "74":
    PREFERRED_TDS = TDS74
elif _env_tds == "73":
    PREFERRED_TDS = TDS73
elif _env_tds == "72":
    PREFERRED_TDS = TDS72
else:
    PREFERRED_TDS = TDS74  # po defaultu stavljam 7.4 jer ti radi

warnings.filterwarnings(
    "ignore",
    message=r"pandas only supports SQLAlchemy connectable.*",
    category=UserWarning,
)

# Debug info
brojac = 0
LAST_DB_ERROR = ""

# Window oko termina
WINDOW_BEFORE_MIN = 60   # koliko minuta prije termina
WINDOW_AFTER_MIN  = 30   # koliko minuta poslije termina


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
    if not rows: return []
    rooms_order, seen = [], set()
    for r in rows:
        rm = r.get("ucionica")
        if rm and rm not in seen:
            seen.add(rm); rooms_order.append(rm)
    colors = ["#F6FAFF", "#FFF8F2"]
    styles = []
    for i, room in enumerate(rooms_order):
        styles.append({
            "if": {"filter_query": f'{{ucionica}} = "{str(room).replace("\"","\\\"")}"'},
            "backgroundColor": colors[i % 2],
        })
    return styles

# =========================
# DASH APLIKACIJA
# =========================
app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server

def initial_date():
    md = fetch_min_date_in_raspored()
    return md if md else date.today()

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
        html.H1("Evidencija čuvara", className="page-title"),

        # FILTERI
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
                                html.H3("Prijave čuvara", className="card-title"),
                                html.Div(
                                    dcc.Checklist(
                                        id="auto-refresh-in",
                                        options=[{"label": " Auto-refresh", "value": "on"}],
                                        value=[],
                                        className="check-control",
                                    ),
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
                            page_action="none",
                            filter_action="none",
                            style_table={"maxHeight": "70vh", "overflowY": "auto", "borderRadius": "6px"},
                            style_data_conditional=[],
                        ),
                    ],
                    className="card",
                ),

                # ----- ODJAVE -----
                html.Div(
                    [
                        html.Div(
                            [
                                html.H3("Odjave čuvara", className="card-title"),
                                html.Div(
                                    dcc.Checklist(
                                        id="auto-refresh-out",
                                        options=[{"label": " Auto-refresh", "value": "on"}],
                                        value=[],
                                        className="check-control",
                                    ),
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
                            page_action="none",
                            filter_action="none",
                            style_table={"maxHeight": "70vh", "overflowY": "auto", "borderRadius": "6px"},
                            style_data_conditional=[],
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

# Toggles
@app.callback(Output("timer-in", "disabled"),  Input("auto-refresh-in", "value"))
def toggle_in(v):  return "on" not in (v or [])

@app.callback(Output("timer-out","disabled"),  Input("auto-refresh-out","value"))
def toggle_out(v): return "on" not in (v or [])

# --- PRIJAVE (lijeva tablica) ---
@app.callback(
    Output("tbl-prijave", "data"),
    Output("tbl-prijave", "style_data_conditional"),
    Input("timer-in", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
    Input("auto-refresh-in", "value"),
    prevent_initial_call=False,
)
def refresh_logins(_, datum, hhmm, _auto):
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
    Input("auto-refresh-out", "value"),
    prevent_initial_call=False,
)
def refresh_logouts(_n, datum, hhmm, _auto):
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


if __name__ == "__main__":
    DEBUG = os.getenv("DEBUG", "0") == "1"
    PORT = int(os.getenv("PORT", "8050"))
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)

