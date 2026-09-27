# grafana-raspi4-dht


```bash
mkdir -p influxdb_data influxdb_config grafana_data
```

## Pip
Le dipendenze sono pinnate in `requirements.txt` (paho-mqtt 2.x ha cambiato l'API dei callback rispetto alla 1.x: un upgrade a major silenzioso rompe il client AWS IoT).

```bash
pip install --upgrade pip
pip install -r requirements.txt
```
> Nota: adafruit-circuitpython-dht su Raspberry Pi richiede anche libgpiod2 a livello di sistema:

```bash
sudo apt-get update
sudo apt-get install -y libgpiod2
```


## .env
INFLUX_PASSWORD= xxx
INFLUX_TOKEN= xxx
INFLUX_ORG= xxx
INFLUX_BUCKET= xxx
INFLUX_URL= xxx
GRAFANA_PASSWORD= xxx

## per jupyter

```bash
pip install influxdb-client pandas matplotlib
```

## AWS IoT Core (opzionale)


> Verifiche: `python verifica_iot.py --help`.

Invio della stessa telemetria ad AWS IoT Core, con conservazione in CloudWatch e
dashboard in Grafana. Il monitoraggio locale su InfluxDB resta il primario: se
AWS è assente, irraggiungibile o disabilitato, **non cambia nulla**.

Con `AWS_IOT_ENABLED=false` (impostazione di default) il ramo cloud è spento.

### Come funziona

`monitor_ambiente.py` scrive su InfluxDB come prima e, in più, ogni 60s pubblica
un payload JSON su MQTT. Il ramo cloud non rilegge il DHT11: riusa gli ultimi
valori letti, così i sensori non vengono interrogati due volte.

Una IoT Rule instrada il payload a 6 metriche CloudWatch nel namespace
`Casa/Telemetria`, senza dimensioni, e in più lo copia in un log group.

| Locale | Frequenza | Destinazione |
|---|---|---|
| Ambiente (DHT11) | 30s | InfluxDB |
| Sistema (CPU/RAM/temp) | 10s | InfluxDB |
| Cloud | 60s | IoT Core → CloudWatch |

60s e non meno: CloudWatch aggrega a risoluzione standard di 60s, quindi scendere
sotto non aggiunge risoluzione, e in high-res la retention è di sole 3 ore.

Ogni payload che la rule vede viene anche copiato in un log group con retention
a 14 giorni, che risponde subito mentre le metriche ci mettono fino a 15 minuti:

```bash
aws logs tail /aws/iot/rules/TelemetriaPiCloudWatch/payload \
  --since 2m --region eu-central-1
```

### Topic

| Topic | QoS | Retain | Contenuto |
|---|---|---|---|
| `casa/raspberry_pi_4/telemetria` | 0 | no | payload con tutte e 6 le misure |
| `casa/raspberry_pi_4/stato/online` | 0 | sì | `online` / `offline` (Last Will) |
| `casa/raspberry_pi_4/stato/ultima_lettura` | 0 | sì | ultimo payload valido |
| `casa/raspberry_pi_4/comandi` | 0 | no | comandi on-demand |

I messaggi retained servono perché il Pi è acceso solo poche ore al giorno: chi si
connette quando è acceso vede subito l'ultima lettura e lo stato, senza aspettare
il publish successivo. Il Last Will annuncia la disconnessione anche per
spegnimento, rete caduta o blackout.

### Setup

1. In AWS IoT Core (regione `eu-central-1`) crea una **thing** tipo `raspberry-pi-4-telemetria`
   con un **certificato** X.509. Scarica i tre file e mettili in `certs/`:
   `certificate.pem.crt`, `private.pem.key`, `AmazonRootCA1.pem`. La cartella è
   già in `.gitignore`: non vanno mai nel repo.
2. Crea una policy e allegala al certificato:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid":"ConnessioneDevice", "Effect":"Allow", "Action":"iot:Connect", "Resource":"arn:aws:iot:eu-central-1:<ACC>:client/raspberry-pi-4-telemetria" },
    { "Sid":"Pubblicazione",     "Effect":"Allow", "Action":"iot:Publish", "Resource":"arn:aws:iot:eu-central-1:<ACC>:topic/casa/raspberry_pi_4/*" },
    { "Sid":"Sottoscrizione",    "Effect":"Allow", "Action":"iot:Subscribe", "Resource":"arn:aws:iot:eu-central-1:<ACC>:topicfilter/casa/raspberry_pi_4/comandi" },
    { "Sid":"Ricezione",         "Effect":"Allow", "Action":"iot:Receive",   "Resource":"arn:aws:iot:eu-central-1:<ACC>:topic/casa/raspberry_pi_4/*" },
    { "Sid":"Retained",          "Effect":"Allow", "Action":"iot:RetainPublish", "Resource":"arn:aws:iot:eu-central-1:<ACC>:topic/casa/raspberry_pi_4/*" },
    { "Sid":"VerificaConnect",   "Effect":"Allow", "Action":"iot:Connect",   "Resource":"arn:aws:iot:eu-central-1:<ACC>:client/verifica-raspberry-pi-4-telemetria" },
    { "Sid":"VerificaSubscribe", "Effect":"Allow", "Action":"iot:Subscribe", "Resource":"arn:aws:iot:eu-central-1:<ACC>:topicfilter/casa/raspberry_pi_4/#" },
    { "Sid":"VerificaReceive",   "Effect":"Allow", "Action":"iot:Receive",   "Resource":"arn:aws:iot:eu-central-1:<ACC>:topic/casa/raspberry_pi_4/#" }
  ]
}
```

   Tre dettagli che non sono ovvi, e che ognuno costa un pomeriggio se li
   scopri da solo:

   - **`iot:RetainPublish` è indispensabile.** Il Last Will è un messaggio
     retained: senza quel permesso il CONNECT viene rifiutato e il sintomo è un
     `Unspecified error` che non nomina la causa, mentre la telemetria continua a
     pubblicare apparentemente bene.
   - **`iot:Receive` va su `topic/`, `iot:Subscribe` su `topicfilter/`.** Invertire
     i prefissi non dà errori: la sottoscrizione viene concessa e i messaggi non
     arrivano mai.
   - **Le righe `Verifica*` servono ai test dal PC.** Condividono il certificato
     e cambiano solo il `clientId`, perché AWS ne ammette uno solo attivo per
     volta: senza, un test dal PC butterebbe fuori il device.

3. Crea una **IoT Rule** che trasformi il payload in metriche CloudWatch.
   Query SQL (il payload non è wrappers, quindi non serve `unquote`):

```sql
SELECT * FROM 'casa/raspberry_pi_4/telemetria'
```

   Con `SELECT *` la regola produce **un solo messaggio**, non uno per campo: le
   sei metriche si ottengono con **sei azioni** CloudWatch, una per metrica.
   Ogni azione referenzia i campi del payload con `${...}` e nomina la metrica
   come serve in CloudWatch:

| Metric name | Metric value | Metric unit |
|---|---|---|
| `Temperatura` | `${temperatura}` | `None` |
| `Umidita` | `${umidita}` | `Percent` |
| `Percepita` | `${percepita}` | `None` |
| `CaricoCPU` | `${cpu_load}` | `Percent` |
| `CaricoRAM` | `${ram_load}` | `Percent` |
| `TempCPU` | `${cpu_temp}` | `None` |

   I campi sono quelli del payload, in italiano: una `Metric value` deve poterli
   copiare alla lettera, e `${temperatura}` non diventa `${temperature}` perché
   così si chiama in italiano.

   Le tre temperature vanno con **`None`**, non con `Celsius`. Verificato su
   account reale: un'azione con `metricUnit` a `Celsius` non scrive nulla e non
   segnala alcun errore, mentre le azioni con `Percent` nello stesso messaggio
   funzionano. L'unità la dichiari nella query di Grafana.

   Per ogni azione: namespace `Casa/Telemetria`, `Metric value` il campo
   corrispondente, e il **IAM role**
   con `cloudwatch:PutMetricData` che AWS IoT assume per conto del device (vedi
   `SETUP_AWS.md` sezione 5). Senza quel ruolo la rule non produce nulla e non
   segnala alcun errore.

4. Nel `.env` metti `AWS_IOT_ENABLED=true` e l'endpoint
   `<account>-ats.iot.eu-central-1.amazonaws.com` (lo trovi con
   `aws iot describe-endpoint --endpoint-type iot:Data-ATS`).
5. Proteggi le credenziali e riavvia:

```bash
chmod 600 certs/private.pem.key
sudo systemctl restart monitor-ambiente
python verifica_iot.py --verbose connessione
```

   Le righe da cercare sono `Sending CONNECT` e subito dopo
   `Received CONNACK (0, 0)`. Se il primo c'è e il secondo no, il pacchetto è
   partito e il rifiuto è di autorizzazione: il problema è la policy.

La procedura completa, con la spiegazione di ogni permesso e cosa controllare
quando qualcosa non torna, è in [`SETUP_AWS.md`](SETUP_AWS.md). Per provare i
topic uno a uno e guardare i messaggi dalla console IoT c'è
[`GUIDA_TEST_IOT.md`](GUIDA_TEST_IOT.md).

### Comando on-demand

Chiedi una misura immediata, senza attendere il prossimo ciclo:

```bash
python verifica_iot.py leggi_ora
```

Il Pi risponde con una telemetria immediata. I comandi sono protetti da un
debounce di 10s, per non martellare il DHT11.

Limite da tenere presente: con `clean_session=True` un comando inviato mentre il
Pi è **spento** viene perso, non accodato. Serve che il Pi sia acceso.

### Costi

Alle frequenze sopra, con il Pi acceso **6 ore a settimana** (≈26 h/mese):

| Voce | Calcolo | €/mese |
|---|---|---|
| IoT ConnectionMinutes | 1.560 min | ~0,0001 |
| IoT Messages | 3.120 msg (2 topic × 60s) | ~0,003 |
| CloudWatch 6 metriche | 6 × 26h/720h × $0,30 | ~0,065 |
| CloudWatch PutMetricData | sotto il free tier (1M/mese) | 0 |
| **Totale** | | **~0,07** |

CloudWatch addebita le metriche solo nelle ore in cui riceve dati, quindi le ore
in cui il Pi è spento non costano nulla.

`GetMetricData` invece è sempre a pagamento e dipende dalle query di Grafana,
non dal Pi: costa circa 0,52 €/mese con refresh ogni 5 min, 0,17 ogni 15 min,
0,04 ogni ora. Rete di sicurezza consigliata: billing alarm a 1 € e budget a 5 €.

La retention delle metriche standard è di 15 giorni: CloudWatch **non** è
l'archivio storico, quello resta InfluxDB. I grafici avranno buchi nei giorni in
cui il Pi era spento.

### Troubleshooting

- **`Ramo AWS IoT disabilitato`** → normale con `AWS_IOT_ENABLED=false`.
- **`Client AWS IoT non inizializzato`** → endpoint o file di credenziali mancanti;
  il messaggio dice cosa non è stato trovato. Il ramo cloud resta spento e il
  monitoraggio locale prosegue.
- **`Connesso a AWS IoT` assente nei log** → verifica `AWS_IOT_ENDPOINT` e che i
  permessi IP del tuo ISP non blocchino la porta 8883 in uscita. Il client
  riprova da solo con un backoff crescente.
- **Niente dati in CloudWatch** → controlla che la IoT Rule sia abilitata e che
  l'action abbia un IAM role con `cloudwatch:PutMetricData`: senza il ruolo la
  rule non scrive nulla e non segnala errori. Verifica il topic nella console IoT.
- **`Unspecified error` in `on_disconnect`** → quasi sempre manca
  `iot:RetainPublish` nella policy: il Last Will è un messaggio retained e senza
  quel permesso il CONNECT viene rifiutato.
- **Una SUBSCRIBE concessa ma nessun messaggio** → manca `iot:Receive`, o è
  dichiarata su `topicfilter/` invece che su `topic/`. Il SUBACK arriva lo stesso.
- **Metriche che compaiono a ondate** → è l'indicizzazione di CloudWatch, non un
  errore: verifica i valori e attendi qualche minuto prima di concludere che
  qualcosa non funzioni.

La tabella completa dei sintomi è in [`SETUP_AWS.md`](SETUP_AWS.md) sezione 11.
- **Le metriche non compaiono** → CloudWatch le indicizza con fino a 15 minuti di
  ritardo. Solo dopo, se restano vuote, il problema è nella rule.

