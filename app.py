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


# --- HEADER & UI LAYOUT ---
image_id = "1IVYXW6Ye48OeHt6Xo89gJPp7NRySHwFH"
image_url = f"https://lh3.googleusercontent.com/d/{image_id}"

# Window oko termina
WINDOW_BEFORE_MIN = 60   # koliko minuta prije termina
WINDOW_AFTER_MIN  = 30   # koliko minuta poslije termina


# =========================
# UNIVERZALNI DOHVAT (pytds) — koristi %s placeholdere
# =========================
def fetch_data_from_db(query: str, params=None) -> pd.DataFrame | None:
    """
    Dohvaća DataFrame iz SQL Servera koristeći python-tds (bez ODBC).
    """
    global brojac, LAST_DB_ERROR, PREFERRED_TDS
    # Kandidati za TDS: prvo preferirani (74), pa 73, pa 72, pa auto
    tds_candidates = [PREFERRED_TDS, TDS73, TDS72, None]

    for ver in tds_candidates:
        try:
            kw = dict(port=DB_PORT, login_timeout=5, timeout=10, autocommit=True)
            if ver is not None:
                kw["tds_version"] = ver
            with pytds.connect(DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, **kw) as conn:
                df = pd.read_sql(query, conn, params=params)
            brojac += 1
            LAST_DB_ERROR = ""
            # zapamti prvu uspješnu TDS verziju (ako je bila auto, ne mijenjamo)
            if ver is not None:
                PREFERRED_TDS = ver
            #print(f"Broj spajanja na SQL: {brojac} (TDS {ver or 'auto'})")
            return df

        except Exception as e:
            LAST_DB_ERROR = f"{type(e).__name__}: {e}"
            # probaj sljedeći ver
            continue

    print("⛔ Greška u konekciji/SQL:", LAST_DB_ERROR)
    return None

# =========================
# SQL UPITI
# =========================

def rooms_for_time(raspored: pd.DataFrame, hhmm: str) -> pd.DataFrame:
    """
    Sve učionice iz rasporeda za točno zadani HH:MM.
    Vraća ucionica (za prikaz), room_key (za spajanje), termin_hhmm.
    """
    if raspored is None or raspored.empty:
        return pd.DataFrame(columns=["ucionica", "room_key", "termin_hhmm"])

    r = raspored.copy()
    r["termin_hhmm"] = r["termin"].dt.strftime("%H:%M")
    out = (
        r.loc[r["termin_hhmm"] == hhmm, ["ucionica", "room_key", "termin_hhmm"]]
         .drop_duplicates(subset=["room_key"])
    )

    # ⇩ prirodni poredak učionica
    out = sort_rooms_natural(out, col="ucionica")

    return out


def _norm_room(series: pd.Series) -> pd.Series:
    # " C4 " -> "C4", "d10" -> "D10", ukloni višestruke razmake i non-ASCII
    return (series.astype(str)
                  .str.strip()
                  .str.upper()
                  .str.replace(r"\s+", "", regex=True))

def fetch_raspored_for_date(d: date) -> pd.DataFrame:
    q = """
        SELECT
            termin,
            [učionica] AS ucionica,
            [state]
        FROM dbo.ispiti_raspored
        WHERE [state] = 1
          AND CONVERT(date, termin) = CONVERT(date, %s)
    """
    df = fetch_data_from_db(q, params=[d])
    if df is None:
        return pd.DataFrame(columns=["termin","ucionica","state"])
    if df.empty:
        return df

    df["termin"]   = pd.to_datetime(df["termin"])
    df["ucionica"] = _norm_room(df["ucionica"])
    return df

def fetch_log_for_date(d: date) -> pd.DataFrame:
    start = datetime.combine(d, datetime.min.time())
    end   = datetime.combine(d, datetime.max.time())
    q = """
        SELECT
            [time],
            [card_no]     AS uid_kartice,
            [device_name] AS ucionica,
            [state]
        FROM dbo.acc_monitor_log
        WHERE [state] = 1
          AND [time] >= %s AND [time] <= %s
    """
    df = fetch_data_from_db(q, params=[start, end])
    if df is None:
        return pd.DataFrame(columns=["time","uid_kartice","ucionica","state"])
    if df.empty:
        return df

    df["time"]        = pd.to_datetime(df["time"])
    df["ucionica"]    = _norm_room(df["ucionica"])
    df["uid_kartice"] = df["uid_kartice"].astype(str).str.strip()
    return df

def fetch_kartice() -> pd.DataFrame:
    q = """
        SELECT
            [Čuvar]       AS cuvar,
            [UID kartice] AS uid_kartice
        FROM dbo.cuvari_kartice
    """
    df = fetch_data_from_db(q)
    if df is None:
        return pd.DataFrame(columns=["cuvar","uid_kartice"])
    if df.empty:
        return df

    df["uid_kartice"] = df["uid_kartice"].astype(str).str.strip()
    return df




def fetch_min_date_in_raspored() -> date | None:
    q = "SELECT MIN(CONVERT(date, termin)) AS d FROM dbo.ispiti_raspored WHERE [state]=1"
    df = fetch_data_from_db(q)
    if df is not None and not df.empty and pd.notna(df.iloc[0]["d"]):
        return pd.to_datetime(df.iloc[0]["d"]).date()
    return None

# =========================
# MAPIRANJE PRIJAVA NA TERMINE 
# =========================

def assign_logs_to_terms(log_df: pd.DataFrame, windows: pd.DataFrame, kartice: pd.DataFrame) -> pd.DataFrame:
    """
    Spaja prijave iz log_df na prozore u 'windows' po učionici i vremenskom intervalu.
    Vraća: ucionica, vrijeme_prijave, broj_kartice, cuvar, termin
    """
    if log_df is None or log_df.empty or windows is None or windows.empty:
        return pd.DataFrame(columns=["ucionica","vrijeme_prijave","broj_kartice","cuvar","termin"])

    df = log_df.copy()
    k  = kartice.copy()

    # kartica -> čuvar
    df = df.merge(k, on="uid_kartice", how="left")

    # učionica + vrijeme u prozoru
    j = df.merge(windows, on="ucionica", how="inner")

    m = (j["time"] >= j["window_start"]) & (j["time"] < j["window_end"])
    j = j[m].copy()

    # ako bi se više termina diralo prozorima, uzmi najbliži termin
    j["delta"] = (j["time"] - j["termin"]).abs()
    j = j.sort_values(["time","ucionica","delta"]).drop_duplicates(
        subset=["time","ucionica","uid_kartice"], keep="first"
    )

    out = j.rename(columns={
        "time": "vrijeme_prijave",
        "uid_kartice": "broj_kartice",
    })[["ucionica","vrijeme_prijave","broj_kartice","cuvar","termin"]]

    return out.sort_values(["ucionica","vrijeme_prijave"])



def build_windows_for_time(raspored: pd.DataFrame, hhmm: str) -> pd.DataFrame:
    """
    Za sve termine čije vrijeme == hhmm, napravi prozor [termin-2h, termin+2h]
    i vrati (ucionica, termin, window_start, window_end).
    """
    if raspored is None or raspored.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])

    r = raspored.copy()
    r["termin"] = pd.to_datetime(r["termin"])
    r = r[r["termin"].dt.strftime("%H:%M") == hhmm].copy()
    if r.empty:
        return pd.DataFrame(columns=["ucionica","termin","window_start","window_end"])

    r["window_start"] = r["termin"] - pd.Timedelta(minutes=WINDOW_BEFORE_MIN)
    r["window_end"]   = r["termin"] + pd.Timedelta(minutes=WINDOW_AFTER_MIN)
    
    # (ako ima duplikata po učionici, makni)
    r = r.drop_duplicates(subset=["ucionica","termin"])
    return r[["ucionica","termin","window_start","window_end"]]


def sort_rooms_natural(df: pd.DataFrame, col: str = "ucionica", extra_order: list[str] | None = None) -> pd.DataFrame:
    """
    Sortira učionice prirodno: npr. C6 prije C10.
    extra_order: dodatne kolone za sekundarno sortiranje (npr. ["vrijeme_prijave"])
    """
    if df is None or df.empty or col not in df.columns:
        return df

    work = df.copy()
    # prefiks slova + broj (npr. "C", 6) iz "C6", "D10", "A101"...
    pref = work[col].astype(str).str.extract(r"^([A-Za-zČĆŽŠĐ]+)", expand=False).fillna("")
    num  = work[col].astype(str).str.extract(r"(\d+)", expand=False)
    num  = pd.to_numeric(num, errors="coerce").fillna(0).astype(int)

    work["__pref"] = pref
    work["__num"]  = num

    sort_cols = ["__pref", "__num"]
    if extra_order:
        sort_cols += extra_order
    # stabilno: ako dvije učionice imaju isti ključ, razveži originalnim nazivom
    sort_cols += [col]

    work = work.sort_values(sort_cols).drop(columns=["__pref", "__num"])
    return work

def make_group_stripes(rows: list[dict]) -> list[dict]:
    """
    Vraća style_data_conditional za izmjenične boje po učionici.
    Boja je ista za sve retke iste učionice.
    """
    if not rows:
        return []

    # redoslijed učionica po trenutnim redovima
    rooms_order = []
    for r in rows:
        room = r.get("ucionica")
        if room and room not in rooms_order:
            rooms_order.append(room)

    colors = ["#F6FAFF", "#FFF8F2"]  # svijetle nijanse, možeš promijeniti po želji
    styles = []
    for i, room in enumerate(rooms_order):
        color = colors[i % 2]
        # filter_query sintaksa: {kolona} = "vrijednost"
        room_escaped = str(room).replace('"', '\\"')
        styles.append({
            "if": {"filter_query": f'{{ucionica}} = "{room_escaped}"'},
            "backgroundColor": color,
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

# --- HEADER & UI LAYOUT ---
image_id = "1IVYXW6Ye48OeHt6Xo89gJPp7NRySHwFH"
image_url = f"https://lh3.googleusercontent.com/d/{image_id}"

app.layout = html.Div(
    [
        # HEADER s logom
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
            },
        ),

        # GLAVNI NASLOV
        html.H1("Prijave čuvara", className="page-title"),

        # FILTER TRAKA
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
                # ⇩ NOVO: Auto-refresh toggle (desno)
                html.Div(
                    [
                    html.Label(" ", className="filter-label", style={"visibility": "hidden"}),  # držač visine kao i ostali
                    html.Div(
                        dcc.Checklist(
                            id="auto-refresh",
                            options=[{"label": " Auto-refresh", "value": "on"}],
                            value=["on"],                           # uključen po defaultu
                            className="check-control",
                        ),
                        className="checkbox-box",                  # okvir kao na dropdownu
                    ),
                    ],
                    className="filter-item",
                    style={"marginLeft": "auto"},
                ),        
            ],
            className="filter-bar",
        ),

        # STATUS DB (po želji ostavi/ukloni)
        html.Div(id="db-status", style={"color": "#b00020", "marginBottom": 8}),

        # TABLICA
        dash_table.DataTable(
            id="tbl-prijave",
            columns=[
                {"name": "Učionica",        "id": "ucionica"},
                {"name": "Vrijeme prijave", "id": "vrijeme_prijave"},
                {"name": "Broj kartice",    "id": "broj_kartice"},
                {"name": "Čuvar",           "id": "cuvar"},
            ],
            style_cell={"fontFamily": "Inter, system-ui", "padding": "8px", "fontSize": "16px"},
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
            style_data_conditional=[],  # zebra boje puni tvoj callback
        ),

        dcc.Interval(id="timer", interval=5_000, n_intervals=0),
        dcc.Store(id="store-datum"),
    ],
    style={"maxWidth": "1100px", "margin": "30px auto", "padding": "0 10px"},
)


# status konekcije (prikazuje zadnju grešku ako je ima)
@app.callback(Output("db-status", "children"), Input("timer", "n_intervals"))
def show_db_status(_):
    return LAST_DB_ERROR


# 1) Popuna dropdowna termina – samo unikatni sati (HH:MM), bez učionica
@app.callback(
    Output("dropdown-termin", "options"),
    Output("dropdown-termin", "value"),
    Output("store-datum", "data"),
    Input("picker-datum", "date"),
)
def update_termini(datum):
    if not datum:
        return [], None, None
    d = pd.to_datetime(datum).date()
    raspored = fetch_raspored_for_date(d)
    if raspored is None or raspored.empty:
        return [], None, d.isoformat()

    times = sorted(raspored["termin"].dt.strftime("%H:%M").unique().tolist())
    options = [{"label": t, "value": t} for t in times]
    value = "18:30" if "18:30" in times else (times[0] if times else None)
    return options, value, d.isoformat()


# Uključi/Isključi automatsko osvježavanje
@app.callback(
    Output("timer", "disabled"),
    Input("auto-refresh", "value"),
)
def toggle_interval(value):
    # kad 'on' NIJE u vrijednostima -> onemogući interval
    return "on" not in (value or [])


@app.callback(
    Output("tbl-prijave", "data"),
    Output("tbl-prijave", "style_data_conditional"),
    # ⇩ Dodaj ova dva inputa uz postojeći timer
    Input("timer", "n_intervals"),
    Input("picker-datum", "date"),
    Input("dropdown-termin", "value"),
    Input("auto-refresh", "value"),   # da okine odmah kad upališ/ugasiš
    prevent_initial_call=False,
)
def refresh_table(_, datum, selected_hhmm, auto):
    # zaštite
    if not datum or not selected_hhmm:
        return [], []

    d = pd.to_datetime(datum).date()

    # 1) raspored i učionice za taj sat
    raspored = fetch_raspored_for_date(d)
    if raspored is None or raspored.empty:
        return [], []

    rooms = (
        raspored.assign(termin_hhmm=raspored["termin"].dt.strftime("%H:%M"))
                .loc[lambda x: x["termin_hhmm"] == selected_hhmm, ["ucionica"]]
                .drop_duplicates()
    )

    # 2) prozori (T-60 min, T+30 min – tvoje postavke)
    windows_sel = build_windows_for_time(raspored, selected_hhmm)

    # 3) log + kartice → assign
    log     = fetch_log_for_date(d)
    kartice = fetch_kartice()
    assigned = assign_logs_to_terms(log, windows_sel, kartice)

    # 4) merge – zadrži sve učionice, čak i bez prijave
    if not assigned.empty:
        assigned = assigned.copy()
        assigned["vrijeme_prijave"] = pd.to_datetime(assigned["vrijeme_prijave"]).dt.strftime("%d.%m.%Y. %H:%M:%S")
        merged = rooms.merge(
            assigned[["ucionica","vrijeme_prijave","broj_kartice","cuvar"]],
            on="ucionica", how="left"
        )
    else:
        merged = rooms.copy()
        merged["vrijeme_prijave"] = None
        merged["broj_kartice"]    = None
        merged["cuvar"]           = None

    merged[["vrijeme_prijave","broj_kartice","cuvar"]] = (
        merged[["vrijeme_prijave","broj_kartice","cuvar"]].fillna("—")
    )

    out_df = sort_rooms_natural(
        merged[["ucionica","vrijeme_prijave","broj_kartice","cuvar"]],
        col="ucionica",
        extra_order=["vrijeme_prijave"]
    )
    out_records = out_df.to_dict("records")

    stripes = make_group_stripes(out_records)
    return out_records, stripes



if __name__ == "__main__":
    DEBUG = os.getenv("DEBUG", "0") == "1"
    PORT = int(os.getenv("PORT", "8050"))
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)

