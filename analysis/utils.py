# utils.py
import pandas as pd
from influxdb_client import InfluxDBClient


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


def fetch_influx_data(query, url, token, org):
    """
    query su InfluxDB e restituisce un DataFrame.
    """
    client = InfluxDBClient(url=url, token=token, org=org)
    query_api = client.query_api()

    df = query_api.query_data_frame(query)

    client.close()
    return df
