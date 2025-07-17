# app.py
import os, re, time
import pandas as pd
from flask import Flask, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# ─── App + CORS ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ─── DB SETUP ─────────────────────────────────────────────────────────────────
# Make sure in Azure’s App Settings you have:
#   Name=SQLAZURE  Value=Driver={ODBC Driver 18 for SQL Server};Server=tcp:…;Database=…;Uid=…;Pwd=REAL_PASSWORD;Encrypt=yes;TrustServerCertificate=no;
conn_str = os.environ["SQLAZURE"]
engine   = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")

# ─── EXCEL → CSV IMPORT LOGIC ─────────────────────────────────────────────────
INPUT_FILE  = "testrunrinse.xlsx"

def load_and_prepare():
    df = pd.read_excel(INPUT_FILE)
    # normalize headers
    df.columns = [c.strip() for c in df.columns]

    # identify columns
    date_col = next(c for c in df if "date" in c.lower())
    name_col = next(c for c in df if "customer" in c.lower())
    wf_col   = next(c for c in df if "wf" in c.lower() or "lbs" in c.lower())

    # select & rename
    df = df[[date_col, name_col, wf_col]]
    df.columns = ["Date", "Customer", "WF_LBS"]

    # drop rows missing both
    df = df.dropna(subset=["Date", "Customer"], how="all")

    # classify Category
    def classify_service(val):
        s = str(val).strip()
        if s.isdigit() or float(s or 0) == 0:
            return "Hang Dry"
        return "Wash & Fold"
    df["Category"] = df["WF_LBS"].apply(classify_service)

    # mark if row has TODAY token
    df["HasTODAY"] = df["Date"].astype(str).str.upper().str.contains("TODAY")

    # extract the bare date string (for comparison)
    df["BareDate"] = df["Date"].astype(str).replace(r".*TODAY\s*","", regex=True).str.strip()

    # compute all rush-dates (any row where HasTODAY is True)
    rush_dates = set(df.loc[df["HasTODAY"], "BareDate"])

    # final Rush flag
    def is_rush(row):
        if row["HasTODAY"]:
            return "RUSH"
        # also if its date matches any rush date
        if row["BareDate"] in rush_dates:
            return "RUSH"
        return "NON-RUSH"
    df["RushFlag"] = df.apply(is_rush, axis=1)

    return df

# ─── ENDPOINT: /import-data ───────────────────────────────────────────────────
@app.route("/import-data", methods=["POST"])
def import_data():
    try:
        df = load_and_prepare()
    except Exception as e:
        return jsonify({"error": f"Excel load failed: {e}"}), 500

    total = len(df)
    rush_cnt = int((df["RushFlag"]=="RUSH").sum())
    nonrush_cnt = total - rush_cnt
    hangdry_cnt = int((df["Category"]=="Hang Dry").sum())

    with engine.begin() as conn:
        # 1) ensure table has an IDENTITY PK
        conn.execute(text("""
        IF OBJECT_ID('dbo.bags','U') IS NULL
          CREATE TABLE dbo.bags(
            id INT IDENTITY(1,1) PRIMARY KEY,
            Customer NVARCHAR(200),
            Category NVARCHAR(50),
            RushFlag NVARCHAR(10)
          );
        """))

        # 2) truncate before reload
        conn.execute(text("TRUNCATE TABLE dbo.bags"))

        # 3) bulk insert
        insert_sql = text("INSERT INTO dbo.bags(Customer,Category,RushFlag) VALUES(:c,:cat,:r)")
        conn.execute(
            insert_sql,
            [
                {"c": row.Customer, "cat": row.Category, "r": row.RushFlag}
                for _, row in df.iterrows()
            ]
        )

    return jsonify({
        "message":    f"Imported {total} rows",
        "rush":       rush_cnt,
        "non_rush":   nonrush_cnt,
        "hang_dry":   hangdry_cnt
    })

# ─── STATUS: /bags ─────────────────────────────────────────────────────────────
@app.route("/bags", methods=["GET"])
def get_bags():
    rows = engine.execute(text("SELECT Customer, Category, RushFlag FROM dbo.bags")).fetchall()
    return jsonify([{"customer":r.Customer, "category":r.Category, "rush":r.RushFlag} for r in rows])

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
