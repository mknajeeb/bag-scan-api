# app.py

import os
import re
import logging
import traceback
from datetime import datetime

import pandas as pd
from flask import Flask, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ─── APP SETUP ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.DEBUG)
app.logger.setLevel(logging.DEBUG)

# ─── DATABASE CONFIG ─────────────────────────────────────────────────────────
# Ensure SQLAZURE is set in App Service configuration
conn_str = os.environ.get("SQLAZURE")
if not conn_str:
    app.logger.error("Missing SQLAZURE environment variable")
    raise RuntimeError("Missing SQLAZURE environment variable")
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# Excel file location
INPUT_FILE = "testrunrinse.xlsx"

# ─── HELPER: Load & Prepare Excel ─────────────────────────────────────────────
def load_and_prepare():
    df = pd.read_excel(INPUT_FILE, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Identify columns
    date_col = next(c for c in df.columns if "date" in c.lower())
    name_col = next(c for c in df.columns if "customer" in c.lower())
    wf_col   = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())

    # Select and rename
    df = df[[date_col, name_col, wf_col]].dropna(subset=[date_col, name_col])
    df.columns = ["Date", "Customer", "WF_LBS"]

    # Clean date and detect "TODAY"
    df["Actual_Date"] = df["Date"].astype(str).replace(r"\s*TODAY\s*", "", regex=True)
    df["HasTODAY"]   = df["Date"].astype(str).str.upper().str.contains("TODAY")
    rush_dates = set(df.loc[df["HasTODAY"], "Actual_Date"])

    # Classify service
    def classify_service(v):
        s = str(v).strip().upper().replace("LBS", "").strip()
        try:
            w = float(s)
            return "Hang Dry" if w == 0 else "Wash & Fold"
        except:
            return "Hang Dry"

    df["Category"] = df["WF_LBS"].apply(classify_service)

    # Determine RushFlag
    df["RushFlag"] = df.apply(
        lambda r: "RUSH" if r["HasTODAY"] or r["Actual_Date"] in rush_dates else "NON-RUSH",
        axis=1
    )

    return df

# ─── ENDPOINT: /import-data ───────────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        df = load_and_prepare()
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Excel load failed:\n%s", tb)
        return jsonify({"error": "Excel load failed", "details": str(e)}), 500

    total_rows   = len(df)
    rush_count   = int((df["RushFlag"] == "RUSH").sum())
    nonrush_count= total_rows - rush_count
    hangdry_count= int((df["Category"] == "Hang Dry").sum())

    try:
        with engine.begin() as conn:
            # Create table if missing
            conn.execute(text("""
                IF OBJECT_ID('dbo.bags','U') IS NULL
                CREATE TABLE dbo.bags(
                  id INT IDENTITY(1,1) PRIMARY KEY,
                  Customer NVARCHAR(200) NOT NULL,
                  Category NVARCHAR(50) NOT NULL,
                  RushFlag NVARCHAR(10) NOT NULL,
                  scanned BIT DEFAULT 0,
                  scan_date DATE NULL,
                  lbs FLOAT NULL
                );
            """))

            # Truncate existing data
            conn.execute(text("TRUNCATE TABLE dbo.bags"))

            # Bulk insert all rows
            insert_sql = text(
                "INSERT INTO dbo.bags(Customer, Category, RushFlag, scanned, scan_date, lbs)"
                " VALUES(:c, :cat, :r, 0, NULL, NULL)"
            )
            conn.execute(
                insert_sql,
                [ {"c": r.Customer, "cat": r.Category, "r": r.RushFlag} for _, r in df.iterrows() ]
            )
    except SQLAlchemyError as e:
        tb = traceback.format_exc()
        app.logger.error("DB import failed:\n%s", tb)
        return jsonify({"error": "Database import failed", "details": str(e)}), 500

    return jsonify({
        "message":    f"Imported {total_rows} rows",
        "rush":       rush_count,
        "non_rush":   nonrush_count,
        "hang_dry":   hangdry_count
    }), 200

# ─── ENDPOINT: /status ─────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
        with engine.connect() as conn:
            total   = conn.execute(text("SELECT COUNT(*) FROM dbo.bags")).scalar()
            scanned = conn.execute(text("SELECT COUNT(*) FROM dbo.bags WHERE scanned=1")).scalar()
    except Exception as e:
        app.logger.error("Status query failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"total": total, "scanned": scanned, "remaining": total - scanned})

# ─── ENDPOINT: /bags ──────────────────────────────────────────────────────────
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
    return jsonify({"bags": data})

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

