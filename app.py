# app.py

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, json, re, time

app = Flask(__name__)
CORS(app)

# ─── Azure Read API Settings ─────────────────────────────────────────────────
AZURE_ENDPOINT = "https://firstone-muhammad.cognitiveservices.azure.com/"
AZURE_KEY      = "YOUR_AZURE_KEY_HERE"

# ─── Your Rush Bag List & State ───────────────────────────────────────────────
bag_list     = []  # will be populated on import-data
scanned_bags = []

# ─── 0.5) GET /import-data (initialize bag_list)
@app.route("/import-data", methods=["POST"])
def import_data():
    global bag_list, scanned_bags
    scanned_bags = []
    try:
        # load your Excel into bag_list
        import pandas as pd, os, re, time
        xlsx_path = os.path.join(os.getcwd(), "testrunrinse.xlsx")
        df = pd.read_excel(xlsx_path, engine="openpyxl").rename(columns=lambda x: x.strip())
        name_col = next(c for c in df.columns if "customer" in c.lower())
        bag_list = df[name_col].dropna().drop_duplicates().tolist()
        return jsonify({"message": f"Imported {len(bag_list)} bags from testrunrinse.xlsx"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── 1) Status Endpoint ────────────────────────────────────────────────────── ──────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def status():
    remaining = [n for n in bag_list if n not in scanned_bags]
    return jsonify({
        "total":     len(bag_list),
        "scanned":   len(scanned_bags),
        "remaining": len(remaining),
        "remaining_list": remaining
    })

# ─── 2) Manual Scan by Name ───────────────────────────────────────────────────
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

# ─── 3) OCR → Name + Order Type ───────────────────────────────────────────────
@app.route("/api/ocr", methods=["POST"])
def ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img_bytes = request.files["image"].read()

    # submit to Azure Read
    read_url = AZURE_ENDPOINT + "vision/v3.2/read/analyze"
    headers  = {
        "Ocp-Apim-Subscription-Key": AZURE_KEY,
        "Content-Type": "application/octet-stream"
    }
    r = requests.post(read_url, headers=headers, data=img_bytes)
    if r.status_code not in (200, 202):
        return jsonify({"error": "Read API failed", "details": r.text}), 500

    op_url = r.headers.get("Operation-Location")
    for _ in range(15):
        j = requests.get(op_url, headers={"Ocp-Apim-Subscription-Key": AZURE_KEY}).json()
        if j.get("status") == "succeeded":
            analyze = j["analyzeResult"]
            break
        time.sleep(1)

    lines = []
    for page in analyze.get("readResults", []):
        for ln in page.get("lines", []):
            lines.append(ln.get("text", "").strip())

    # exact match against your bag_list
    customer_name = ""
    low_lines = [l.lower() for l in lines]
    for target in bag_list:
        words = target.lower().split()
        if all(any(w in ll for ll in low_lines) for w in words):
            customer_name = target
            break

    # fallback: first two purely-alphabetic lines
    if not customer_name:
        alpha = [l for l in lines if re.fullmatch(r"[A-Za-z ]+", l)]
        if len(alpha) >= 2:
            customer_name = " ".join(w.title() for w in alpha[:2])
        elif alpha:
            customer_name = alpha[0].title()
        else:
            customer_name = "UNKNOWN"

    # find order type
    order_type = next(
        (l.upper() for l in lines
         if l.strip().upper() in ("HANG DRY", "WASH & FOLD")),
        "UNKNOWN"
    )

    return jsonify({
        "name":       customer_name,
        "order_type": order_type
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

