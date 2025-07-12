# app.py
import os
import time
import re
import requests
import urllib
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine

app = Flask(__name__)
CORS(app)

# ─── Database setup ────────────────────────────────────────────────────────────
# Read raw ODBC connection from environment, then URL-encode it
raw = os.environ["SQLAZURE"]
conn_str = urllib.parse.quote_plus(raw)
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── Azure Read API Settings ─────────────────────────────────────────────────
AZURE_ENDPOINT = "https://firstone-muhammad.cognitiveservices.azure.com/"
AZURE_KEY      = "YOUR_AZURE_COMPUTER_VISION_KEY"

# ─── Your Rush Bag List & State ───────────────────────────────────────────────
bag_list     = ["Allie Gorti", "Danielle Marshall", "Hugh Cochran"]
scanned_bags = []

# 1) Status Endpoint
@app.route("/status", methods=["GET"])
def status():
    remaining = [n for n in bag_list if n not in scanned_bags]
    return jsonify({
        "total":     len(bag_list),
        "scanned":   len(scanned_bags),
        "remaining": len(remaining),
    })

# 2) Manual Scan by Name
@app.route("/scan", methods=["POST"])
def scan():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "No name provided."}), 400
    if name in scanned_bags:
        return jsonify({"error": f"{name} already scanned."}), 400
    if name not in bag_list:
        return jsonify({"error": f"{name} is not in list."}), 400

    scanned_bags.append(name)
    return jsonify({"message": f"{name} scanned!"})

# 3) OCR → Name + Order Type
@app.route("/api/ocr", methods=["POST"])
def ocr():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    img = request.files["image"].read()

    # submit to Azure Read
    url = AZURE_ENDPOINT + "vision/v3.2/read/analyze"
    hdr = {"Ocp-Apim-Subscription-Key": AZURE_KEY,
           "Content-Type": "application/octet-stream"}
    r = requests.post(url, headers=hdr, data=img)
    if r.status_code not in (200,202):
        return jsonify({"error":"Read API failed","details":r.text}),500
    op = r.headers["Operation-Location"]

    # poll
    hdr2 = {"Ocp-Apim-Subscription-Key": AZURE_KEY}
    for _ in range(15):
        j = requests.get(op, headers=hdr2).json()
        if j["status"]=="succeeded":
            res = j["analyzeResult"]
            break
        time.sleep(1)
    else:
        return jsonify({"error":"Timeout polling OCR"}),500

    # extract text
    lines = [ln["text"] for p in res["readResults"] for ln in p["lines"]]

    # match against bag_list
    customer = ""
    low = [l.lower() for l in lines]
    for tgt in bag_list:
        if all(w in " ".join(low) for w in tgt.lower().split()):
            customer = tgt
            break

    if not customer:
        alpha = [l for l in lines if re.fullmatch(r"[A-Za-z ]+",l)]
        customer = " ".join(alpha[:2]).title() if alpha else "UNKNOWN"

    order_type = next(
      (l.upper() for l in lines
        if l.strip().upper() in ("HANG DRY","WASH & FOLD")),
      "UNKNOWN"
    )
    return jsonify({"name":customer,"order_type":order_type})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
