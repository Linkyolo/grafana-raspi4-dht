import adafruit_dht
import board
import psutil
import os
import time
import logging
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from aws_iot_publisher import TelemetriaAWS

load_dotenv()

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "casa")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "monitoraggio_pi")

INTERVALLO_DHT = 30
INTERVALLO_SISTEMA = 10
HOST_TAG = "raspberry_pi_4"

# Frequenza di pubblicazione su AWS IoT Core, indipendente dalle frequenze
# locali: la telemetria cloud non rilegge il DHT11, riusa gli ultimi valori.
INTERVALLO_CLOUD = int(os.getenv("INTERVALLO_CLOUD", "60"))

# Cache dell'ultima lettura valida per dominio. Con INTERVALLO_CLOUD=60 e
# INTERVALLO_DHT=30 la cache viene letta senza mai rincorrere il sensore.
_ultimo_ambiente = {}
_ultimo_sistema = {}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

sensor = adafruit_dht.DHT11(board.D12)

client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)

aws = TelemetriaAWS()


def calcola_percepita(T, H):
    hi = 0.5 * (T + 61.0 + ((T - 18.0) * 1.2) + (H * 0.094))
    if hi > 26:
        c1, c2, c3 = -8.78469475556, 1.61139411, 2.33854883889
        c4, c5, c6 = -0.14611605, -0.012308094, -0.0164248277778
        c7, c8, c9 = 0.002211732, 0.00072546, -0.000003582
        hi = (
            c1
            + (c2 * T)
            + (c3 * H)
            + (c4 * T * H)
            + (c5 * T**2)
            + (c6 * H**2)
            + (c7 * T**2 * H)
            + (c8 * T * H**2)
            + (c9 * T**2 * H**2)
        )
    return round(hi, 1)


def get_cpu_temp():
    res = os.popen("vcgencmd measure_temp").readline()
    return float(res.replace("temp=", "").replace("'C\n", ""))


def leggi_e_invia_ambiente():
    try:
        temp = sensor.temperature
        hum = sensor.humidity
        if temp is not None and hum is not None:
            percepita = calcola_percepita(temp, hum)
            point = (
                Point("ambiente")
                .tag("host", HOST_TAG)
                .tag("stanza", "camera")
                .field("temperatura", float(temp))
                .field("umidita", float(hum))
                .field("percepita", float(percepita))
            )
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
            _ultimo_ambiente.update(
                temperatura=float(temp),
                umidita=float(hum),
                percepita=float(percepita),
            )
            log.info(f"Ambiente -> T:{temp}°C U:{hum}% P:{percepita}°C")
    except RuntimeError as e:
        log.warning(f"Lettura DHT fallita (normale, si riprova): {e}")
    except Exception as e:
        log.error(f"Errore imprevisto lettura ambiente: {e}")


def leggi_e_invia_sistema():
    try:
        cpu_usage = psutil.cpu_percent(interval=1)
        ram_usage = psutil.virtual_memory().percent
        cpu_temp = get_cpu_temp()
        point = (
            Point("risorse_sistema")
            .tag("host", HOST_TAG)
            .field("cpu_load", cpu_usage)
            .field("ram_load", ram_usage)
            .field("cpu_temp", cpu_temp)
        )
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        _ultimo_sistema.update(
            cpu_load=float(cpu_usage),
            ram_load=float(ram_usage),
            cpu_temp=float(cpu_temp),
        )
        log.info(f"Sistema -> CPU:{cpu_usage}% Temp:{cpu_temp}°C RAM:{ram_usage}%")
    except Exception as e:
        log.error(f"Errore imprevisto lettura sistema: {e}")


def costruisci_payload():
    """Assembla il payload AWS IoT dalle cache. None se i dati non bastano."""
    if not _ultimo_ambiente or not _ultimo_sistema:
        return None
    return {
        "ts": int(time.time()),
        "host": HOST_TAG,
        **_ultimo_ambiente,
        **_ultimo_sistema,
    }


def pubblica_cloud(forzato=False):
    """Pubblica la telemetria su AWS IoT Core. Non solleva mai eccezioni.

    Va senza eccezioni di proposito: il ramo cloud non deve poter fermare la
    scrittura su InfluxDB. Un fallimento AWS degrada il monitoraggio cloud,
    non quello locale.
    """
    if not aws.enabled:
        return False
    payload = costruisci_payload()
    if payload is None:
        if forzato:
            log.warning("Lettura on-demand scartata: nessuna misura valida in cache.")
        return False
    try:
        if aws.pubblica_telemetria(payload):
            aws.pubblica_ultima_lettura(payload)
            log.info(
                "Cloud -> T:%s°C U:%s%% CPU:%s%% RAM:%s%%%s",
                payload["temperatura"],
                payload["umidita"],
                payload["cpu_load"],
                payload["ram_load"],
                " (on-demand)" if forzato else "",
            )
            return True
    except Exception as e:
        log.error(f"Pubblicazione su AWS IoT non riuscita: {e}")
    return False


def main():
    log.info("Avvio monitoraggio ambiente + sistema...")
    aws.connetti()
    ultimo_dht = 0
    ultimo_sistema = 0
    ultimo_cloud = 0

    while True:
        ora = time.time()

        if ora - ultimo_dht >= INTERVALLO_DHT:
            leggi_e_invia_ambiente()
            ultimo_dht = ora

        if ora - ultimo_sistema >= INTERVALLO_SISTEMA:
            leggi_e_invia_sistema()
            ultimo_sistema = ora

        # Comando on-demand ricevuto via MQTT: il callback di rete ha solo
        # acceso il flag, la lettura avviene qui per non toccare il DHT11
        # da un thread concorrente.
        if aws.richiesta_lettura.is_set():
            aws.richiesta_lettura.clear()
            leggi_e_invia_ambiente()
            leggi_e_invia_sistema()
            ultimo_dht = ultimo_sistema = time.time()
            if pubblica_cloud(forzato=True):
                ultimo_cloud = time.time()

        elif ora - ultimo_cloud >= INTERVALLO_CLOUD:
            if pubblica_cloud():
                ultimo_cloud = ora

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Script fermato manualmente.")
    finally:
        aws.chiudi()
        client.close()
