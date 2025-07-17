# app.py

import os
import time
import re
import requests
import pandas as pd
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Your in‐memory bag list & scan state ───────────────────────────────────────
bag_list     = []
scanned_bags = []

# ─── 1) GET /bags — return all bag names + scanned status ─────────────────────
@app.route("/bags", methods=["GET"])
def get_bags():
    return jsonify({
        "bags": [
            {"name": name, "scanned": (name in scanned_bags)}
            for name in bag_list
        ]
    })

# ─── 2) POST /import-data — load Excel, rebuild bag_list, clear scans ─────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        INPUT_FILE = os.path.join(os.getcwd(), "testrunrinse.xlsx")
        if not os.path.exists(INPUT_FILE):
            return jsonify({"error": f"Excel not found at {INPUT_FILE}"}), 500

        df = pd.read_excel(INPUT_FILE, engine="openpyxl")
        df = df.rename(columns=lambda x: x.strip())

        # detect columns
        date_col = next(c for c in df.columns if "date" in c.lower())
        name_col = next(c for c in df.columns if "customer" in c.lower())
        wf_col   = next(c for c in df.columns if "wf" in c.lower() or "lbs" in c.lower())

        df = df[[date_col, name_col, wf_col]]
        df.columns = ["Date", "Customer Name", "WF_LBS"]
        df = df.dropna(subset=["Date", "Customer Name"])

        # classify Category
        def classify_service(val):
            s = str(val).strip()
            if s.isdigit(): return "Hang Dry"
            try:
                float(s)
                return "Wash and Fold"
            except:
                return "Wash and Fold" if "lbs" in s.lower() else "Hang Dry"

        df["Category"] = df["WF_LBS"].apply(classify_service)

        # compute Rush vs Non‑Rush
        df["Actual_Date"] = df["Date"].astype(str).str.replace(r"\s*TODAY\s*", "", regex=True, flags=re.IGNORECASE)
        has_today = df["Date"].astype(str).str.upper().str.contains("TODAY")
        rush_days = set(df.loc[has_today, "Actual_Date"])
        df["Rush"] = df["Actual_Date"].apply(lambda d: "RUSH" if d in rush_days else "NON-RUSH")

        # rebuild in-memory lists
        global bag_list, scanned_bags
        bag_list     = df["Customer Name"].astype(str).tolist()
        scanned_bags = []

        # (optional) save a CSV snapshot
        csv_path = os.path.join(os.getcwd(), "bag_app_data.csv")
        df[["Date","Customer Name","Category","Rush"]].to_csv(csv_path, index=False)

        return jsonify({"message": f"Imported {len(bag_list)} bags from Excel."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── 3) GET /status — summary counts ───────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    remaining = [n for n in bag_list if n not in scanned_bags]
    return jsonify({
        "total":          len(bag_list),
        "scanned":        len(scanned_bags),
        "remaining":      len(remaining),
        "remaining_list": remaining
    })

# ─── 4) POST /scan — mark a bag as scanned ────────────────────────────────────
@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided."}), 400
    if name in scanned_bags:
        return jsonify({"error": f"{name} has already been scanned."}), 400
    if name not in bag_list:
        return jsonify({"error": f"{name} is not in the bag list."}), 400

    scanned_bags.append(name)
    return jsonify({"message": f"{name} scanned successfully!"})

# ─── 5) POST /api/ocr — Azure Read OCR ─────────────────────────────────────────
AZURE_ENDPOINT = "https://firstone-muhammad.cognitiveservices.azure.com/"
AZURE_KEY      = "YOUR_AZURE_CV_KEY"

@app.route("/api/ocr", methods=["POST"])
def ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img_bytes = request.files["image"].read()

    read_url = AZURE_ENDPOINT + "vision/v3.2/read/analyze"
    hdr = {
        "Ocp-Apim-Subscription-Key": AZURE_KEY,
        "Content-Type": "application/octet-stream"
    }
    r = requests.post(read_url, headers=hdr, data=img_bytes)
    if r.status_code not in (200, 202):
        return jsonify({"error": "Read API failed", "details": r.text}), 500

    op_url = r.headers.get("Operation-Location")
    if not op_url:
        return jsonify({"error": "Missing Operation-Location"}), 500

    # poll until done
    poll_h = {"Ocp-Apim-Subscription-Key": AZURE_KEY}
    for _ in range(15):
        j = requests.get(op_url, headers=poll_h).json()
        if j.get("status") == "succeeded":
            analyze = j["analyzeResult"]
            break
        if j.get("status") == "failed":
            return jsonify({"error": "OCR processing failed", "details": j}), 500
        time.sleep(1)
    else:
        return jsonify({"error": "Timeout polling OCR"}), 500

    # extract text
    lines = [ln["text"] for page in analyze.get("readResults", []) for ln in page.get("lines", [])]

    # match customer
    customer = ""
    low = [l.lower() for l in lines]
    for tgt in bag_list:
        if all(w in " ".join(low) for w in tgt.lower().split()):
            customer = tgt
            break

    if not customer:
        alpha = [l for l in lines if re.fullmatch(r"[A-Za-z ]+", l)]
        customer = " ".join(alpha[:2]).title() if alpha else "UNKNOWN"

    order_type = next(
        (l.upper() for l in lines if l.strip().upper() in ("HANG DRY", "WASH & FOLD")),
        "UNKNOWN"
    )

    return jsonify({"name": customer, "order_type": order_type})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

