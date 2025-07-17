# app.py

import os
import time
import re
import requests
import pandas as pd
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text

app = Flask(__name__)
CORS(app)

# ─── Azure Read API Settings ───────────────────────────────────────────────────
AZURE_ENDPOINT = os.environ.get(
    "AZURE_ENDPOINT",
    "https://firstone-muhammad.cognitiveservices.azure.com/"
)
AZURE_KEY = os.environ.get("AZURE_KEY", "YOUR_KEY_HERE")

# ─── DATABASE SETUP ────────────────────────────────────────────────────────────
# Read your ODBC connection string from App Service Configuration → Connection strings
env_conn = os.environ.get("SQLAZURE")
if not env_conn:
    raise RuntimeError("Missing SQLAZURE connection string in App Service configuration")
conn_str = env_conn
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── In‑memory backup lists (for import‑data) ───────────────────────────────────
bag_list = []
scanned_bags = []

# ─── 0) GET /bags — list all bags + scan state ─────────────────────────────────
@app.route("/bags", methods=["GET"])
def get_bags():
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name, scanned FROM bags")).fetchall()
        return jsonify({
            "bags": [{"name": r[0], "scanned": bool(r[1])} for r in rows]
        })
    except Exception:
        # fallback to in-memory
        return jsonify({
            "bags": [
                {"name": name, "scanned": (name in scanned_bags)}
                for name in bag_list
            ]
        })

# ─── 1) POST /import-data — load Excel, rebuild DB & in-memory lists ─────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        xlsx_path = os.path.join(os.getcwd(), "testrunrinse.xlsx")
        if not os.path.exists(xlsx_path):
            return jsonify({"error": f"Excel not found at {xlsx_path}"}), 500

        # load DataFrame (requires openpyxl in requirements.txt)
        df = pd.read_excel(xlsx_path, engine="openpyxl")
        df = df.rename(columns=lambda x: x.strip())

        # pick columns
        date_col = next(c for c in df.columns if "date" in c.lower())
        name_col = next(c for c in df.columns if "customer" in c.lower())
        wf_col = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())
        df = df[[date_col, name_col, wf_col]]
        df.columns = ["Date", "Customer Name", "WF_LBS"]
        df = df.dropna(subset=["Date", "Customer Name"])

        # classify service
        def classify_service(v):
            s = str(v).strip()
            if s.isdigit():
                return "Hang Dry"
            try:
                float(s)
                return "Wash and Fold"
            except:
                return "Wash and Fold" if "lbs" in s.lower() else "Hang Dry"
        df["Category"] = df["WF_LBS"].apply(classify_service)

        # determine rush
        df["Actual_Date"] = df["Date"].astype(str).str.replace(
            r"\s*TODAY\s*", "", regex=True, flags=re.IGNORECASE
        )
        has_today = df["Date"].astype(str).str.upper().str.contains("TODAY")
        rush_days = set(df.loc[has_today, "Actual_Date"])
        df["Rush"] = df["Actual_Date"].apply(
            lambda d: "RUSH" if d in rush_days else "NON-RUSH"
        )

        # rebuild DB
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS bags"))
            conn.execute(text(
                "CREATE TABLE bags (name NVARCHAR(200) PRIMARY KEY, scanned BIT NOT NULL)"
            ))
            for name in df["Customer Name"]:
                conn.execute(
                    text("INSERT INTO bags (name, scanned) VALUES (:n, 0)"),
                    {"n": name}
                )

        # update in‑memory lists
        global bag_list, scanned_bags
        bag_list = df["Customer Name"].astype(str).tolist()
        scanned_bags = []

        return jsonify({"message": f"Imported {len(bag_list)} bags"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── 2) GET /status — summary counts ───────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    try:
        with engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM bags")).scalar()
            scanned = conn.execute(text("SELECT COUNT(*) FROM bags WHERE scanned = 1")).scalar()
            remaining = total - scanned
        return jsonify({"total": total, "scanned": scanned, "remaining": remaining})
    except Exception:
        remaining = [n for n in bag_list if n not in scanned_bags]
        return jsonify({
            "total": len(bag_list),
            "scanned": len(scanned_bags),
            "remaining": len(remaining)
        })

# ─── 3) POST /scan — mark a bag scanned ────────────────────────────────────────
@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided."}), 400
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT COUNT(*) FROM bags WHERE name = :n"), {"n": name}
        ).scalar()
        if not exists:
            return jsonify({"error": f"{name} is not in list."}), 400
        already = conn.execute(
            text("SELECT scanned FROM bags WHERE name = :n"), {"n": name}
        ).scalar()
        if already:
            return jsonify({"error": f"{name} already scanned."}), 400
        conn.execute(
            text("UPDATE bags SET scanned = 1 WHERE name = :n"), {"n": name}
        )
    return jsonify({"message": f"{name} scanned successfully!"})

# ─── 4) POST /api/ocr — Azure OCR ─────────────────────────────────────────────
@app.route("/api/ocr", methods=["POST"])
def ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img = request.files["image"].read()

    read_url = AZURE_ENDPOINT + "vision/v3.2/read/analyze"
    hdr = {"Ocp-Apim-Subscription-Key": AZURE_KEY,
           "Content-Type": "application/octet-stream"}
    r = requests.post(read_url, headers=hdr, data=img)
    if r.status_code not in (200, 202):
        return jsonify({"error": "Read API failed", "details": r.text}), 500

    op_url = r.headers.get("Operation-Location")
    if not op_url:
        return jsonify({"error": "Missing Operation-Location"}), 500

    poll_h = {"Ocp-Apim-Subscription-Key": AZURE_KEY}
    for _ in range(15):
        j = requests.get(op_url, headers=poll_h).json()
        if j.get("status") == "succeeded":
            analyze = j["analyzeResult"]
            break
        time.sleep(1)
    else:
        return jsonify({"error": "Timeout polling OCR"}), 500

    lines = [ln["text"] for p in analyze.get("readResults", []) for ln in p.get("lines", [])]
    # (reuse your matching logic here to set `customer` and `order_type`)
    return jsonify({"name": customer, "order_type": order_type})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

