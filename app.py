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
raw_conn = os.environ.get("SQLAZURE") or os.environ.get("SQLAZURECONNSTR_SQLAZURE")
if not raw_conn:
    raise RuntimeError("Missing SQLAZURE connection string")
from urllib.parse import quote_plus
conn_str = quote_plus(raw_conn)
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── EXCEL INPUT CONFIG ───────────────────────────────────────────────────────
INPUT_FILE = os.environ.get("INPUT_FILE_PATH") or os.path.join(os.getcwd(), "testrunrinse.xlsx")

# ─── HELPER: LOAD & PROCESS ──────────────────────────────────────────────────
def load_and_prepare():
    if not os.path.exists(INPUT_FILE):
        raise FileNotFoundError(f"Excel file not found at {INPUT_FILE}")
    df = pd.read_excel(INPUT_FILE, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # locate columns
    date_col = next(c for c in df.columns if "date" in c.lower())
    name_col = next(c for c in df.columns if "customer" in c.lower())
    qr_col   = next((c for c in df.columns if "qr" in c.lower()), None)
    wf_col   = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())

    df = df[[date_col, name_col, qr_col, wf_col]].dropna(subset=[date_col, name_col, qr_col]).copy()
    df.columns = ["Date","Customer","QR","WF_LBS"]

    df["RawDate"] = df["Date"].astype(str)
    df["ActualDate"] = df["RawDate"].replace(r"\s*TODAY\s*", "", regex=True)
    df["HasTODAY"] = df["RawDate"].str.upper().str.contains("TODAY")
    rush_dates = set(df.loc[df["HasTODAY"], "ActualDate"])

    def classify_service(val):
        s = str(val).upper().replace("LBS","" ).strip()
        try:
            w = float(re.sub(r"[^0-9.]", "", s))
            return "Hang Dry" if w == 0 else "Wash & Fold"
        except:
            return "Hang Dry"
    df["Category"] = df["WF_LBS"].apply(classify_service)
    df["RushFlag"] = df.apply(lambda r: "RUSH" if (r["HasTODAY"] or r["ActualDate"] in rush_dates) else "NON-RUSH", axis=1)

    return df

# ─── ENDPOINT: IMPORT & REFRESH ───────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        df = load_and_prepare()
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Excel load failed:\n%s", tb)
        return jsonify({"error":"Excel load failed","details":str(e)}),500

    total = len(df)
    rush = int((df["RushFlag"]=="RUSH").sum())
    non_rush = total - rush
    hang_dry = int((df["Category"]=="Hang Dry").sum())

    try:
        with engine.begin() as conn:
            conn.execute(text("IF OBJECT_ID('dbo.bags','U') IS NOT NULL DROP TABLE dbo.bags;"))
            conn.execute(text(
                "CREATE TABLE dbo.bags("
                " id INT IDENTITY(1,1) PRIMARY KEY,"
                " Customer NVARCHAR(200) NOT NULL,"
                " QR NVARCHAR(200) NOT NULL UNIQUE,"
                " Category NVARCHAR(50) NOT NULL,"
                " RushFlag NVARCHAR(10) NOT NULL,"
                " scanned BIT NOT NULL DEFAULT 0,"
                " lbs FLOAT NULL"
                ");"))
            insert_sql = text(
                "INSERT INTO dbo.bags(Customer, QR, Category, RushFlag, scanned, lbs)"
                " VALUES(:cust,:qr,:cat,:rush,0,:lbs)"
            )
            params = []
            for _,r in df.iterrows():
                lbs = float(re.sub(r"[^0-9.]","",str(r.WF_LBS))) if pd.notna(r.WF_LBS) else None
                params.append({"cust":r.Customer,"qr":r.QR,"cat":r.Category,"rush":r.RushFlag,"lbs":lbs})
            conn.execute(insert_sql, params)
    except SQLAlchemyError as e:
        tb = traceback.format_exc()
        app.logger.error("DB import failed:\n%s", tb)
        return jsonify({"error":"Database import failed","details":str(e)}),500

    return jsonify({"message":f"Imported {total} rows","rush":rush,"non_rush":non_rush,"hang_dry":hang_dry}),200

# ─── ENDPOINT: SCAN ──────────────────────────────────────────────────────────
@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json() or {}
    qr = data.get("qr","" ).strip()
    if not qr:
        return jsonify({"error":"No QR code provided."}),400
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id,Customer FROM dbo.bags WHERE QR=:qr"),{"qr":qr}
            ).first()
            if not row:
                return jsonify({"error":f"Unknown QR: {qr}"}),400
            if conn.execute(text("SELECT scanned FROM dbo.bags WHERE id=:id"),{"id":row.id}).scalar():
                return jsonify({"error":"Bag already scanned."}),400
            conn.execute(text("UPDATE dbo.bags SET scanned=1 WHERE id=:id"),{"id":row.id})
    except SQLAlchemyError as e:
        tb = traceback.format_exc()
        app.logger.error("Scan failed:\n%s", tb)
        return jsonify({"error":"Scan failed","details":str(e)}),500
    return jsonify({"message":f"{row.Customer} bag ({qr}) scanned!"}),200

# ─── ENDPOINT: LIST ─────────────────────────────────────────────────────────
@app.route("/bags", methods=["GET"])
def list_bags():
    try:
        rows = engine.execute(text(
            "SELECT id,Customer,QR,Category,RushFlag,scanned FROM dbo.bags ORDER BY id"
        )).fetchall()
        data = [{"id":r.id,"customer":r.Customer,"qr":r.QR,"category":r.Category,"rush":r.RushFlag,"scanned":bool(r.scanned)} for r in rows]
    except Exception as e:
        return jsonify({"error":str(e)}),500
    return jsonify({"bags":data}),200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5001,debug=True)

