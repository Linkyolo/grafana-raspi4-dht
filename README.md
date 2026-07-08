# grafana-raspi4-dht


```bash
mkdir -p influxdb_data influxdb_config grafana_data
```

## Pip
```bash
pip install --upgrade pip
pip install adafruit-circuitpython-dht influxdb-client psutil python-dotenv
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
