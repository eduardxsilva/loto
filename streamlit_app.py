from __future__ import annotations

import io
import json
import math
import re
import zipfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    from scipy.stats import chisquare
except Exception:  # pragma: no cover
    chisquare = None


# =========================================================
# CONFIGURAÇÃO GERAL
# =========================================================

st.set_page_config(
    page_title="Lotofácil Analytics Pro v3.1",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

CAIXA_DOWNLOAD_URLS = [
    "https://servicebus2.caixa.gov.br/portaldeloterias/api/resultados/download?modalidade=Lotof%C3%A1cil",
    "https://servicebus2.caixa.gov.br/portaldeloterias/api/resultados/download?modalidade=Lotofacil",
    "https://servicebus2.caixa.gov.br/portaldeloterias/api/resultados/download?modalidade=lotofacil",
]

CAIXA_LOTOFACIL_PAGE = "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/zip,text/csv,text/plain,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

DEZENA_COLS = [f"dezena_{i:02d}" for i in range(1, 16)]
ALL_NUMBERS = list(range(1, 26))
EXPECTED_P_NUMBER = 15 / 25


st.markdown(
    """
    <style>
        .main .block-container {padding-top: 1.25rem; padding-bottom: 2.5rem;}
        h1, h2, h3 {letter-spacing: -0.025em;}
        .app-card {
            background: linear-gradient(135deg, rgba(15,23,42,0.97), rgba(30,41,59,0.93));
            border: 1px solid rgba(148,163,184,0.22);
            border-radius: 18px;
            padding: 18px 20px;
            box-shadow: 0 14px 34px rgba(0,0,0,0.23);
        }
        .warning-card {
            background: rgba(245, 158, 11, 0.10);
            border: 1px solid rgba(245, 158, 11, 0.35);
            padding: 14px 16px;
            border-radius: 14px;
        }
        .good-card {
            background: rgba(16, 185, 129, 0.10);
            border: 1px solid rgba(16, 185, 129, 0.30);
            padding: 14px 16px;
            border-radius: 14px;
        }
        .bad-card {
            background: rgba(239, 68, 68, 0.10);
            border: 1px solid rgba(239, 68, 68, 0.30);
            padding: 14px 16px;
            border-radius: 14px;
        }
        div[data-testid="stMetric"] {
            background: rgba(15,23,42,0.72);
            border: 1px solid rgba(148,163,184,0.18);
            padding: 14px 16px;
            border-radius: 16px;
        }
        div[data-testid="stDownloadButton"] button {border-radius: 12px; font-weight: 700;}
        .small-note {font-size: 0.88rem; color: rgba(226,232,240,0.74);}
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# FUNÇÕES UTILITÁRIAS
# =========================================================

def normalize_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def compact_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_text(value))


def safe_int(value: object) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    txt = str(value).strip()
    if not txt or txt.lower() == "nan":
        return None
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"[^0-9,.-]", "", txt)
    if not txt:
        return None
    txt = txt.replace(".", "").replace(",", ".")
    match = re.search(r"-?\d+", txt)
    if not match:
        return None
    try:
        return int(match.group())
    except Exception:
        return None


def parse_date_br(value: object) -> pd.Timestamp:
    if value is None or pd.isna(value):
        return pd.NaT

    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        if 20000 <= float(value) <= 80000:
            return pd.to_datetime(value, unit="D", origin="1899-12-30", errors="coerce")

    txt = str(value).strip()
    if not txt or txt.lower() == "nan":
        return pd.NaT

    txt = re.sub(r"\s+00:00:00$", "", txt)
    return pd.to_datetime(txt, dayfirst=True, errors="coerce")


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(str(x) for x in tup if str(x) != "nan").strip() for tup in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]
    return df


def promote_header_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    df = flatten_columns(df).dropna(how="all").dropna(axis=1, how="all").copy()
    current = " ".join(normalize_text(c) for c in df.columns)
    if "concurso" in current and ("data" in current) and ("bola" in current or "dezena" in current):
        return df

    scan_rows = min(30, len(df))
    for idx in range(scan_rows):
        row_values = [str(x).strip() for x in df.iloc[idx].tolist()]
        row_norm = " ".join(normalize_text(x) for x in row_values)
        bola_hits = len(re.findall(r"\bbola\s*0?\d{1,2}\b", row_norm))
        dez_hits = len(re.findall(r"\bdezena\s*0?\d{1,2}\b", row_norm))
        score = 0
        score += 4 if "concurso" in row_norm else 0
        score += 3 if "data" in row_norm and "sorteio" in row_norm else 0
        score += 2 if "data" in row_norm else 0
        score += bola_hits + dez_hits
        if score >= 8:
            out = df.iloc[idx + 1 :].copy()
            out.columns = row_values
            return flatten_columns(out).dropna(how="all").dropna(axis=1, how="all")
    return df


def read_json_table(data: bytes) -> Optional[pd.DataFrame]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return None

    if isinstance(obj, list):
        return pd.DataFrame(obj)

    if isinstance(obj, dict):
        candidates = []

        def walk(value, path="root"):
            if isinstance(value, list):
                candidates.append((path, value))
            elif isinstance(value, dict):
                for k, v in value.items():
                    walk(v, f"{path}.{k}")

        walk(obj)
        if candidates:
            _, best = max(candidates, key=lambda x: len(x[1]))
            return pd.DataFrame(best)
    return None


def table_quality_score(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return -1
    cols = " ".join(normalize_text(c) for c in df.columns)
    compact_cols = compact_text(cols)
    score = df.shape[0] * max(df.shape[1], 1)
    if "concurso" in cols:
        score += 100000
    if "data" in cols and "sorteio" in cols:
        score += 100000
    if "bola1" in compact_cols or "dezena1" in compact_cols:
        score += 100000
    return score


def read_any_table(data: bytes, filename: str = "") -> pd.DataFrame:
    """Lê XLSX, XLS, CSV, TSV, TXT, HTML, JSON e ZIP da série da Lotofácil."""
    name = normalize_text(filename)
    errors: list[str] = []

    # JSON direto
    if data[:1] in (b"{", b"["):
        df_json = read_json_table(data)
        if df_json is not None and not df_json.empty:
            return flatten_columns(df_json)

    # Excel direto. XLSX também é ZIP internamente; por isso vem antes do bloco ZIP.
    if name.endswith((".xlsx", ".xls", ".xlsm", ".xlsb")) or data[:2] == b"PK" or data[:8].startswith(b"\xd0\xcf\x11\xe0"):
        try:
            excel_obj = pd.ExcelFile(io.BytesIO(data))
            candidates = []
            for sheet in excel_obj.sheet_names:
                try:
                    df_sheet = pd.read_excel(excel_obj, sheet_name=sheet, dtype=object)
                    candidates.append(promote_header_if_needed(df_sheet))
                except Exception as exc:
                    errors.append(f"Excel/{sheet}: {exc}")
            if candidates:
                return flatten_columns(max(candidates, key=table_quality_score))
        except Exception as exc:
            errors.append(f"Excel direto: {exc}")

    # ZIP externo com CSV/HTML/XLS etc.
    if zipfile.is_zipfile(io.BytesIO(data)):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                candidates = []
                for inner_name in zf.namelist():
                    lower = inner_name.lower()
                    if lower.startswith("__macosx") or lower.endswith("/"):
                        continue
                    if lower.endswith((".xlsx", ".xls", ".csv", ".txt", ".htm", ".html", ".json")):
                        try:
                            df_inner = read_any_table(zf.read(inner_name), filename=inner_name)
                            candidates.append(df_inner)
                        except Exception as exc:
                            errors.append(f"ZIP/{inner_name}: {exc}")
                if candidates:
                    return flatten_columns(max(candidates, key=table_quality_score))
        except Exception as exc:
            errors.append(f"ZIP: {exc}")

    # HTML direto ou HTML disfarçado.
    for enc in ("latin1", "cp1252", "utf-8"):
        try:
            tables = pd.read_html(io.BytesIO(data), encoding=enc)
            if tables:
                tables = [promote_header_if_needed(t) for t in tables]
                return flatten_columns(max(tables, key=table_quality_score))
        except Exception as exc:
            errors.append(f"HTML/{enc}: {str(exc)[:100]}")

    # CSV/TXT/TSV.
    for enc in ("utf-8-sig", "latin1", "cp1252", "utf-8"):
        for sep in ("\t", ";", ",", "|"):
            try:
                df_csv = pd.read_csv(io.BytesIO(data), sep=sep, encoding=enc, dtype=object)
                if df_csv.shape[1] >= 10:
                    return flatten_columns(promote_header_if_needed(df_csv))
            except Exception as exc:
                errors.append(f"CSV/{enc}/{repr(sep)}: {str(exc)[:80]}")

    detail = " | ".join(errors[-8:])
    raise ValueError(
        "Não consegui ler a série histórica. Use arquivo com colunas Concurso, Data Sorteio e Bola1 até Bola15. "
        f"Detalhes técnicos: {detail}"
    )


@st.cache_data(ttl=3600, show_spinner=False)
def download_caixa_history() -> Tuple[pd.DataFrame, str]:
    last_error = None
    for url in CAIXA_DOWNLOAD_URLS:
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=60)
            resp.raise_for_status()
            content = resp.content
            if len(content) < 300:
                raise ValueError("Resposta curta demais. Provável bloqueio/endpoint indisponível.")
            disposition = resp.headers.get("content-disposition", "")
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition, flags=re.I)
            filename = match.group(1) if match else "download_caixa"
            return read_any_table(content, filename=filename), url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Falha no download automático da CAIXA. Último erro: {last_error}")


def parse_pasted_table(text: str) -> pd.DataFrame:
    text = text.strip()
    if not text:
        raise ValueError("Nada foi colado.")
    return read_any_table(text.encode("utf-8"), filename="tabela_colada.txt")


# =========================================================
# LIMPEZA E VALIDAÇÃO DA BASE
# =========================================================

def identify_lotofacil_columns(df_raw: pd.DataFrame) -> Tuple[Optional[str], Optional[str], list[str]]:
    df = flatten_columns(df_raw)
    norm = {c: normalize_text(c) for c in df.columns}
    compact = {c: compact_text(c) for c in df.columns}

    concurso_col = None
    for c in df.columns:
        if "concurso" in compact[c]:
            concurso_col = c
            break

    date_col = None
    for c in df.columns:
        cc = compact[c]
        if "datasorteio" in cc or ("data" in cc and "sorteio" in cc):
            date_col = c
            break
    if date_col is None:
        for c in df.columns:
            if "data" in compact[c]:
                date_col = c
                break

    bola_cols = []
    for c in df.columns:
        cc = compact[c]
        m = re.fullmatch(r"(?:bola|dezena|dez)0?(\d{1,2})", cc)
        if m:
            bola_cols.append((int(m.group(1)), c))
    bola_cols = [c for _, c in sorted(bola_cols, key=lambda x: x[0])]

    if len(bola_cols) < 15:
        bola_cols = []
        for c in df.columns:
            nn = norm[c]
            m = re.search(r"(?:bola|dezena|dez)\s*_?\s*0?(\d{1,2})\b", nn)
            if m:
                bola_cols.append((int(m.group(1)), c))
        bola_cols = [c for _, c in sorted(bola_cols, key=lambda x: x[0])]

    if len(bola_cols) < 15:
        candidates = []
        for c in df.columns:
            if c in (concurso_col, date_col):
                continue
            s = pd.to_numeric(
                df[c].astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False),
                errors="coerce",
            )
            valid = s.dropna()
            if len(valid) == 0:
                continue
            ratio = ((valid >= 1) & (valid <= 25)).mean()
            if ratio >= 0.85:
                candidates.append(c)
        if len(candidates) >= 15:
            bola_cols = candidates[:15]

    return concurso_col, date_col, bola_cols[:15]


def clean_lotofacil_results(df_raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = promote_header_if_needed(flatten_columns(df_raw))
    df = df.dropna(how="all").dropna(axis=1, how="all").copy()

    concurso_col, date_col, bola_cols = identify_lotofacil_columns(df)
    if not concurso_col or not date_col or len(bola_cols) < 15:
        raise ValueError(
            "Colunas obrigatórias não identificadas. O arquivo precisa ter Concurso, Data Sorteio e Bola1 até Bola15. "
            f"Encontrado: concurso={concurso_col}, data={date_col}, dezenas={len(bola_cols)}."
        )

    rows = []
    dropped = 0
    bad_reasons = Counter()
    for _, row in df.iterrows():
        concurso = safe_int(row.get(concurso_col))
        data_sorteio = parse_date_br(row.get(date_col))
        dezenas = []
        for c in bola_cols:
            n = safe_int(row.get(c))
            if n is not None and 1 <= n <= 25:
                dezenas.append(n)

        unique_dezenas = sorted(set(dezenas))
        if concurso is None:
            dropped += 1
            bad_reasons["concurso inválido"] += 1
            continue
        if pd.isna(data_sorteio):
            dropped += 1
            bad_reasons["data inválida"] += 1
            continue
        if len(unique_dezenas) != 15:
            dropped += 1
            bad_reasons["dezenas inválidas ou repetidas"] += 1
            continue

        record = {
            "concurso": concurso,
            "data_sorteio": pd.Timestamp(data_sorteio).normalize(),
        }
        for i, n in enumerate(unique_dezenas, start=1):
            record[f"dezena_{i:02d}"] = n
        record["combinacao_15"] = "-".join(f"{n:02d}" for n in unique_dezenas)
        rows.append(record)

    cleaned = pd.DataFrame(rows)
    if cleaned.empty:
        raise ValueError("Nenhuma linha válida foi encontrada após a limpeza.")

    before_dedup = len(cleaned)
    cleaned = cleaned.drop_duplicates(subset=["concurso"], keep="last")
    duplicate_concursos = before_dedup - len(cleaned)

    cleaned = cleaned.sort_values(["data_sorteio", "concurso"]).reset_index(drop=True)

    iso = cleaned["data_sorteio"].dt.isocalendar()
    cleaned["ano_iso"] = iso["year"].astype(int)
    cleaned["semana_iso_num"] = iso["week"].astype(int)
    cleaned["semana_iso"] = cleaned["ano_iso"].astype(str) + "-S" + cleaned["semana_iso_num"].astype(str).str.zfill(2)
    cleaned["semana_inicio"] = cleaned["data_sorteio"] - pd.to_timedelta(cleaned["data_sorteio"].dt.weekday, unit="D")
    cleaned["semana_fim"] = cleaned["semana_inicio"] + pd.to_timedelta(6, unit="D")
    cleaned["mes"] = cleaned["data_sorteio"].dt.to_period("M").astype(str)
    cleaned["ano"] = cleaned["data_sorteio"].dt.year.astype(int)
    cleaned["dia_semana"] = cleaned["data_sorteio"].dt.day_name(locale=None)

    meta = {
        "linhas_entrada": int(len(df)),
        "linhas_validas": int(len(cleaned)),
        "linhas_descartadas": int(dropped),
        "concursos_duplicados_removidos": int(duplicate_concursos),
        "concurso_col": str(concurso_col),
        "data_col": str(date_col),
        "bola_cols": ", ".join(str(c) for c in bola_cols),
        "motivos_descarte": dict(bad_reasons),
    }
    return cleaned, meta


def validate_clean_base(df: pd.DataFrame) -> pd.DataFrame:
    diagnostics = []
    diagnostics.append({"verificacao": "concursos válidos", "valor": len(df), "status": "ok" if len(df) > 0 else "erro"})
    diagnostics.append({"verificacao": "data mínima", "valor": str(df["data_sorteio"].min().date()), "status": "info"})
    diagnostics.append({"verificacao": "data máxima", "valor": str(df["data_sorteio"].max().date()), "status": "info"})

    dup = df["concurso"].duplicated().sum()
    diagnostics.append({"verificacao": "concursos duplicados", "valor": int(dup), "status": "ok" if dup == 0 else "atenção"})

    invalid_sets = 0
    for _, row in df[DEZENA_COLS].iterrows():
        vals = [int(x) for x in row.tolist()]
        if len(vals) != 15 or len(set(vals)) != 15 or min(vals) < 1 or max(vals) > 25:
            invalid_sets += 1
    diagnostics.append({"verificacao": "linhas com dezenas inválidas", "valor": int(invalid_sets), "status": "ok" if invalid_sets == 0 else "erro"})

    concurso_sorted = df.sort_values("concurso")
    gaps = concurso_sorted["concurso"].diff().dropna()
    missing_est = int((gaps[gaps > 1] - 1).sum()) if not gaps.empty else 0
    diagnostics.append({"verificacao": "possíveis concursos faltantes", "valor": missing_est, "status": "ok" if missing_est == 0 else "atenção"})

    non_mono_dates = int((df.sort_values("concurso")["data_sorteio"].diff().dt.days < 0).sum())
    diagnostics.append({"verificacao": "datas fora da ordem dos concursos", "valor": non_mono_dates, "status": "ok" if non_mono_dates == 0 else "atenção"})

    return pd.DataFrame(diagnostics)


# =========================================================
# ESTATÍSTICA E PROBABILIDADE
# =========================================================

def norm_two_sided_p(z: float) -> float:
    if not np.isfinite(z):
        return np.nan
    return float(math.erfc(abs(float(z)) / math.sqrt(2)))


def bh_adjust(p_values: pd.Series) -> pd.Series:
    """Benjamini-Hochberg para controle de FDR."""
    p = pd.to_numeric(p_values, errors="coerce").astype(float)
    n = p.notna().sum()
    out = pd.Series(np.nan, index=p.index, dtype=float)
    if n == 0:
        return out
    valid = p.dropna().sort_values()
    ranks = np.arange(1, len(valid) + 1)
    adj = valid.values * len(valid) / ranks
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    adj = np.clip(adj, 0, 1)
    out.loc[valid.index] = adj
    return out


def binomial_z_score(observed: float, draws: int, probability: float) -> float:
    expected = draws * probability
    variance = draws * probability * (1 - probability)
    if variance <= 0:
        return 0.0
    return float((observed - expected) / math.sqrt(variance))


def probability_combo(k: int) -> float:
    if not 1 <= k <= 15:
        return 0.0
    return math.comb(25 - k, 15 - k) / math.comb(25, 15)


def hypergeom_match_probability(hits: int) -> float:
    """Probabilidade de um jogo fixo de 15 números acertar exatamente hits em um sorteio de 15 entre 25."""
    if hits < 0 or hits > 15:
        return 0.0
    if 15 - hits > 10:
        return 0.0
    return math.comb(15, hits) * math.comb(10, 15 - hits) / math.comb(25, 15)


def theoretical_probabilities() -> pd.DataFrame:
    rows = []
    for hits in range(5, 16):
        p = hypergeom_match_probability(hits)
        rows.append(
            {
                "acertos": hits,
                "probabilidade_exata": p,
                "1_em_aproximadamente": round(1 / p, 2) if p > 0 else np.nan,
                "probabilidade_acumulada_maior_igual": sum(hypergeom_match_probability(h) for h in range(hits, 16)),
            }
        )
    out = pd.DataFrame(rows).sort_values("acertos", ascending=False)
    out["observacao"] = np.where(out["acertos"].isin([11, 12, 13, 14, 15]), "faixa típica de premiação", "sem prêmio usual")
    return out


def number_long_table(df: pd.DataFrame) -> pd.DataFrame:
    long_df = df[["concurso", "data_sorteio", "semana_iso", "semana_inicio", "semana_fim", "mes", "ano"] + DEZENA_COLS].melt(
        id_vars=["concurso", "data_sorteio", "semana_iso", "semana_inicio", "semana_fim", "mes", "ano"],
        value_vars=DEZENA_COLS,
        value_name="dezena",
    )
    long_df["dezena"] = long_df["dezena"].astype(int)
    return long_df.drop(columns=["variable"])


def number_frequency_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Calcula frequência das dezenas por período.

    Correção importante: no modo ``geral`` a coluna artificial ``periodo``
    precisa existir tanto na tabela longa quanto na tabela-base usada para
    contar sorteios. Sem isso, o pandas dispara ``KeyError: 'periodo'``.
    """
    base_df = df.copy()
    long_df = number_long_table(base_df)

    if period == "semana":
        keys = ["semana_iso", "semana_inicio", "semana_fim"]
        period_key = "semana_iso"
        sort_cols = ["semana_inicio", "frequencia", "dezena"]
        ascending = [False, False, True]
    elif period == "mes":
        keys = ["mes"]
        period_key = "mes"
        sort_cols = ["mes", "frequencia", "dezena"]
        ascending = [False, False, True]
    elif period == "ano":
        keys = ["ano"]
        period_key = "ano"
        sort_cols = ["ano", "frequencia", "dezena"]
        ascending = [False, False, True]
    elif period == "geral":
        base_df["periodo"] = "GERAL"
        long_df["periodo"] = "GERAL"
        keys = ["periodo"]
        period_key = "periodo"
        sort_cols = ["frequencia", "dezena"]
        ascending = [False, True]
    else:
        raise ValueError("period inválido")

    period_draws = base_df.groupby(keys, dropna=False)["concurso"].nunique().reset_index(name="sorteios_periodo")
    freq = long_df.groupby(keys + ["dezena"], dropna=False).size().reset_index(name="frequencia")
    freq = freq.merge(period_draws, on=keys, how="left")
    freq["freq_esperada"] = freq["sorteios_periodo"] * EXPECTED_P_NUMBER
    freq["percentual_sorteios"] = freq["frequencia"] / freq["sorteios_periodo"]
    freq["z_score"] = [binomial_z_score(o, n, EXPECTED_P_NUMBER) for o, n in zip(freq["frequencia"], freq["sorteios_periodo"])]
    freq["p_valor_aprox"] = freq["z_score"].map(norm_two_sided_p)
    freq["p_valor_bh"] = bh_adjust(freq["p_valor_aprox"])
    freq["ranking_no_periodo"] = freq.groupby(period_key, dropna=False)["frequencia"].rank(method="dense", ascending=False).astype(int)
    return freq.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)


def consistency_by_number(df: pd.DataFrame, recent_window: int = 100) -> pd.DataFrame:
    long_df = number_long_table(df)
    total_draws = len(df)
    counts = long_df["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0).rename_axis("dezena").reset_index(name="frequencia")

    weeks = long_df.groupby("dezena")["semana_iso"].nunique().reindex(ALL_NUMBERS, fill_value=0).rename("semanas_com_presenca")
    months = long_df.groupby("dezena")["mes"].nunique().reindex(ALL_NUMBERS, fill_value=0).rename("meses_com_presenca")
    years = long_df.groupby("dezena")["ano"].nunique().reindex(ALL_NUMBERS, fill_value=0).rename("anos_com_presenca")
    total_weeks = df["semana_iso"].nunique()
    total_months = df["mes"].nunique()
    total_years = df["ano"].nunique()

    last_rows = long_df.sort_values("data_sorteio").groupby("dezena").tail(1).set_index("dezena")
    last_concurso = last_rows["concurso"].reindex(ALL_NUMBERS)
    last_date = last_rows["data_sorteio"].reindex(ALL_NUMBERS)
    max_concurso = int(df["concurso"].max())
    last_df_index = df.reset_index().melt(id_vars=["index", "concurso"], value_vars=DEZENA_COLS, value_name="dezena")
    last_idx = last_df_index.groupby("dezena")["index"].max().reindex(ALL_NUMBERS)

    recent = df.tail(min(recent_window, len(df)))
    prev = df.iloc[max(0, len(df) - 2 * recent_window) : max(0, len(df) - recent_window)]
    recent_counts = number_long_table(recent)["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0)
    prev_counts = number_long_table(prev)["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0) if not prev.empty else pd.Series(0, index=ALL_NUMBERS)

    out = counts.copy()
    out["percentual_sorteios"] = out["frequencia"] / total_draws
    out["freq_esperada"] = total_draws * EXPECTED_P_NUMBER
    out["desvio_abs"] = out["frequencia"] - out["freq_esperada"]
    out["z_score"] = [binomial_z_score(o, total_draws, EXPECTED_P_NUMBER) for o in out["frequencia"]]
    out["p_valor_aprox"] = out["z_score"].map(norm_two_sided_p)
    out["p_valor_bh"] = bh_adjust(out["p_valor_aprox"])
    out["semanas_com_presenca"] = out["dezena"].map(weeks).astype(int)
    out["pct_semanas_com_presenca"] = out["semanas_com_presenca"] / total_weeks if total_weeks else np.nan
    out["meses_com_presenca"] = out["dezena"].map(months).astype(int)
    out["pct_meses_com_presenca"] = out["meses_com_presenca"] / total_months if total_months else np.nan
    out["anos_com_presenca"] = out["dezena"].map(years).astype(int)
    out["pct_anos_com_presenca"] = out["anos_com_presenca"] / total_years if total_years else np.nan
    out["ultimo_concurso"] = out["dezena"].map(last_concurso)
    out["ultima_data"] = out["dezena"].map(last_date)
    out["atraso_em_concursos"] = max_concurso - out["ultimo_concurso"]
    out["atraso_em_sorteios_da_base"] = (len(df) - 1 - out["dezena"].map(last_idx)).astype("Int64")
    out["freq_janela_recente"] = out["dezena"].map(recent_counts).astype(int)
    out["freq_janela_anterior"] = out["dezena"].map(prev_counts).astype(int)
    out["tendencia_recente"] = out["freq_janela_recente"] - out["freq_janela_anterior"]

    # Score apenas descritivo. Não é recomendação de aposta.
    z_freq = (out["z_score"] - out["z_score"].mean()) / (out["z_score"].std(ddof=0) or 1)
    z_trend = (out["tendencia_recente"] - out["tendencia_recente"].mean()) / (out["tendencia_recente"].std(ddof=0) or 1)
    z_cons = (out["pct_meses_com_presenca"] - out["pct_meses_com_presenca"].mean()) / (out["pct_meses_com_presenca"].std(ddof=0) or 1)
    z_delay = (out["atraso_em_sorteios_da_base"] - out["atraso_em_sorteios_da_base"].mean()) / (out["atraso_em_sorteios_da_base"].std(ddof=0) or 1)
    out["score_descritivo"] = 0.40 * z_freq + 0.25 * z_trend + 0.25 * z_cons + 0.10 * z_delay
    out["ranking_score_descritivo"] = out["score_descritivo"].rank(method="first", ascending=False).astype(int)

    return out.sort_values(["score_descritivo", "frequencia", "dezena"], ascending=[False, False, True]).reset_index(drop=True)


def chi_square_uniformity(df: pd.DataFrame) -> pd.DataFrame:
    counts = number_long_table(df)["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0).sort_index()
    expected = np.repeat(len(df) * EXPECTED_P_NUMBER, 25)
    if chisquare is not None:
        stat, p = chisquare(f_obs=counts.values, f_exp=expected)
    else:
        stat = float(((counts.values - expected) ** 2 / expected).sum())
        p = np.nan
    return pd.DataFrame(
        [
            {
                "teste": "Qui-quadrado de uniformidade das dezenas",
                "estatistica": float(stat),
                "p_valor": float(p) if pd.notna(p) else np.nan,
                "gl": 24,
                "interpretacao": "p baixo sugere desvio da uniformidade histórica; não implica previsibilidade futura.",
            }
        ]
    )


def rolling_frequency(df: pd.DataFrame, window: int = 100) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    records = []
    df_sorted = df.sort_values("data_sorteio").reset_index(drop=True)
    for end_idx in range(1, len(df_sorted) + 1):
        if end_idx < max(10, min(window, len(df_sorted))):
            continue
        chunk = df_sorted.iloc[max(0, end_idx - window) : end_idx]
        counts = number_long_table(chunk)["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0)
        row_date = df_sorted.iloc[end_idx - 1]["data_sorteio"]
        row_concurso = df_sorted.iloc[end_idx - 1]["concurso"]
        for dezena, freq in counts.items():
            records.append(
                {
                    "concurso_final_janela": row_concurso,
                    "data_final_janela": row_date,
                    "dezena": int(dezena),
                    "janela_sorteios": len(chunk),
                    "frequencia_janela": int(freq),
                    "percentual_janela": float(freq / len(chunk)),
                }
            )
    return pd.DataFrame(records)


def combination_frequency(df: pd.DataFrame, combo_size: int, period: str, min_freq: int = 2) -> pd.DataFrame:
    if combo_size < 2 or combo_size > 15:
        raise ValueError("Tamanho da combinação deve ficar entre 2 e 15.")

    if period == "semana":
        key_cols = ["semana_iso", "semana_inicio", "semana_fim"]
        period_key = "semana_iso"
    elif period == "mes":
        key_cols = ["mes"]
        period_key = "mes"
    elif period == "ano":
        key_cols = ["ano"]
        period_key = "ano"
    elif period == "geral":
        df = df.copy()
        df["periodo"] = "GERAL"
        key_cols = ["periodo"]
        period_key = "periodo"
    else:
        raise ValueError("period inválido")

    counter = Counter()
    period_draws = df.groupby(key_cols)["concurso"].nunique().reset_index(name="sorteios_periodo")
    period_draws_dict = {tuple(row[c] for c in key_cols): int(row["sorteios_periodo"]) for _, row in period_draws.iterrows()}

    for _, row in df.iterrows():
        key = tuple(row[c] for c in key_cols)
        dezenas = [int(row[c]) for c in DEZENA_COLS]
        for combo in combinations(dezenas, combo_size):
            counter[(key, combo)] += 1

    p_combo = probability_combo(combo_size)
    records = []
    for (key, combo), freq in counter.items():
        if freq < min_freq:
            continue
        n_draws = period_draws_dict[key]
        expected = n_draws * p_combo
        z = binomial_z_score(freq, n_draws, p_combo)
        rec = {col: val for col, val in zip(key_cols, key)}
        rec.update(
            {
                "combo_tamanho": combo_size,
                "combinacao": "-".join(f"{n:02d}" for n in combo),
                "frequencia": int(freq),
                "sorteios_periodo": int(n_draws),
                "prob_teorica_combo_por_sorteio": p_combo,
                "freq_esperada": expected,
                "z_score": z,
                "p_valor_aprox": norm_two_sided_p(z),
            }
        )
        records.append(rec)

    out = pd.DataFrame(records)
    if out.empty:
        return out
    out["p_valor_bh"] = bh_adjust(out["p_valor_aprox"])
    out["ranking_no_periodo"] = out.groupby(period_key)["frequencia"].rank(method="dense", ascending=False).astype(int)
    sort_cols = key_cols + ["frequencia", "z_score", "combinacao"]
    asc = [False] * len(key_cols) + [False, False, True]
    return out.sort_values(sort_cols, ascending=asc).reset_index(drop=True)


def cooccurrence_pairs(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    pair_counter = Counter()
    for _, row in df.iterrows():
        nums = [int(row[c]) for c in DEZENA_COLS]
        for pair in combinations(nums, 2):
            pair_counter[pair] += 1

    n = len(df)
    p_pair = probability_combo(2)
    records = []
    matrix = pd.DataFrame(0, index=ALL_NUMBERS, columns=ALL_NUMBERS, dtype=int)
    for (a, b), freq in pair_counter.items():
        z = binomial_z_score(freq, n, p_pair)
        records.append(
            {
                "dezena_a": a,
                "dezena_b": b,
                "par": f"{a:02d}-{b:02d}",
                "frequencia": int(freq),
                "sorteios": n,
                "freq_esperada": n * p_pair,
                "z_score": z,
                "p_valor_aprox": norm_two_sided_p(z),
            }
        )
        matrix.loc[a, b] = freq
        matrix.loc[b, a] = freq
    pairs = pd.DataFrame(records)
    pairs["p_valor_bh"] = bh_adjust(pairs["p_valor_aprox"])
    pairs = pairs.sort_values(["frequencia", "z_score", "par"], ascending=[False, False, True]).reset_index(drop=True)
    matrix.index.name = "dezena"
    return pairs, matrix.reset_index()


def backtest_models(df: pd.DataFrame, train_window: int = 200, recent_window: int = 50) -> tuple[pd.DataFrame, pd.DataFrame]:
    df_sorted = df.sort_values(["data_sorteio", "concurso"]).reset_index(drop=True)
    if len(df_sorted) <= train_window + 1:
        return pd.DataFrame(), pd.DataFrame()

    records = []
    models = ["frequencia_janela", "frequencia_recente", "score_composto"]

    for idx in range(train_window, len(df_sorted)):
        train = df_sorted.iloc[idx - train_window : idx]
        test_row = df_sorted.iloc[idx]
        actual = set(int(test_row[c]) for c in DEZENA_COLS)

        train_long = number_long_table(train)
        hist_counts = train_long["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0)

        recent = train.tail(min(recent_window, len(train)))
        recent_counts = number_long_table(recent)["dezena"].value_counts().reindex(ALL_NUMBERS, fill_value=0)

        weeks_presence = train_long.groupby("dezena")["semana_iso"].nunique().reindex(ALL_NUMBERS, fill_value=0)
        total_weeks = max(train["semana_iso"].nunique(), 1)
        consistency = weeks_presence / total_weeks

        # Recência: quanto maior o atraso, maior o score neste componente. Apenas descritivo.
        melted = train.reset_index().melt(id_vars=["index"], value_vars=DEZENA_COLS, value_name="dezena")
        last_idx = melted.groupby("dezena")["index"].max().reindex(ALL_NUMBERS)
        delay = train.index.max() - last_idx
        delay = delay.fillna(train_window)

        def z(series: pd.Series) -> pd.Series:
            s = series.astype(float)
            sd = s.std(ddof=0)
            if not np.isfinite(sd) or sd == 0:
                return pd.Series(0.0, index=s.index)
            return (s - s.mean()) / sd

        scores = pd.DataFrame(index=ALL_NUMBERS)
        scores["frequencia_janela"] = hist_counts
        scores["frequencia_recente"] = recent_counts
        scores["score_composto"] = 0.45 * z(hist_counts) + 0.30 * z(recent_counts) + 0.15 * z(consistency) + 0.10 * z(delay)

        for model in models:
            selected = set(scores.sort_values([model], ascending=False).head(15).index.astype(int))
            hits = len(selected & actual)
            records.append(
                {
                    "modelo": model,
                    "concurso_teste": int(test_row["concurso"]),
                    "data_teste": test_row["data_sorteio"],
                    "acertos": int(hits),
                    "numeros_selecionados": "-".join(f"{n:02d}" for n in sorted(selected)),
                    "numeros_sorteados": "-".join(f"{n:02d}" for n in sorted(actual)),
                }
            )

    details = pd.DataFrame(records)
    if details.empty:
        return details, pd.DataFrame()

    random_mean = sum(h * hypergeom_match_probability(h) for h in range(5, 16))
    summary_records = []
    for model, group in details.groupby("modelo"):
        mean_hits = group["acertos"].mean()
        std_hits = group["acertos"].std(ddof=1)
        n_tests = len(group)
        se = std_hits / math.sqrt(n_tests) if std_hits and np.isfinite(std_hits) and n_tests > 1 else np.nan
        z_mean = (mean_hits - random_mean) / se if se and np.isfinite(se) and se > 0 else np.nan
        summary_records.append(
            {
                "modelo": model,
                "testes": int(n_tests),
                "media_acertos": float(mean_hits),
                "mediana_acertos": float(group["acertos"].median()),
                "max_acertos": int(group["acertos"].max()),
                "pct_11_ou_mais": float((group["acertos"] >= 11).mean()),
                "pct_12_ou_mais": float((group["acertos"] >= 12).mean()),
                "pct_13_ou_mais": float((group["acertos"] >= 13).mean()),
                "media_aleatoria_teorica": float(random_mean),
                "delta_vs_aleatorio": float(mean_hits - random_mean),
                "z_media_vs_aleatorio": float(z_mean) if pd.notna(z_mean) else np.nan,
                "p_valor_media_aprox": norm_two_sided_p(z_mean) if pd.notna(z_mean) else np.nan,
            }
        )
    summary = pd.DataFrame(summary_records).sort_values("media_acertos", ascending=False).reset_index(drop=True)
    return details, summary


# =========================================================
# RELATÓRIO EXCEL
# =========================================================

def make_methodology_df(
    combo_size: int,
    min_combo_freq: int,
    rolling_window: int,
    recent_window: int,
    backtest_window: int,
    source_info: str,
) -> pd.DataFrame:
    rows = [
        ("Fonte", source_info),
        ("Aviso", "Estatística descritiva. Não prevê sorteios e não garante ganho."),
        ("Probabilidade de uma dezena aparecer", "15/25 = 0,60 por sorteio"),
        ("Z-score dezenas", "(frequência observada - frequência esperada) / desvio padrão binomial"),
        ("Combinações", f"Tamanho k={combo_size}; frequência mínima={min_combo_freq}"),
        ("Probabilidade de uma combinação fixa", "C(25-k,15-k)/C(25,15)"),
        ("Correção múltipla", "Benjamini-Hochberg nos p-valores aproximados"),
        ("Tendência", f"Janela móvel de {rolling_window} sorteios"),
        ("Janela recente", f"{recent_window} sorteios"),
        ("Backtest", f"Treino móvel com {backtest_window} sorteios anteriores; teste no concurso seguinte"),
        ("Interpretação", "Padrões históricos podem ocorrer por acaso em processos aleatórios."),
    ]
    return pd.DataFrame(rows, columns=["item", "descricao"])


def excel_bytes_report(sheets: dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="dd/mm/yyyy", date_format="dd/mm/yyyy") as writer:
        for sheet_name, df_sheet in sheets.items():
            safe_name = sheet_name[:31]
            df_to_write = df_sheet.copy() if df_sheet is not None else pd.DataFrame()
            df_to_write.to_excel(writer, index=False, sheet_name=safe_name)

        workbook = writer.book
        header_fmt = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#0F172A", "border": 1})
        date_fmt = workbook.add_format({"num_format": "dd/mm/yyyy"})
        pct_fmt = workbook.add_format({"num_format": "0.00%"})
        dec_fmt = workbook.add_format({"num_format": "0.0000"})
        int_fmt = workbook.add_format({"num_format": "0"})
        money_fmt = workbook.add_format({"num_format": "R$ #,##0.00"})

        for sheet_name, df_sheet in sheets.items():
            safe_name = sheet_name[:31]
            ws = writer.sheets[safe_name]
            df_sheet = df_sheet if df_sheet is not None else pd.DataFrame()
            ws.freeze_panes(1, 0)
            max_col = max(len(df_sheet.columns) - 1, 0)
            max_row = max(len(df_sheet), 1)
            if len(df_sheet.columns) > 0:
                ws.autofilter(0, 0, max_row, max_col)
            for col_idx, col_name in enumerate(df_sheet.columns):
                ws.write(0, col_idx, col_name, header_fmt)
                sample = df_sheet[col_name].astype(str).head(600).tolist() if not df_sheet.empty else []
                width = min(max([len(str(col_name))] + [len(x) for x in sample]) + 2, 54)
                col_lower = str(col_name).lower()
                fmt = None
                if "data" in col_lower or "inicio" in col_lower or "fim" in col_lower:
                    fmt = date_fmt
                elif col_lower.startswith("pct") or "percentual" in col_lower or "probabilidade" in col_lower:
                    fmt = pct_fmt
                elif "z_score" in col_lower or "p_valor" in col_lower or "freq_esperada" in col_lower or "score" in col_lower or "media" in col_lower:
                    fmt = dec_fmt
                elif col_lower in ("frequencia", "sorteios_periodo", "dezena", "concurso", "acertos") or col_lower.startswith("dezena_"):
                    fmt = int_fmt
                elif "rateio" in col_lower or "premio" in col_lower:
                    fmt = money_fmt
                ws.set_column(col_idx, col_idx, width, fmt)
            ws.set_zoom(90)
    return output.getvalue()


# =========================================================
# INTERFACE
# =========================================================

st.title("📊 Lotofácil Analytics Pro v3.1")
st.caption("Série histórica, estatística descritiva, coocorrência, atraso, tendência, probabilidade e backtest.")

st.markdown(
    """
    <div class="warning-card">
    <b>Aviso objetivo:</b> este app faz análise de dados. Ele não prevê sorteios. Em loteria, frequência passada não garante repetição futura.
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Entrada de dados")
    source = st.radio(
        "Fonte",
        ["Baixar automaticamente da CAIXA", "Importar arquivo local", "Colar tabela"],
        index=1,
        help="O modo automático pode falhar por bloqueio/mudança do endpoint. O modo local é o mais estável.",
    )

    uploaded_file = None
    pasted_text = ""
    if source == "Importar arquivo local":
        uploaded_file = st.file_uploader(
            "Histórico da Lotofácil",
            type=["xlsx", "xls", "csv", "txt", "htm", "html", "zip", "json"],
        )
    elif source == "Colar tabela":
        pasted_text = st.text_area(
            "Cole a tabela com cabeçalho",
            height=220,
            placeholder="Concurso\tData Sorteio\tBola1\tBola2\t...\tBola15\n1\t29/09/2003\t2\t3\t...",
        )

    st.divider()
    st.header("Parâmetros")
    combo_size = st.slider(
        "Tamanho da combinação",
        min_value=2,
        max_value=15,
        value=3,
        step=1,
        help="2 = pares, 3 = trios, 4 = quartetos. Tamanhos altos crescem muito rápido.",
    )
    min_combo_freq = st.number_input("Frequência mínima das combinações", min_value=1, max_value=50, value=2, step=1)
    top_n = st.number_input("Top N na tela", min_value=10, max_value=10000, value=300, step=50)

    rolling_window = st.number_input("Janela móvel de tendência", min_value=20, max_value=1000, value=100, step=10)
    recent_window = st.number_input("Janela recente", min_value=10, max_value=500, value=50, step=10)
    backtest_window = st.number_input("Janela de treino do backtest", min_value=50, max_value=2000, value=200, step=50)

    st.divider()
    st.header("Desempenho")
    max_combos = st.number_input("Limite de combinações processadas", min_value=100_000, max_value=100_000_000, value=5_000_000, step=100_000)
    force_heavy = st.checkbox("Forçar cálculo pesado", value=False)

try:
    raw_df = None
    source_info = ""

    with st.spinner("Lendo dados..."):
        if source == "Baixar automaticamente da CAIXA":
            raw_df, source_info = download_caixa_history()
        elif source == "Importar arquivo local":
            if uploaded_file is None:
                st.info("Importe o arquivo da série histórica para iniciar.")
                st.stop()
            raw_df = read_any_table(uploaded_file.getvalue(), filename=uploaded_file.name)
            source_info = uploaded_file.name
        else:
            if not pasted_text.strip():
                st.info("Cole a tabela para iniciar.")
                st.stop()
            raw_df = parse_pasted_table(pasted_text)
            source_info = "tabela colada"

        cleaned, meta = clean_lotofacil_results(raw_df)

    st.markdown(
        f"""
        <div class="good-card">
        Base carregada: <b>{len(cleaned):,}</b> concursos válidos. Fonte: <b>{source_info}</b>.
        </div>
        """.replace(",", "."),
        unsafe_allow_html=True,
    )

    min_date = cleaned["data_sorteio"].min().date()
    max_date = cleaned["data_sorteio"].max().date()

    f1, f2 = st.columns(2)
    with f1:
        start_date = st.date_input("Data inicial", min_date, min_value=min_date, max_value=max_date)
    with f2:
        end_date = st.date_input("Data final", max_date, min_value=min_date, max_value=max_date)

    if start_date > end_date:
        st.error("Data inicial maior que data final.")
        st.stop()

    filtered = cleaned[(cleaned["data_sorteio"].dt.date >= start_date) & (cleaned["data_sorteio"].dt.date <= end_date)].copy()
    if filtered.empty:
        st.error("Nenhum concurso no intervalo selecionado.")
        st.stop()

    total_combo_units = len(filtered) * math.comb(15, combo_size)
    heavy_blocked = total_combo_units > max_combos and not force_heavy

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Concursos", f"{len(filtered):,}".replace(",", "."))
    c2.metric("Semanas", f"{filtered['semana_iso'].nunique():,}".replace(",", "."))
    c3.metric("Meses", f"{filtered['mes'].nunique():,}".replace(",", "."))
    c4.metric("Combinações", f"{total_combo_units:,}".replace(",", "."))
    c5.metric("Chance 15 acertos", f"1 em {math.comb(25, 15):,}".replace(",", "."))

    if heavy_blocked:
        st.warning(
            f"Cálculo de combinações bloqueado por desempenho: seriam {total_combo_units:,} combinações. "
            "Aumente o limite, reduza o tamanho da combinação ou marque 'Forçar cálculo pesado'.".replace(",", ".")
        )

    with st.spinner("Calculando estatísticas das dezenas..."):
        base_diagnostics = validate_clean_base(filtered)
        dez_geral = number_frequency_period(filtered, "geral")
        dez_semana = number_frequency_period(filtered, "semana")
        dez_mes = number_frequency_period(filtered, "mes")
        dez_ano = number_frequency_period(filtered, "ano")
        consistencia = consistency_by_number(filtered, recent_window=int(recent_window))
        chi_df = chi_square_uniformity(filtered)
        rolling_df = rolling_frequency(filtered, window=int(rolling_window))
        prob_df = theoretical_probabilities()

    if heavy_blocked:
        combos_semana = pd.DataFrame()
        combos_mes = pd.DataFrame()
        combos_geral = pd.DataFrame()
    else:
        with st.spinner("Calculando combinações recorrentes..."):
            combos_semana = combination_frequency(filtered, combo_size, "semana", min_freq=int(min_combo_freq))
            combos_mes = combination_frequency(filtered, combo_size, "mes", min_freq=int(min_combo_freq))
            combos_geral = combination_frequency(filtered, combo_size, "geral", min_freq=int(min_combo_freq))

    with st.spinner("Calculando coocorrência e backtest..."):
        pairs_df, pair_matrix = cooccurrence_pairs(filtered)
        backtest_details, backtest_summary = backtest_models(filtered, train_window=int(backtest_window), recent_window=int(recent_window))

    methodology = make_methodology_df(
        combo_size=int(combo_size),
        min_combo_freq=int(min_combo_freq),
        rolling_window=int(rolling_window),
        recent_window=int(recent_window),
        backtest_window=int(backtest_window),
        source_info=source_info,
    )

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
        [
            "Dashboard",
            "Dezenas",
            "Combinações",
            "Coocorrência",
            "Tendência",
            "Backtest",
            "Probabilidade",
            "Base/Exportar",
        ]
    )

    with tab1:
        st.subheader("Resumo estatístico")
        col_a, col_b = st.columns([2, 1])
        with col_a:
            chart_df = consistencia.sort_values("dezena").copy()
            chart_df["dezena_label"] = chart_df["dezena"].astype(str).str.zfill(2)
            fig = px.bar(chart_df, x="dezena_label", y="frequencia", text="frequencia", title="Frequência geral por dezena")
            fig.add_hline(y=len(filtered) * EXPECTED_P_NUMBER, line_dash="dash", annotation_text="esperado teórico")
            fig.update_layout(height=430, xaxis_title="Dezena", yaxis_title="Frequência")
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            st.dataframe(chi_df, use_container_width=True, hide_index=True)
            st.markdown(
                "<div class='small-note'>Qui-quadrado baixo ou alto não transforma a loteria em processo previsível. Serve para auditoria exploratória.</div>",
                unsafe_allow_html=True,
            )

        st.subheader("Ranking técnico das dezenas")
        st.dataframe(consistencia.head(int(top_n)), use_container_width=True, hide_index=True)

    with tab2:
        st.subheader("Frequência semanal")
        st.dataframe(dez_semana.head(int(top_n)), use_container_width=True, hide_index=True)
        st.subheader("Frequência mensal")
        st.dataframe(dez_mes.head(int(top_n)), use_container_width=True, hide_index=True)
        st.subheader("Frequência anual")
        st.dataframe(dez_ano.head(int(top_n)), use_container_width=True, hide_index=True)

    with tab3:
        st.subheader(f"Combinações recorrentes de tamanho {combo_size}")
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.markdown("**Semanal**")
            st.dataframe(combos_semana.head(int(top_n)) if not combos_semana.empty else combos_semana, use_container_width=True, hide_index=True)
        with col_b:
            st.markdown("**Mensal**")
            st.dataframe(combos_mes.head(int(top_n)) if not combos_mes.empty else combos_mes, use_container_width=True, hide_index=True)
        with col_c:
            st.markdown("**Geral**")
            st.dataframe(combos_geral.head(int(top_n)) if not combos_geral.empty else combos_geral, use_container_width=True, hide_index=True)

    with tab4:
        st.subheader("Pares que mais coocorrem")
        st.dataframe(pairs_df.head(int(top_n)), use_container_width=True, hide_index=True)
        st.subheader("Matriz de coocorrência dos pares")
        heat = pair_matrix.set_index("dezena")
        fig = px.imshow(heat, labels=dict(x="Dezena", y="Dezena", color="Frequência"), aspect="auto")
        fig.update_layout(height=650)
        st.plotly_chart(fig, use_container_width=True)

    with tab5:
        st.subheader("Tendência em janela móvel")
        selected_numbers = st.multiselect("Dezenas para visualizar", ALL_NUMBERS, default=[1, 2, 3, 4, 5])
        plot_roll = rolling_df[rolling_df["dezena"].isin(selected_numbers)].copy()
        if not plot_roll.empty:
            plot_roll["dezena_label"] = plot_roll["dezena"].astype(str).str.zfill(2)
            fig = px.line(
                plot_roll,
                x="data_final_janela",
                y="percentual_janela",
                color="dezena_label",
                title=f"Percentual na janela móvel de {rolling_window} sorteios",
            )
            fig.add_hline(y=EXPECTED_P_NUMBER, line_dash="dash", annotation_text="esperado teórico 60%")
            fig.update_layout(height=520, xaxis_title="Data", yaxis_title="Percentual")
            st.plotly_chart(fig, use_container_width=True)
        st.dataframe(rolling_df.head(int(top_n)), use_container_width=True, hide_index=True)

    with tab6:
        st.subheader("Backtest fora da amostra")
        if backtest_summary.empty:
            st.warning("Base insuficiente para o tamanho da janela de treino escolhida.")
        else:
            st.dataframe(backtest_summary, use_container_width=True, hide_index=True)
            st.markdown(
                "<div class='small-note'>Se a média dos modelos ficar perto de 9 acertos, o comportamento é compatível com seleção aleatória de 15 números.</div>",
                unsafe_allow_html=True,
            )
            fig = px.box(backtest_details, x="modelo", y="acertos", points="outliers", title="Distribuição de acertos no backtest")
            fig.add_hline(y=9, line_dash="dash", annotation_text="média aleatória teórica")
            fig.update_layout(height=460)
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(backtest_details.tail(int(top_n)), use_container_width=True, hide_index=True)

    with tab7:
        st.subheader("Probabilidades teóricas de acerto")
        st.dataframe(prob_df, use_container_width=True, hide_index=True)
        st.markdown(
            f"""
            <div class="warning-card">
            Um jogo simples de 15 dezenas tem probabilidade de <b>1 em {math.comb(25, 15):,}</b> de acertar as 15 dezenas.
            O app não altera essa probabilidade; ele apenas descreve a série histórica.
            </div>
            """.replace(",", "."),
            unsafe_allow_html=True,
        )

    with tab8:
        st.subheader("Validação da base")
        st.dataframe(base_diagnostics, use_container_width=True, hide_index=True)
        st.subheader("Diagnóstico de leitura")
        st.json(meta)
        st.subheader("Base limpa")
        st.dataframe(filtered, use_container_width=True, hide_index=True)
        with st.expander("Prévia da base bruta"):
            st.dataframe(raw_df.head(30), use_container_width=True, hide_index=True)

        sheets = {
            "Resumo": pd.DataFrame(
                [
                    {"indicador": "concursos", "valor": len(filtered)},
                    {"indicador": "data_inicial", "valor": str(filtered["data_sorteio"].min().date())},
                    {"indicador": "data_final", "valor": str(filtered["data_sorteio"].max().date())},
                    {"indicador": "combo_size", "valor": combo_size},
                    {"indicador": "fonte", "valor": source_info},
                    {"indicador": "chance_15_acertos", "valor": f"1 em {math.comb(25, 15)}"},
                ]
            ),
            "Metodologia": methodology,
            "Validacao_base": base_diagnostics,
            "Base_limpa": filtered,
            "Dezenas_modelagem": consistencia,
            "Dezenas_semanal": dez_semana,
            "Dezenas_mensal": dez_mes,
            "Dezenas_anual": dez_ano,
            "Teste_uniformidade": chi_df,
            "Tendencia_rolling": rolling_df,
            "Combos_semanal": combos_semana,
            "Combos_mensal": combos_mes,
            "Combos_geral": combos_geral,
            "Pares_coocorrencia": pairs_df,
            "Matriz_coocorrencia": pair_matrix,
            "Backtest_resumo": backtest_summary,
            "Backtest_detalhado": backtest_details,
            "Probabilidades": prob_df,
        }
        with st.spinner("Gerando Excel..."):
            report_bytes = excel_bytes_report(sheets)

        st.download_button(
            "⬇️ Baixar relatório Excel robusto",
            data=report_bytes,
            file_name=f"lotofacil_analytics_pro_v3_1_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

except Exception as exc:
    st.error("Falha ao processar.")
    st.exception(exc)
    st.markdown(
        """
        Correção prática: use um arquivo com cabeçalho igual a este:
        `Concurso`, `Data Sorteio`, `Bola1`, `Bola2`, ..., `Bola15`.

        A importação local é mais confiável que o download automático da CAIXA.
        """
    )
