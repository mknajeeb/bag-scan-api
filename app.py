# app.py

import os
import re
import time
import logging
import traceback
import pandas as pd
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

app = Flask(__name__)
CORS(app)
app.logger.setLevel(logging.DEBUG)

# ─── Configuration ───────────────────────────────────────────────────────────
# Azure SQL Connection String (set in App Service Configuration)
conn_str = os.environ.get("SQLAZURE")
if not conn_str:
    app.logger.error("Missing SQLAZURE environment variable")
    raise RuntimeError("Missing SQLAZURE environment variable")
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

INPUT_FILE = "testrunrinse.xlsx"

# ─── Helper: Load & Prepare Excel ─────────────────────────────────────────────
def load_and_prepare():
    df = pd.read_excel(INPUT_FILE, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Detect relevant columns
    date_col = next(c for c in df.columns if "date" in c.lower())
    name_col = next(c for c in df.columns if "customer" in c.lower())
    wf_col   = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())

    df = df[[date_col, name_col, wf_col]].dropna(subset=[date_col, name_col])
    df.columns = ["Date", "Customer", "WF_LBS"]

    # Clean date & detect rush marker
    df["Actual_Date"] = df["Date"].astype(str).replace(r"\s*TODAY\s*", "", regex=True)
    df["HasTODAY"]   = df["Date"].astype(str).str.upper().str.contains("TODAY")
    rush_dates = set(df.loc[df["HasTODAY"], "Actual_Date"])

    # Service classification
    def classify_service(val):
        s = str(val).strip().upper().replace("LBS", "").strip()
        try:
            w = float(s)
            return "Wash & Fold" if w > 0 else "Hang Dry"
        except:
            return "Hang Dry"

    df["Category"] = df["WF_LBS"].apply(classify_service)

    # Rush flag if row had TODAY or its date matches a TODAY date
    df["RushFlag"] = df.apply(
        lambda r: "RUSH" if r["HasTODAY"] or r["Actual_Date"] in rush_dates else "NON-RUSH",
        axis=1
    )

    return df

# ─── Endpoint: /import-data ───────────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        df = load_and_prepare()
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Excel load failed:\n%s", tb)
        return jsonify({"error": "Excel load failed", "details": str(e)}), 500

    total      = len(df)
    rush_cnt   = int((df["RushFlag"] == "RUSH").sum())
    nonrush_cnt= total - rush_cnt
    hangdry_cnt= int((df["Category"] == "Hang Dry").sum())

    try:
        with engine.begin() as conn:
            # ensure table schema
            conn.execute(text("""
                IF OBJECT_ID('dbo.bags','U') IS NULL
                CREATE TABLE dbo.bags(
                  id INT IDENTITY(1,1) PRIMARY KEY,
                  Customer NVARCHAR(200),
                  Category NVARCHAR(50),
                  RushFlag NVARCHAR(10)
                );
            """))
            # truncate existing data
            conn.execute(text("TRUNCATE TABLE dbo.bags"))

            # bulk insert
            insert_sql = text(
                "INSERT INTO dbo.bags(Customer,Category,RushFlag) VALUES(:c,:cat,:r)"
            )
            conn.execute(
                insert_sql,
                [
                  {"c": row.Customer, "cat": row.Category, "r": row.RushFlag}
                  for _, row in df.iterrows()
                ]
            )
    except SQLAlchemyError as e:
        tb = traceback.format_exc()
        app.logger.error("DB import failed:\n%s", tb)
        return jsonify({"error": "Database import failed", "details": str(e)}), 500

    return jsonify({
        "message":     f"Imported {total} rows",
        "rush":        rush_cnt,
        "non_rush":    nonrush_cnt,
        "hang_dry":    hangdry_cnt
    }), 200

# ─── Endpoint: /status ─────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
        with engine.connect() as conn:
            total   = conn.execute(text("SELECT COUNT(*) FROM dbo.bags")).scalar()
            scanned = conn.execute(text("SELECT COUNT(*) FROM dbo.bags WHERE scanned=1")).scalar()
    except Exception as e:
        app.logger.error("Status query failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"total": total, "scanned": scanned, "remaining": total-scanned})

# ─── Endpoint: /bags ──────────────────────────────────────────────────────────
@app.route("/bags", methods=["GET"])
def list_bags():
    try:
        rows = engine.execute(text(
            "SELECT Customer, Category, RushFlag FROM dbo.bags ORDER BY Customer"
        )).fetchall()
        data = [dict(customer=r.Customer, category=r.Category, rush=r.RushFlag) for r in rows]
    except Exception as e:
        app.logger.error("/bags query failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"bags": data})

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

