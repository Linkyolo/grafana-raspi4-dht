# utils.py
import pandas as pd
from influxdb_client import InfluxDBClient
import os
from dotenv import load_dotenv

# Carica le variabili di ambiente dal file .env nella cartella padre
dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(dotenv_path)

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "casa")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "monitoraggio_pi")


if not INFLUX_TOKEN:
    print("Errore: INFLUX_TOKEN non trovato! WTF .env.")
else:
    print(f"Configurazione caricata correttamente!")
    print(f" URL: {INFLUX_URL} |  Bucket: {INFLUX_BUCKET}")

query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -3h)
  |> filter(fn: (r) => r["_measurement"] == "ambiente")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
'''


def load_and_clean_data(file_path):
    """
    Carica il file CSV, esegue il pivot delle serie temporali
    e uniforma la frequenza su base minuto.
    """
    df = pd.read_csv(file_path, parse_dates=["_time"])

    # Pivot Table: righe di _field in colonne
    df_pivot = df.pivot_table(
        index="_time", columns="_field", values="_value", aggfunc="mean"
    )

    # Resample: per serie temporali disomogenee
    # Uniformiamo tutto su base minuto e interpoliamo i buchi.
    return df_pivot.resample("min").mean().interpolate(method="linear")


def fetch_influx_data(query):
    """
    query su InfluxDB e restituisce un DataFrame.
    """
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    df = query_api.query_data_frame(query)

    client.close()
    return df
