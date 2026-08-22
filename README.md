# padel-auto-booking

Automatisiertes Buchungsskript für Padel-Plätze im Sportpark Weywiesen
(Bottrop), Buchungssystem [bsbb.ebusy.de](https://bsbb.ebusy.de). Prüft
in einem konfigurierbaren Zeitfenster, ob dein Ziel-Slot (Wochentag +
Uhrzeit, jeweils 7 Tage im Voraus) buchbar geworden ist, und bucht ihn
automatisch, sobald er freigeschaltet wird.

Läuft dauerhaft als `systemd`-Dienst auf einer **Oracle Cloud Free Tier
VM** (kostenlos). GitHub Actions wird nur für Deployment/Sync des Codes
auf die VM genutzt — **nicht** für die eigentliche zeitkritische
Buchung, da Cron-Timing in GitHub Actions zu ungenau ist.

**Kosten:** 0 €. Oracle Cloud Always-Free-VM, kostenlose GitHub-Actions-Minuten
(Deploy-Job dauert Sekunden), keine externen APIs/Dienste nötig. E-Mail-
Benachrichtigung ist optional und nutzt einen bestehenden, kostenlosen
SMTP-Account (z.B. GMX/Gmail).

---

## Wie es technisch funktioniert

`bsbb.ebusy.de` läuft auf **Spring Security + Spring Web Flow**,
serverseitig gerendertes HTML, jQuery im Frontend (kein React/Vue/SPA).
Das Skript bildet den Ablauf per direkten HTTP-Requests nach (kein
Browser/Playwright nötig — schneller und ressourcenschonender, wichtig
beim Wettlauf um einen frisch freigeschalteten Slot):

1. **Login** (`POST /login`) mit CSRF-Token aus den Meta-Tags der Seite.
2. **Slot-Suche** (`GET /padel?selectedDate=...`) — die Kalenderzelle für
   den Zieltermin trägt alle nötigen Daten direkt als `data-*`-Attribute.
3. **Buchungs-Flow** (`GET`/`POST /court-single-booking-flow`) — ein
   mehrstufiger Spring-Web-Flow-Dialog (`execution=e<N>s<N>`-Zustände).
   Das Skript geht die Schritte automatisch durch und erkennt Erfolg/
   Fehlschlag an `execution`-Statuswechseln bzw. `.alert-danger`-Meldungen
   im HTML (z.B. "7-Tage-Regel" oder "Terminkonflikt").

Ein Detail ist **nicht** hundertprozentig verifiziert: der exakte
`_eventId`-Name des allerletzten "Jetzt buchen"-Klicks im Bestätigungs-
Dialog. Das Skript probiert dafür automatisch `next` zuerst (bestätigtes
Muster für den vorherigen Schritt), dann eine Handvoll plausibler
Alternativnamen. **Deshalb: führe einmal den Test-Modus unten aus, bevor
du das Skript unbeaufsichtigt laufen lässt.**

---

## 1. Lokale Einrichtung

```bash
git clone <dieses-repo>
cd padel-auto-booking
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env öffnen und EBUSY_USERNAME / EBUSY_PASSWORD eintragen
```

`.env` wird durch `.gitignore` von Git ausgeschlossen — landet nie im
Repo. Zugangsdaten werden ausschließlich über Umgebungsvariablen
gelesen (`src/padel_booker/config.py`), nie im Code.

### Login testen

```bash
python main.py test-login
```

### Prüfen, ob der aktuell konfigurierte Ziel-Slot gerade buchbar ist

```bash
python main.py find-slot
```

### Buchungs-Flow einmal wirklich verifizieren (wichtig!)

Da der letzte Schritt des Buchungsdialogs nicht zu 100% verifiziert ist,
solltest du das **einmal bewusst** mit einem echten freien Slot testen —
idealerweich sowieso einem Termin, den du wirklich spielen willst
(verbraucht deine eine erlaubte Vorausbuchung):

```bash
# Zeigt nur den gefundenen Slot, bucht NICHT:
python main.py test-booking

# Bucht wirklich (nach Sicherheits-Nachfrage "ja"):
python main.py test-booking --confirm
```

Falls der Flow dabei feststeckt, gibt das Skript eine klare Fehlermeldung
inkl. HTML-Kontext aus (`FlowStuckError`) — dann bitte den Buchungs-
Dialog im Browser mit offenen DevTools nachvollziehen und in
`src/padel_booker/client.py` (`_STATIC_FALLBACK_EVENTS`) den korrekten
`_eventId`-Namen ergänzen.

---

## 2. Ziel-Slot wöchentlich anpassen

Alles Wesentliche steht in `config.yaml`:

```yaml
target:
  weekday: saturday       # gewünschter Wochentag
  time: "10:00"           # gewünschte Startzeit
  duration_minutes: 90    # max. 90 laut Platzregeln
  courts: [2, 1]          # Prioritäts-Reihenfolge der Courts

search_window:
  start_time: "07:55"     # Beginn des aktiven Poll-Fensters
  end_time: "09:30"       # Ende des aktiven Poll-Fensters
```

Das Poll-Fenster läuft **am selben Wochentag wie `target.weekday`**,
weil "7 Tage im Voraus buchbar" bedeutet: der neu freigeschaltete Tag
hat immer denselben Wochentag wie heute. Willst du z.B. immer samstags
10:00 Uhr spielen, pollt das Skript jeden Samstag im konfigurierten
Fenster.

Setze das Fenster großzügig, da der exakte Freischaltzeitpunkt laut
Anlage unregelmäßig ist — außerhalb des Fensters prüft das Skript nur
träge alle `polling.idle_check_seconds`, ob das Fenster begonnen hat;
innerhalb pollt es alle `polling.interval_seconds` (Standard: 3s).

**Nach jeder Änderung (per SSH/Git):** committen, pushen — der GitHub-Actions-
Workflow deployed automatisch auf die VM (siehe unten). Alternativ lokal auf
der VM `git pull`. Ein Neustart des Dienstes ist **nicht** nötig — der
Scheduler liest `config.yaml` bei jedem Durchlauf neu ein.

### Alternative: Web-Oberfläche statt SSH

Für die wöchentliche Anpassung ohne Terminal gibt es eine schlanke,
passwortgeschützte Weboberfläche (`main.py serve-ui`), erreichbar unter
`http://<Public-IP-der-VM>:8080`. Sie bearbeitet dieselbe `config.yaml`
direkt auf der VM (inkl. Erhalt der Kommentare) — der laufende Buchungs-
Dienst übernimmt Änderungen automatisch, ganz ohne Git/SSH/Neustart.

Voraussetzungen einmalig einrichten:

1. In `.env` (auf der VM) `UI_USERNAME` und `UI_PASSWORD` setzen — ein
   eigenes, starkes Passwort, **nicht** dein eBuSy-Passwort wiederverwenden.
2. Port **8080** in der OCI Security List freigeben (analog zu Port 22 in
   Schritt 4 der VM-Einrichtung unten: Ingress-Regel, Source `0.0.0.0/0`,
   TCP, Port 8080).
3. `sudo systemctl start padel-booking-ui` (der Dienst wird von
   `install.sh` bereits mit angelegt, siehe Abschnitt 3).

Dann im Browser `http://<Public-IP>:8080` öffnen, mit `UI_USERNAME`/
`UI_PASSWORD` einloggen, Wochentag/Uhrzeit/Suchfenster/Courts/Benach­richtigung
anpassen, Speichern klicken.

**Sicherheitshinweis:** Die Oberfläche läuft über einfaches HTTP (kein
eigenes TLS-Zertifikat, da keine eigene Domain) und ist nur per Basic-Auth
abgesichert — für den persönlichen Gebrauch ausreichend, aber die Zugangs­daten
gehen unverschlüsselt über die Leitung. Sie zeigt **nirgends** deine
eBuSy-Zugangsdaten an, nur die operative Konfiguration (Zielzeit, Suchfenster
etc.). Falls dir das zu unsicher ist: Port 8080 einfach nicht freigeben und
bei SSH/`nano config.yaml` bleiben.

---

## 3. Deployment auf die Oracle Cloud Free Tier VM

### Einmaliges Setup auf der VM

Voraussetzung: eine laufende **Always-Free**-VM (z.B. Ampere A1,
Ubuntu/Oracle Linux), SSH-Zugriff.

```bash
# Auf der VM:
sudo apt-get update && sudo apt-get install -y python3 python3-venv git   # Ubuntu
git clone <dieses-repo> ~/padel-auto-booking
cd ~/padel-auto-booking
./deploy/install.sh
```

`install.sh` erstellt ein venv, installiert die Abhängigkeiten, legt bei
Bedarf `.env` aus der Vorlage an und installiert einen `systemd`-Dienst
(`deploy/padel-booking.service`).

```bash
# .env mit echten Zugangsdaten befüllen, dann:
nano .env
python main.py test-login          # Login verifizieren
sudo systemctl start padel-booking
journalctl -u padel-booking -f     # Logs live verfolgen
```

Der Dienst startet automatisch bei VM-Reboot (`systemctl enable`, macht
`install.sh` bereits) und läuft dauerhaft im Hintergrund — kein
manuelles "am Laptop sitzen" nötig.

### Automatisches Deployment via GitHub Actions

`.github/workflows/deploy.yml` deployed bei jedem Push auf `main` per
SSH auf die VM (führt dort `deploy/redeploy.sh` aus: `git pull`,
Dependencies aktualisieren, Dienst neu starten). GitHub Actions wird
**nur** hierfür genutzt — die eigentliche Buchung läuft ausschließlich
im `systemd`-Dienst auf der VM, wo das Timing präzise ist.

In den Repo-Settings unter **Settings → Secrets and variables →
Actions** anlegen:

| Secret        | Wert                                              |
|---------------|----------------------------------------------------|
| `VM_HOST`     | Öffentliche IP/Hostname der Oracle-VM              |
| `VM_USER`     | SSH-Benutzer (z.B. `ubuntu`)                       |
| `VM_SSH_KEY`  | Privater SSH-Key mit Zugriff auf die VM (PEM-Format) |
| `VM_APP_DIR`  | Absoluter Pfad des Repo-Checkouts auf der VM (z.B. `/home/ubuntu/padel-auto-booking`) |

Der öffentliche Teil des SSH-Keys muss in `~/.ssh/authorized_keys` des
`VM_USER` auf der VM eingetragen sein. Erzeuge idealerweise ein
dediziertes Deploy-Keypair statt deines persönlichen SSH-Keys.

### Ohne GitHub Actions (rein manuell)

Funktioniert genauso gut, nur ohne Auto-Deploy:

```bash
# Auf der VM, nach jeder Konfigurationsänderung:
cd ~/padel-auto-booking
git pull
sudo systemctl restart padel-booking
```

---

## 4. Optional: E-Mail-Benachrichtigung

In `config.yaml`:

```yaml
notification:
  email_enabled: true
  email_to: "deine-adresse@example.com"
```

Und in `.env`:

```
SMTP_HOST=smtp.gmx.net
SMTP_PORT=587
SMTP_USER=dein-account@gmx.de
SMTP_PASSWORD=...
SMTP_FROM=dein-account@gmx.de
```

Funktioniert kostenlos mit jedem bestehenden Mail-Account, der SMTP
erlaubt (GMX, Gmail mit App-Passwort, etc.). Bei `email_enabled: false`
(Standard) wird nur ins Log geschrieben — ebenfalls kostenlos, kein
externer Dienst nötig.

---

## 5. Sicherheit & Fair Use

- Zugangsdaten ausschließlich über `.env` / Umgebungsvariablen, niemals
  im Code oder Git-Repo (`.gitignore` schließt `.env` explizit aus).
- Das Poll-Intervall ist bewusst moderat (Standard: 3s) und läuft nur
  innerhalb des konfigurierten Zeitfensters, nicht permanent — die
  Kalenderseite selbst pollt bereits periodisch (`/padel?timestamp=...`);
  ein Skript, das deutlich schneller/häufiger abfragt, kann als
  missbräuchlich auffallen und andere Nutzer:innen der Anlage
  beeinträchtigen.
- Das Skript stoppt automatisch nach einer erfolgreichen Buchung pro
  Woche (kein wiederholtes Buchen desselben Slots) und respektiert damit
  implizit das "max. 1 Vorausbuchung"-Limit der Anlage.

## Projektstruktur

```
main.py                        CLI-Einstieg (run / test-login / find-slot / test-booking / serve-ui)
config.yaml                    Wöchentlich anpassbare Zielkonfiguration
.env.example                   Vorlage für Zugangsdaten/Secrets (.env wird nicht committet)
src/padel_booker/
  client.py                    eBuSy-HTTP-Client: Login, Slot-Suche, Buchungs-Flow
  webflow.py                   Parsing-Helfer für den Spring-Web-Flow-Dialog
  scheduler.py                 Poll-Loop, Zeitfenster-/Wochentagslogik, liest config.yaml laufend neu
  notify.py                    Log- und optionale E-Mail-Benachrichtigung
  config.py                    Laden von config.yaml + .env (fürs Booking-Skript)
  config_io.py                 Kommentar-erhaltendes Lesen/Schreiben von config.yaml (fürs Web-UI)
  web.py                       Passwortgeschützte Web-Oberfläche zum Anpassen des Ziel-Slots
deploy/
  padel-booking.service        systemd-Unit-Vorlage (Booking-Dienst)
  padel-booking-ui.service     systemd-Unit-Vorlage (Web-Oberfläche, Port 8080)
  install.sh                   Einmaliges VM-Setup
  redeploy.sh                  Wird von GitHub Actions (oder manuell) für Updates genutzt
.github/workflows/deploy.yml   Auto-Deploy auf die VM bei Push auf main
```
