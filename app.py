```python
# app.py

import os
import re
import logging
import traceback
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ─── APP SETUP ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.DEBUG)
app.logger.setLevel(logging.DEBUG)

# ─── DATABASE CONFIG ─────────────────────────────────────────────────────────
# Try App Settings 'SQLAZURE' or 'SQLAZURECONNSTR_SQLAZURE'
raw_conn = os.environ.get("SQLAZURE") or os.environ.get("SQLAZURECONNSTR_SQLAZURE")
if not raw_conn:
    app.logger.error("Missing SQLAZURE connection string. Check App Service configuration.")
    raise RuntimeError("Missing SQLAZURE connection string")
from urllib.parse import quote_plus
conn_str = quote_plus(raw_conn)
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── EXCEL INPUT ──────────────────────────────────────────────────────────────
# File must be deployed alongside your app
INPUT_FILE = os.path.join(os.getcwd(), "testrunrinse.xlsx")

# ─── HELPER: LOAD & PROCESS ──────────────────────────────────────────────────
def load_and_prepare():
    # read workbook
    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(f"Excel file not found at {INPUT_FILE}")
    df = pd.read_excel(INPUT_FILE, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # locate columns
    date_col = next(c for c in df.columns if "date" in c.lower())
    name_col = next(c for c in df.columns if "customer" in c.lower())
    wf_col   = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())

    # trim blank rows
    df = df[[date_col, name_col, wf_col]].dropna(subset=[date_col, name_col]).copy()
    df.columns = ["Date", "Customer", "WF_LBS"]

    # clean date string and detect TODAY
    df["RawDate"] = df["Date"].astype(str)
    df["ActualDate"] = df["RawDate"].replace(r"\s*TODAY\s*", "", regex=True)
    df["HasTODAY"] = df["RawDate"].str.upper().str.contains("TODAY")
    # any date matching a date with TODAY flagged is also rush
    rush_dates = set(df.loc[df["HasTODAY"], "ActualDate"])

    # classify service by WF_LBS
    def classify_service(val):
        s = str(val).upper().replace("LBS", "").strip()
        try:
            w = float(re.sub(r"[^0-9.]", "", s))
            return "Hang Dry" if w == 0 else "Wash & Fold"
        except:
            return "Hang Dry"
    df["Category"] = df["WF_LBS"].apply(classify_service)

    # determine rush flag
    df["RushFlag"] = df.apply(
        lambda r: "RUSH" if (r["HasTODAY"] or r["ActualDate"] in rush_dates) else "NON-RUSH",
        axis=1
    )

    return df

# ─── ENDPOINT: IMPORT & REFRESH ───────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        df = load_and_prepare()
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Excel load failed:\n%s", tb)
        return jsonify({"error": "Excel load failed", "details": str(e)}), 500

    total = len(df)
    rush_count = int((df["RushFlag"] == "RUSH").sum())
    nonrush_count = int((df["RushFlag"] == "NON-RUSH").sum())
    hangdry_count = int((df["Category"] == "Hang Dry").sum())

    try:
        with engine.begin() as conn:
            # drop and recreate table each import
            conn.execute(text("IF OBJECT_ID('dbo.bags','U') IS NOT NULL DROP TABLE dbo.bags;"))
            conn.execute(text(
                "CREATE TABLE dbo.bags("
                " id INT IDENTITY(1,1) PRIMARY KEY,"
                " Customer NVARCHAR(200) NOT NULL,"
                " Category NVARCHAR(50) NOT NULL,"
                " RushFlag NVARCHAR(10) NOT NULL,"
                " scanned BIT NOT NULL DEFAULT 0,"
                " lbs FLOAT NULL"
                ");"
            ))
            # bulk insert rows
            insert_sql = text(
                "INSERT INTO dbo.bags(Customer, Category, RushFlag, scanned, lbs)"
                " VALUES(:cust, :cat, :rush, 0, NULL)"
            )
            conn.execute(
                insert_sql,
                [{"cust": r.Customer, "cat": r.Category, "rush": r.RushFlag}
                 for _, r in df.iterrows()]
            )
    except SQLAlchemyError as e:
        tb = traceback.format_exc()
        app.logger.error("DB import failed:\n%s", tb)
        return jsonify({"error": "Database import failed", "details": str(e)}), 500

    return jsonify({
        "message": f"Imported {total} rows",
        "rush": rush_count,
        "non_rush": nonrush_count,
        "hang_dry": hangdry_count
    }), 200

# ─── ENDPOINT: STATUS ────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
        with engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM dbo.bags")).scalar()
            scanned = conn.execute(text("SELECT COUNT(*) FROM dbo.bags WHERE scanned=1")).scalar()
    except Exception as e:
        app.logger.error("Status query failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"total": total, "scanned": scanned, "remaining": total - scanned}), 200

# ─── ENDPOINT: LIST BAGS ─────────────────────────────────────────────────────
@app.route("/bags", methods=["GET"])
def list_bags():
    try:
        rows = engine.execute(text(
            "SELECT Customer, Category, RushFlag, scanned FROM dbo.bags ORDER BY Customer"
        )).fetchall()
        data = [
            {"customer": r.Customer, "category": r.Category, "rush": r.RushFlag, "scanned": bool(r.scanned)}
            for r in rows
        ]
    except Exception as e:
        app.logger.error("/bags query failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"bags": data}), 200

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
```

