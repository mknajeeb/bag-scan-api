# app.py

import os
import re
import time
import pandas as pd
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

app = Flask(__name__)
CORS(app)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
# Azure Computer Vision (unused here but kept for OCR endpoint if you wish)
AZURE_ENDPOINT = "https://firstone-muhammad.cognitiveservices.azure.com/"
AZURE_KEY      = os.getenv("AZURE_KEY", "YOUR_KEY_HERE")

# Database via env var in App Service → Configuration → Connection strings
conn_str = os.environ["SQLAZURE"]   # e.g. "Driver={ODBC Driver 18 for SQL Server};Server=...;Database=...;UID=...;PWD=..."
engine   = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── 1) STATUS ─────────────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    """Return counts of total, scanned, remaining."""
    with engine.connect() as conn:
        total   = conn.execute(text("SELECT COUNT(*) FROM dbo.bags")).scalar()
        scanned = conn.execute(text("SELECT COUNT(*) FROM dbo.bags WHERE scanned=1")).scalar()
    remaining = total - scanned
    return jsonify({
        "total":     total,
        "scanned":   scanned,
        "remaining": remaining
    })

# ─── 2) LIST ALL BAGS ──────────────────────────────────────────────────────────
@app.route("/bags", methods=["GET"])
def list_bags():
    """Return full list of bags with their scan, rush & category flags."""
    rows = []
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT name, scanned, scan_date, lbs, category, rush
              FROM dbo.bags
              ORDER BY name
        """))
        for r in result:
            rows.append({
                "name":     r.name,
                "scanned":  bool(r.scanned),
                "scan_date": r.scan_date.isoformat() if r.scan_date else None,
                "lbs":      float(r.lbs) if r.lbs is not None else None,
                "category": r.category,
                "rush":     bool(r.rush)
            })
    return jsonify({"bags": rows})

# ─── 3) IMPORT‐DATA ─────────────────────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    """
    Truncate dbo.bags, re‑load Excel, classify each row, insert,
    then return summary counts.
    """
    # 1) Load & clean Excel
    INPUT_FILE = "testrunrinse.xlsx"
    df = pd.read_excel(INPUT_FILE)
    df = df.rename(columns=lambda x: x.strip())
    # pick our three columns
    date_col = [c for c in df.columns if "date" in c.lower()][0]
    name_col = [c for c in df.columns if "customer" in c.lower()][0]
    wf_col   = [c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower()][0]
    df = df[[date_col, name_col, wf_col]]
    df.columns = ["Date", "Customer Name", "# WF LBS"]
    df = df.dropna(subset=["Date", "Customer Name"])

    # 2) Identify rush dates (any row with literal “TODAY”)
    df["Actual_Date"] = df["Date"].astype(str).apply(
        lambda s: re.sub(r"\s*TODAY\s*", "", s, flags=re.IGNORECASE).strip()
    )
    df["Has_Today"] = df["Date"].astype(str).str.upper().str.contains("TODAY")
    rush_dates = set(df.loc[df["Has_Today"], "Actual_Date"])

    # 3) Service classification
    def classify_service(val):
        t = str(val).strip()
        # numeric → wash & fold; zero or fail parse → hang dry
        try:
            _ = float(t)
            return "Wash and Fold"
        except:
            return "Hang Dry"

    # 4) TRUNCATE table
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE dbo.bags"))

    # 5) Insert rows, tallying metrics
    rush_count    = 0
    nonrush_count = 0
    hangdry_count = 0
    inserted      = 0

    insert_sql = text("""
      INSERT INTO dbo.bags
        (name, scanned, scan_date, lbs, category, rush)
      VALUES
        (:name, 0, :dt, :lbs, :cat, :rush)
    """)

    with engine.begin() as conn:
        for _, row in df.iterrows():
            name  = row["Customer Name"].strip()
            ad    = row["Actual_Date"]
            # parse date (MM/DD/YYYY) if possible
            try:
                dt = datetime.strptime(ad, "%m/%d/%Y").date()
            except:
                dt = None
            # parse numeric lbs
            try:
                lbs = float(str(row["# WF LBS"]).upper().replace("LBS", "").strip())
            except:
                lbs = None

            cat    = classify_service(row["# WF LBS"])
            is_r   = (ad in rush_dates)

            # metrics
            if is_r:
                rush_count += 1
            else:
                nonrush_count += 1
            if cat == "Hang Dry":
                hangdry_count += 1

            conn.execute(insert_sql, {
                "name": name,
                "dt":   dt,
                "lbs":  lbs,
                "cat":  cat,
                "rush": 1 if is_r else 0
            })
            inserted += 1

    return jsonify({
        "message":   f"Imported {inserted} rows",
        "rush":      rush_count,
        "non_rush":  nonrush_count,
        "hang_dry":  hangdry_count
    }), 200

# ─── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # for local debugging only; in Azure we'll use gunicorn via startup.txt
    app.run(host="0.0.0.0", port=5001, debug=True)

