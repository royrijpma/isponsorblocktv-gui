# iSponsorBlockTV GUI

Sidecar web-GUI naast de ongewijzigde `ghcr.io/dmunozv04/isponsorblocktv` image.

```
isponsorblocktv-gui/
├── docker-compose.yml
├── README.md
└── gui/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py
    └── templates/index.html
```

## Starten op de QNAP

De image wordt door GitHub Actions gebouwd (amd64 + arm64) en staat op `ghcr.io/royrijpma/isponsorblocktv-gui:latest`. Je hoeft op de NAS dus niets te bouwen.

1. Zorg dat `/Volume1/docker/isponsorblocktv/data/config.json` bestaat (maak die eerst met de officiële setup als je dat nog niet deed).
2. Plak `docker-compose.yml` in Portainer (Stacks → Add stack → Web editor) en klik op Deploy.
3. Open `http://<nas-ip>:8095`.

Eenmalig: zet het package op publiek via GitHub → profiel → Packages → isponsorblocktv-gui → Package settings → Change visibility → Public. Daarna kan elke machine de image zonder login pullen.

## Goed om te weten

- De GUI heeft `/var/run/docker.sock` en is dus feitelijk root op de NAS. Houd poort 8095 binnen je LAN en zet eventueel `GUI_PASSWORD` in de compose (gebruiker `admin`).
- Statistieken staan in `data/stats.json`; ze tellen alleen vanaf het moment dat de GUI draait.
- Bij "Opslaan" worden alleen `mute_ads`, `skip_ads` en `skip_categories` aangepast; een back-up staat in `config.json.bak`.
- Update: de GUI kopieert env, volumes, netwerk, restart-policy en labels van de bestaande container, en zet bij een mislukte start de oude image terug.
- De tv-status is gebaseerd op logregels met de apparaatnaam; de exacte logtekst van iSponsorBlockTV kan per versie verschillen, dus pas de regexen in `device_status()` aan als video-ID's niet verschijnen.
