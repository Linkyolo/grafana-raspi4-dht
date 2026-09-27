"""
Client MQTT per AWS IoT Core (regione eu-central-1).

Il modulo si occupa solo del trasporto: il resto del monitoraggio non deve
sapere se il cloud è raggiungibile. Nessuna eccezione lascia mai questo
modulo, cosi' monitor_ambiente.py continua a scrivere su InfluxDB anche con
la rete assente.

Topic usati, tutti sotto il prefisso configurato in AWS_IOT_PREFIX:

    <prefisso>/telemetria            telemetria periodica, non retained
    <prefisso>/stato/online          "online"/"offline", retained (Last Will)
    <prefisso>/stato/ultima_lettura  ultimo payload valido, retained
    <prefisso>/comandi               comandi on-demand, non retained

I messaggi retained sopravvivono a clean_session=True: chi si connette
durante le ore in cui il Pi e' acceso vede subito l'ultima lettura senza
aspettare il publish successivo.
"""

import json
import logging
import os
import ssl
import threading
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

PORTA_IOT_MQTT = 8883
KEEPALIVE = 60
RECONNECT_MIN = 1
RECONNECT_MAX = 300
MAX_MESSAGGI_IN_CODA = 20
DEBOUNCE_LETTURA = 10
TIMEOUT_PUBBLICA = 2


def _flag(valore):
    """Accetta iTruthy più comuni nelle variabili d'ambiente."""
    return str(valore).strip().lower() in ("1", "true", "yes", "on")


class TelemetriaAWS:
    def __init__(self):
        self.enabled = _flag(os.getenv("AWS_IOT_ENABLED", "false"))
        self.endpoint = os.getenv("AWS_IOT_ENDPOINT", "").strip()
        self.thing = os.getenv("AWS_IOT_THING", "raspberry-pi-4-telemetria")
        self.prefix = os.getenv("AWS_IOT_PREFIX", "casa/raspberry_pi_4").strip("/")
        self.cert_path = os.getenv("AWS_IOT_CERT_PATH", "certs/certificate.pem.crt")
        self.key_path = os.getenv("AWS_IOT_KEY_PATH", "certs/private.pem.key")
        self.rootca_path = os.getenv("AWS_IOT_ROOTCA_PATH", "certs/AmazonRootCA1.pem")

        self.topic_telemetria = f"{self.prefix}/telemetria"
        self.topic_online = f"{self.prefix}/stato/online"
        self.topic_ultima_lettura = f"{self.prefix}/stato/ultima_lettura"
        self.topic_comandi = f"{self.prefix}/comandi"

        # Acceso dal thread di rete di paho, consumato dal main loop. Il
        # callback non legge mai il sensore: il DHT11 non e' thread safe e il
        # main loop potrebbe starlo leggendo nello stesso momento.
        self.richiesta_lettura = threading.Event()

        self._client = None
        self._connesso = False
        self._ultima_lettura_forzata = 0.0

        if not self.enabled:
            log.info("Ramo AWS IoT disabilitato (AWS_IOT_ENABLED=false).")
            return

        try:
            self._verifica_credenziali()
            self._costruisci_client()
        except Exception as e:
            # Fallire qui e' preferibile a fallire al primo publish: cosi'
            # l'errore di configurazione si vede all'avvio dello script.
            log.error("Client AWS IoT non inizializzato: %s", e)
            self.enabled = False

    def _verifica_credenziali(self):
        if not self.endpoint:
            raise RuntimeError("AWS_IOT_ENDPOINT mancante nel .env")
        # Non pretendiamo di riconoscere la forma dell'endpoint: che regione
        # usare e' una scelta della policy, non del client, e l'handshake TLS
        # fallisce comunque da solo se endpoint e certificato non combaciano.
        # Si segnala solo ciò che è un errore di incolla: la porta la sceglie
        # il client (8883) e il path non esiste in AWS IoT Core.
        if any(c in self.endpoint for c in ("://", " ", "/", ":")):
            raise RuntimeError(
                f"AWS_IOT_ENDPOINT '{self.endpoint}' deve essere il solo hostname, "
                "senza schema, porta o percorso"
            )
        if ".iot." not in self.endpoint or not self.endpoint.endswith(".amazonaws.com"):
            log.warning(
                "AWS_IOT_ENDPOINT '%s' non ha la forma tipica di un endpoint IoT "
                "(<account>-ats.iot.<regione>.amazonaws.com).",
                self.endpoint,
            )
        mancanti = [
            p
            for p in (self.cert_path, self.key_path, self.rootca_path)
            if not os.path.isfile(p)
        ]
        if mancanti:
            raise RuntimeError(
                f"file di credenziali non trovati: {', '.join(mancanti)}"
            )

    def _costruisci_client(self):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.thing,
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )

        # La catena dei certificati e' verificata contro Amazon Root CA 1
        # (verify_mode CERT_REQUIRED). Il controllo del nome host e' disattivato
        # perche' il certificato del device e' emesso a nome della thing, non
        # per il DNS dell'endpoint: con check_hostname attivo l'handshake
        # fallirebbe. Non e' un indebolimento della verifica della catena.
        ctx = ssl.create_default_context(cafile=self.rootca_path)
        ctx.check_hostname = False
        ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        client.tls_set_context(ctx)

        client.will_set(self.topic_online, b"offline", qos=0, retain=True)
        client.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)
        # Coda limitata: durante un'assenza prolungata di rete una coda
        # illimitata accumula messaggi in memoria senza mai svuotarsi.
        client.max_queued_messages_set(MAX_MESSAGGI_IN_CODA)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client

    def connetti(self):
        if not self.enabled or self._client is None:
            return False
        try:
            # connect_async non blocca: se il broker non e' raggiungibile
            # riprova da sola con il backoff configurato in loop_start.
            self._client.connect_async(self.endpoint, PORTA_IOT_MQTT, KEEPALIVE)
            self._client.loop_start()
            return True
        except Exception as e:
            log.error("Connessione ad AWS IoT non riuscita: %s", e)
            return False

    def chiudi(self):
        # Il controllo e' su _client e non su enabled: se il ramo cloud viene
        # disattivato a runtime mentre la connessione e' viva, la disconnessione
        # pulita deve comunque avvenire, altrimenti si resta appesi al keepalive.
        if self._client is None:
            return
        try:
            # retained 'offline' esplicito: segnala la chiusura pulita
            # immediatamente, senza aspettare il keepalive del broker.
            self._pubblica(self.topic_online, b"offline", retain=True, attesa=True)
            self._client.disconnect()
            self._client.loop_stop()
        except Exception as e:
            log.warning("Chiusura client AWS IoT non pulita: %s", e)
        finally:
            self._connesso = False

    def connesso(self):
        return self._connesso

    def abilita_log(self):
        """Attiva il log interno di paho: CONNECT, CONNACK, TLS, SUBSCRIBE.

        In paho 2.x il logger resta None finche' enable_logger() non viene
        chiamato esplicitamente: senza questo, un client che non si connette
        non lascia alcuna traccia e il log del servizio dice solo 'non
        connesso'. E' la differenza fra diagnosticare e indovinare.
        """
        if self._client is not None:
            self._client.enable_logger()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("AWS IoT ha rifiutato la connessione: %s", reason_code)
            self._connesso = False
            return
        self._connesso = True
        log.info("Connesso a AWS IoT (%s).", self.endpoint)
        try:
            client.subscribe(self.topic_comandi, qos=0)
        except Exception as e:
            log.warning("Sottoscrizione ai comandi fallita: %s", e)
        self._pubblica(self.topic_online, b"online", retain=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connesso = False
        # Una disconnessione ordinata e' la norma (chiudi(), o il broker che
        # chiude): segnalarla come warning renderebbe il log illeggibile.
        if getattr(reason_code, "is_failure", False):
            log.warning("Disconnesso da AWS IoT (%s).", reason_code)
        else:
            log.info("Disconnesso da AWS IoT.")

    def _on_message(self, client, userdata, message):
        # Difesa esplicita: un broker può recapitare retained di altri topic
        # (stato/ultima_lettura, stato/online) senza che siano richiesti, e
        # vanno trattati come rumore, non come comandi.
        if message.topic != self.topic_comandi:
            log.debug(
                "Messaggio ignorato su %s: non è il topic dei comandi.", message.topic
            )
            return
        try:
            comando = json.loads(message.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log.warning(
                "Comando ignorato, payload non JSON su %s: %s", message.topic, e
            )
            return
        if not isinstance(comando, dict):
            log.warning(
                "Comando ignorato, atteso un oggetto JSON su %s.", message.topic
            )
            return
        if comando.get("cmd") == "leggi_ora":
            self._richiedi_lettura("comando on-demand")
        else:
            log.warning(
                "Comando sconosciuto su %s: %r", message.topic, comando.get("cmd")
            )

    def _richiedi_lettura(self, motivo):
        ora = time.monotonic()
        trascorso = ora - self._ultima_lettura_forzata
        if trascorso < DEBOUNCE_LETTURA:
            log.info(
                "Richiesta (%s) ignorata: ultima forzata %.0fs fa, debounce %ds.",
                motivo,
                trascorso,
                DEBOUNCE_LETTURA,
            )
            return
        self._ultima_lettura_forzata = ora
        log.info("Richiesta di lettura su misura (%s).", motivo)
        self.richiesta_lettura.set()

    def _pubblica(self, topic, payload, retain=False, attesa=False):
        if self._client is None:
            return False
        try:
            info = self._client.publish(topic, payload, qos=0, retain=retain)
            if attesa:
                info.wait_for_publish(timeout=TIMEOUT_PUBBLICA)
            return True
        except Exception as e:
            log.warning("Publish su %s non riuscito: %s", topic, e)
            return False

    def pubblica_telemetria(self, payload):
        if not self.enabled:
            return False
        return self._pubblica(
            self.topic_telemetria, json.dumps(payload).encode("utf-8")
        )

    def pubblica_ultima_lettura(self, payload):
        if not self.enabled:
            return False
        return self._pubblica(
            self.topic_ultima_lettura, json.dumps(payload).encode("utf-8"), retain=True
        )

    def stato(self):
        return {
            "abilitato": self.enabled,
            "connesso": self._connesso,
            "endpoint": self.endpoint,
            "topic_telemetria": self.topic_telemetria,
            "topic_comandi": self.topic_comandi,
            "lettura_pending": self.richiesta_lettura.is_set(),
        }
