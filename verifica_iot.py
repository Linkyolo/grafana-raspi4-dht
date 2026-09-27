"""
Verifiche MQTT per il setup AWS IoT Core, da usare mentre si configura.

Non serve installare nulla: usa paho-mqtt, che e' gia' una dipendenza del
progetto, e la stessa configurazione di aws_iot_publisher.py (letta dal .env).
Ogni subcomando esce con 0 se va a buon fine, 1 se qualcosa non torna, cosi'
si puo' usare anche in uno script.

    python verifica_iot.py connessione
    python verifica_iot.py ascolta --durata 60
    python verifica_iot.py pubblica
    python verifica_iot.py leggi_ora
"""

import argparse
import json
import logging
import os
import ssl
import sys
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

from aws_iot_publisher import (  # noqa: E402
    DEBOUNCE_LETTURA,
    KEEPALIVE,
    PORTA_IOT_MQTT,
    TelemetriaAWS,
)

log = logging.getLogger("verifica_iot")

# Valori volutamente riconoscibili: se li vedi in CloudWatch, vengono da qui
# e non dal sensore.
PAYLOAD_DI_TESTO = {
    "temperatura": 11.1,
    "umidita": 22.2,
    "percepita": 11.1,
    "cpu_load": 1.0,
    "ram_load": 2.0,
    "cpu_temp": 3.0,
}

TIMEOUT_CONNESSIONE = 20


def _client_id_verifica():
    """Client id usato dai subcomandi ausiliari.

    Diverso dalla thing di proposito: se il tool usasse lo stesso client id del
    Raspberry, AWS IoT chiuderebbe la connessione del device a ogni test (un
    solo client id attivo per volta) e, morendo, il device lascerebbe via il
    proprio Last Will un 'offline' retained che riguarda il device e non il
    test. L'identita' si separa quindi per client id, non per certificato.
    """
    return os.getenv("AWS_IOT_CLIENT_VERIFICA", "").strip() or (
        f"verifica-{os.getenv('AWS_IOT_THING', 'raspberry-pi-4-telemetria').strip()}"
    )


def _ambiente():
    """Controlli preventivi, con i motivi per cui falliscono."""
    problemi = []
    for chiave, default in (
        ("AWS_IOT_ENDPOINT", ""),
        ("AWS_IOT_CERT_PATH", "certs/certificate.pem.crt"),
        ("AWS_IOT_KEY_PATH", "certs/private.pem.key"),
        ("AWS_IOT_ROOTCA_PATH", "certs/AmazonRootCA1.pem"),
    ):
        valore = os.getenv(chiave, default).strip()
        if not valore:
            problemi.append(f"{chiave} non impostata")
        elif "/" in valore and not os.path.isfile(valore):
            problemi.append(f"{chiave} punta a '{valore}', che non esiste")
    if any(c in os.getenv("AWS_IOT_ENDPOINT", "") for c in ("://", ":", " ")):
        problemi.append(
            "AWS_IOT_ENDPOINT deve essere il solo hostname, senza schema o porta"
        )
    return problemi


def _client_mqtt(client_id, prefix, on_messaggio=None, verbose=False):
    """Client ausiliario con lo stesso TLS del client di produzione.

    Serve sia per ascoltare sia per pubblicare: usa il certificato del device,
    cosi' verificare che il broker lo accetti e' essa stessa una prova.
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    ctx = ssl.create_default_context(
        cafile=os.getenv("AWS_IOT_ROOTCA_PATH", "certs/AmazonRootCA1.pem")
    )
    ctx.check_hostname = False
    ctx.load_cert_chain(
        certfile=os.getenv("AWS_IOT_CERT_PATH", "certs/certificate.pem.crt"),
        keyfile=os.getenv("AWS_IOT_KEY_PATH", "certs/private.pem.key"),
    )
    client.tls_set_context(ctx)
    if on_messaggio is not None:
        client.on_message = on_messaggio
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    if verbose:
        # Senza questo paho non emette una riga: il suo logger resta None
        # finche' enable_logger() non viene chiamato esplicitamente.
        client.enable_logger()

    def su_connessione(c, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("Broker ha rifiutato la connessione: %s", reason_code)
            return
        c.subscribe(f"{prefix}/#", qos=0)
        log.info("Connesso, in ascolto su %s/#", prefix)

    client.on_connect = su_connessione
    return client


def _connetti(client, endpoint):
    """Torna True se la connessione sale entro un tempo ragionevole."""
    try:
        client.connect(endpoint, PORTA_IOT_MQTT, KEEPALIVE)
    except Exception as e:
        log.error("Connessione fallita: %s", e)
        return False
    client.loop_start()
    limite = time.time() + TIMEOUT_CONNESSIONE
    while time.time() < limite and not client.is_connected():
        time.sleep(0.3)
    return client.is_connected()


def cmd_connessione(args):
    """Verifica TLS, endpoint e policy: arriva al CONNECT?"""
    problemi = _ambiente()
    if problemi:
        for p in problemi:
            log.error(p)
        return 1

    aws = TelemetriaAWS()
    if not aws.enabled:
        log.error("Client AWS IoT non inizializzato. Vedi il messaggio sopra.")
        return 1

    print(f"endpoint : {aws.endpoint}")
    print(f"client id: {aws.thing}")
    print(f"prefisso : {aws.prefix}")
    print(f"certs    : {aws.cert_path}")
    print("")

    if args.verbose:
        aws.abilita_log()

    aws.connetti()
    limite = time.time() + args.timeout
    while time.time() < limite and not aws.connesso():
        time.sleep(0.3)

    if not aws.connesso():
        print("")
        print(f"FALLITO: nessun CONNECT accepted entro {args.timeout}s.")
        print("")
        print("Il distinguere due cause, perche' il sintomo e' identico:")
        print("  1. il CONNECT e' partito ma il broker non ha risposto CONNACK.")
        print("     Il client e' stato rifiutato: quasi sempre la policy. Con un")
        print("     Last Will retained, la causa piu' frequente e' un")
        print("     iot:RetainPublish mancante, oppure il topic del Will non")
        print("     coperto da iot:Publish. Serve un ARN che finisca con '/*'.")
        print("  2. il CONNECT non e' mai partito: rete o TLS.")
        print("     Rilancia con --verbose: se vedi 'Sending CONNECT' senza")
        print("     'Received CONNACK' e' il caso 1, se non vedi nulla e' il 2.")
        print("")
        print("Cosa non e' la causa, con questa configurazione: DNS, porta 8883,")
        print("catena TLS e orologio sono gia' stati verificati al primo avvio.")
        print("Vedi SETUP_AWS.md, sezione 11.")
        return 1

    print("OK: connesso al broker.")
    print("    La policy permette i CONNECT per questo client id.")
    print("    Se sei su una rete che blocca la 8883 in uscita, il fallimento")
    print("    arriva prima: timeout invece di rifiuto.")
    aws.chiudi()
    return 0


def cmd_ascolta(args):
    """Osserva i topic: retained, Last Will e telemetria."""
    problemi = _ambiente()
    if problemi:
        for p in problemi:
            log.error(p)
        return 1

    prefix = os.getenv("AWS_IOT_PREFIX", "casa/raspberry_pi_4").strip("/")
    ricevuti = []
    sottoscrizione = {}

    def su_messaggio(c, userdata, msg):
        ora = time.strftime("%H:%M:%S")
        etichetta = " [retained]" if msg.retain else ""
        testo = msg.payload.decode("utf-8", errors="replace")
        if len(testo) > 160:
            testo = testo[:157] + "..."
        print(f"{ora}  {msg.topic}{etichetta}\n        {testo}")
        ricevuti.append((msg.topic, testo, msg.retain))

    def su_sottoscrizione(c, userdata, mid, reason_codes, properties=None):
        sottoscrizione["motivi"] = [str(r) for r in reason_codes]
        sottoscrizione["ok"] = not any(
            getattr(r, "is_failure", False) for r in reason_codes
        )

    client = _client_mqtt(
        _client_id_verifica(), prefix, su_messaggio, getattr(args, "verbose", False)
    )
    client.on_subscribe = su_sottoscrizione
    if not _connetti(client, os.getenv("AWS_IOT_ENDPOINT")):
        client.loop_stop()
        print("")
        print("FALLITO: il client di verifica non si e' connesso.")
        print(f"Client id usato: {client._client_id.decode()}")
        print("La policy deve avere iot:Connect su client/<quel nome>.")
        print("Vedi SETUP_AWS.md, sezione 4.")
        return 1

    # AWS non risponde affatto a una SUBSCRIBE non autorizzata: niente SUBACK,
    # niente errore, e il client resta in ascolto del vuoto. Senza questo
    # controllo 'nessun messaggio' e 'non sei autorizzato' sono indistinguibili.
    limite_suback = time.time() + 5
    while "ok" not in sottoscrizione and time.time() < limite_suback:
        time.sleep(0.1)
    if "ok" not in sottoscrizione:
        client.loop_stop()
        client.disconnect()
        print("")
        print(f"NESSUNA CONFERMA di sottoscrizione a {prefix}/#")
        print("")
        print("AWS ha ignorato la SUBSCRIBE: manca iot:Subscribe (o iot:Receive)")
        print(f"sul filtro {prefix}/# per il client {client._client_id.decode()},")
        print("oppure il filtro richiesto e' piu' ampio di quello autorizzato.")
        print("Una wildcard '*' copre un solo livello, '#' copre tutti quelli sotto.")
        print("Vedi SETUP_AWS.md, sezione 4.")
        return 1
    if not sottoscrizione["ok"]:
        client.loop_stop()
        client.disconnect()
        print("")
        print(f"Sottoscizione rifiutata: {sottoscrizione['motivi']}")
        print("Vedi SETUP_AWS.md, sezione 4.")
        return 1

    print(
        f"In ascolto su {prefix}/# per {args.durata}s "
        f"(sottoscrizione confermata: {sottoscrizione['motivi'][0]})\n"
    )
    try:
        time.sleep(args.durata)
    except KeyboardInterrupt:
        print("\ninterrotto")
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"\n{len(ricevuti)} messaggi")
    temi = sorted({t for t, _, _ in ricevuti})
    if not temi:
        print("")
        print("Nessun messaggio, ma la sottoscrizione era autorizzata.")
        print("Due cause da distinguere:")
        print("  - non c'e' niente da ascoltare: il Pi e' spento e nessun test")
        print("    ha mai pubblicato. Il broker non ha retained da consegnare.")
        print("  - oppure i messaggi retained vengono memorizzati ma non")
        print("    riconsegnati al subscribe. Lo si verifica dalla CLI:")
        print("      aws iot-data list-retained-messages --region eu-central-1")
        print("    Se la riga c'e' ma qui non arriva nulla, non e' un errore")
        print("    della policy: e' un comportamento del broker.")
        print("")
        print("Per vedere il traffico in tempo reale, pubblica qualcosa:")
        print("  python verifica_iot.py pubblica")
        return 0
    for t in temi:
        quanti = sum(1 for x in ricevuti if x[0] == t)
        print(f"  {t}  x{quanti}")
    return 0


def cmd_pubblica(args):
    """Invia un payload di test: serve a validare la Rule prima dei dati reali."""
    problemi = _ambiente()
    if problemi:
        for p in problemi:
            log.error(p)
        return 1

    aws = TelemetriaAWS()
    if not aws.enabled:
        log.error("Client AWS IoT non inizializzato. Vedi il messaggio sopra.")
        return 1

    if args.verbose:
        aws.abilita_log()

    aws.connetti()
    limite = time.time() + TIMEOUT_CONNESSIONE
    while time.time() < limite and not aws.connesso():
        time.sleep(0.3)
    if not aws.connesso():
        log.error("Non connesso: nada pubblicato.")
        return 1

    for i in range(args.ripetizioni):
        payload = dict(
            PAYLOAD_DI_TESTO,
            ts=int(time.time()),
            host="raspberry_pi_4",
            _test=f"verifica_iot.py #{i + 1}",
        )
        if not aws.pubblica_telemetria(payload):
            log.error("Publish fallito.")
            return 1
        aws.pubblica_ultima_lettura(payload)
        print(f"pubblicato #{i + 1} su {aws.topic_telemetria}")
        if i < args.ripetizioni - 1:
            time.sleep(args.intervallo)

    aws.chiudi()
    print("")
    print("Ora controlla CloudWatch, namespace Casa/Telemetria.")
    print("Le metriche possono comparire anche 15 minuti dopo: e' normale.")
    print("Se invece arriva un 'testo non valido' o la metrica resta a secco,")
    print("la Rule non sta scrivendo: vedi SETUP_AWS.md, sezione 11.")
    return 0


def cmd_leggi_ora(args):
    """Manda il comando on-demand e verifica che arrivi."""
    problemi = _ambiente()
    if problemi:
        for p in problemi:
            log.error(p)
        return 1

    prefix = os.getenv("AWS_IOT_PREFIX", "casa/raspberry_pi_4").strip("/")
    risposte = []

    def su_messaggio(c, userdata, msg):
        if msg.topic.endswith("/telemetria"):
            risposte.append(msg.payload.decode("utf-8", errors="replace"))

    ascoltatore = _client_mqtt(
        _client_id_verifica(), prefix, su_messaggio, getattr(args, "verbose", False)
    )
    if not _connetti(ascoltatore, os.getenv("AWS_IOT_ENDPOINT")):
        ascoltatore.loop_stop()
        print("")
        print("FALLITO: il client di verifica non si e' connesso.")
        print(f"Client id usato: {ascoltatore._client_id.decode()}")
        print("La policy deve avere iot:Connect su client/<quel nome>.")
        return 1
    time.sleep(1.0)  # la sottoscrizione va confermata dal broker

    comando = json.dumps({"cmd": "leggi_ora"})
    topic_comandi = f"{prefix}/comandi"
    ascoltatore.publish(topic_comandi, comando.encode("utf-8"), qos=0)
    print(f"inviato {comando} su {topic_comandi}")
    print(f"il Pi risponde entro {args.timeout}s (debounce {DEBOUNCE_LETTURA}s)")

    limite = time.time() + args.timeout
    try:
        while not risposte and time.time() < limite:
            time.sleep(0.2)
    finally:
        ascoltatore.loop_stop()
        ascoltatore.disconnect()

    if not risposte:
        print("")
        print("NESSUNA RISPOSTA. Cause probabili, in ordine:")
        print("  1. monitor_ambiente.py non e' in esecuzione")
        print("  2. la policy non contiene iot:Subscribe + iot:Receive sui comandi")
        print(
            "  3. hai gia' mandato un comando meno di %ds fa (debounce)"
            % DEBOUNCE_LETTURA
        )
        return 1

    print("")
    print("OK: il Pi ha risposto.")
    print(f"    {risposte[-1][:200]}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Verifiche MQTT per il setup AWS IoT Core."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log di debug, incluso l'handshake TLS"
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("connessione", help="verifica che il CONNECT venga accettato")
    p.add_argument("--timeout", type=int, default=TIMEOUT_CONNESSIONE)
    p.set_defaults(func=cmd_connessione)

    p = sub.add_parser("ascolta", help="osserva i topic per un po'")
    p.add_argument("--durata", type=int, default=60)
    p.set_defaults(func=cmd_ascolta)

    p = sub.add_parser("pubblica", help="invia un payload di test sulla telemetria")
    p.add_argument("--ripetizioni", type=int, default=3)
    p.add_argument("--intervallo", type=float, default=20.0)
    p.set_defaults(func=cmd_pubblica)

    p = sub.add_parser(
        "leggi_ora", help="manda il comando on-demand e attende la risposta"
    )
    p.add_argument("--timeout", type=int, default=30)
    p.set_defaults(func=cmd_leggi_ora)

    args = parser.parse_args()
    # Log su stdout e non stderr: altrimenti l'output dei due flussi si
    # mescola e i messaggi appaiono fuori ordine rispetto ai print().
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
