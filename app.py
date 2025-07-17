# app.py

import os
import re
import time
import json
import pandas as pd
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text

app = Flask(__name__)
CORS(app)

# ─── Database setup ────────────────────────────────────────────────────────────
# read your connection string from environment (set in App Service → Configuration)
conn_str = os.getenv("SQLAZURE")
if not conn_str:
    raise RuntimeError("Missing SQLAZURE environment variable")
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── Azure Read API Settings ─────────────────────────────────────────────────
AZURE_ENDPOINT = "https://firstone-muhammad.cognitiveservices.azure.com/"
AZURE_KEY      = os.getenv("AZURE_KEY") or "YOUR_AZURE_KEY_HERE"

# ─── Your in‑memory state ───────────────────────────────────────────────────────
bag_list     = []      # populated by /import-data
scanned_bags = []

# ─── 0) Load Excel into DB ─────────────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        # 1. Read & clean spreadsheet
        xlsx = os.path.join(os.getcwd(), "testrunrinse.xlsx")
        df = pd.read_excel(xlsx, engine="openpyxl").rename(columns=lambda x: x.strip())
        # detect columns
        date_col = next(c for c in df if "date" in c.lower())
        name_col = next(c for c in df if "customer" in c.lower())
        wf_col   = next(c for c in df if any(w in c.lower() for w in ("wf","lbs")))
        df = df[[date_col, name_col, wf_col]].dropna(subset=[date_col, name_col])
        df.columns = ["Date","Customer Name","# WF LBS"]

        # classify service
        def classify(v):
            s = str(v).strip()
            if s.isdigit(): return "Hang Dry"
            try: float(s); return "Wash and Fold"
            except: return "Wash and Fold" if "lbs" in s.lower() else "Hang Dry"
        df["Category"] = df["# WF LBS"].apply(classify)

        # clean date & rush
        df["Actual_Date"] = df["Date"].astype(str).str.replace(r"\s*TODAY\s*","",regex=True)
        today_mask = df["Date"].astype(str).str.contains("TODAY", case=False)
        rush_dates = set(df.loc[today_mask, "Actual_Date"])
        df["Rush"] = df["Actual_Date"].apply(lambda d: "RUSH" if d in rush_dates else "NON-RUSH")

        # write into SQL
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM bags"))
            for _,row in df.iterrows():
                conn.execute(text(
                    "INSERT INTO bags (date,name,category,rush,scanned) VALUES (:dt,:nm,:ct,:rs,0)"
                ), dt=row["Actual_Date"], nm=row["Customer Name"], ct=row["Category"], rs=row["Rush"])
        # update in‑memory list
        global bag_list, scanned_bags
        bag_list = list(df["Customer Name"].unique())
        scanned_bags = []
        return jsonify({"message": f"Imported {len(df)} rows"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── 1) Status ───────────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    remaining = [n for n in bag_list if n not in scanned_bags]
    return jsonify({
        "total": len(bag_list),
        "scanned": len(scanned_bags),
        "remaining": len(remaining),
        "remaining_list": remaining
    })

# ─── 2) Manual scan ─────────────────────────────────────────────────────────
@app.route("/scan", methods=["POST"])
def scan_manual():
    data = request.get_json() or {}
    name = data.get("name","").strip()
    if not name:
        return jsonify({"error":"No name provided."}), 400
    if name in scanned_bags:
        return jsonify({"error":f"{name} already scanned."}), 400
    if name not in bag_list:
        return jsonify({"error":f"{name} not in bag list."}), 400
    scanned_bags.append(name)
    # update DB as well
    with engine.begin() as conn:
        conn.execute(text("UPDATE bags SET scanned=1 WHERE name=:nm"), nm=name)
    return jsonify({"message":f"{name} scanned successfully!"})

# ─── 3) OCR scan ────────────────────────────────────────────────────────────
@app.route("/api/ocr", methods=["POST"])
def ocr_scan():
    if "image" not in request.files:
        return jsonify({"error":"No image uploaded"}),400
    img = request.files["image"].read()
    # send to Azure Read
    headers = {"Ocp-Apim-Subscription-Key":AZURE_KEY}
    r = requests.post(AZURE_ENDPOINT+"vision/v3.2/read/analyze", headers={**headers,"Content-Type":"application/octet-stream"}, data=img)
    if r.status_code not in (200,202): return jsonify({"error":"Read API failed"}),500
    op = r.headers.get("Operation-Location")
    for _ in range(15):
        j = requests.get(op, headers=headers).json()
        if j.get("status")=="succeeded": res=j["analyzeResult"]; break
        time.sleep(1)
    else: return jsonify({"error":"OCR timeout"}),500
    lines = [ln["text"] for p in res.get("readResults",[]) for ln in p.get("lines",[])]
    # match
    lname=""; low=[l.lower() for l in lines]
    for tgt in bag_list:
        words=tgt.lower().split();
        if all(any(w in ll for ll in low) for w in words): lname=tgt; break
    if not lname:
        alpha=[l for l in lines if re.fullmatch(r"[A-Za-z ]+",l)]; lname = alpha and " ".join(alpha[:2]).title() or "UNKNOWN"
    typ=next((l.upper() for l in lines if l.strip().upper() in ("HANG DRY","WASH & FOLD")),"UNKNOWN")
    return jsonify({"name":lname,"order_type":typ})

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=5001,debug=True)
