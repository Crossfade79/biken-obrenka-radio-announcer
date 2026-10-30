# BIKEN V4.0 + Obrenka Radio Announcer

BIKEN V4.0 + Obrenka Radio Announcer je kompletan Raspberry Pi sistem za radio automatiku, govorne najave, rasporede, ručni PTT i lokalni audio DSP.

Sistem je razvijen za samostalan rad na Raspberry Pi uređaju i ne zahteva plaćeni cloud TTS servis tokom normalnog rada.

## Glavne mogućnosti

- Web interfejs na portu `8891`
- Obrenka Serbian Voice Pack
- Message Builder
- Preseti
- Automatske radio najave
- Najava tačnog vremena
- Podrška za vremenske podatke i temperaturu
- Manual PTT ON/OFF
- Audio Plugin Rack
  - EQ
  - Compressor
  - Limiter
- Lokalna reprodukcija WAV semplova
- GPS/PPS podrška
- Rad bez GPS-a preko sistemskog NTP vremena
- BIKEN V3.7 locked TDoA core

---

## Preporučeni sistem

Referentna platforma na kojoj je sistem razvijan i testiran:

- Raspberry Pi 3A+
- Raspberry Pi OS 12 Bookworm
- Python 3.11
- ALSA audio
- Motorola CM140 kao radio interfejs

Moguće je koristiti i druge Raspberry Pi modele.

---

# Instalacija

Na čistom Raspberry Pi OS Bookworm sistemu pokrenite:

```bash
git clone https://github.com/Crossfade79/biken-obrenka-radio-announcer.git
cd biken-obrenka-radio-announcer
sudo bash install.sh
```

Installer će instalirati potrebne pakete i postaviti aplikaciju u:

```text
/opt/biken-v4
```

Nakon instalacije pokrenite proveru:

```bash
sudo bash verify.sh
```

Ako je instalacija uspešna, na kraju bi trebalo da dobijete:

```text
BIKEN VERIFY: PASS
```

---

# Web interfejs

Web panel je dostupan na:

```text
http://IP_ADRESA_RASPBERRY_PI:8891
```

Primer:

```text
http://192.168.1.100:8891
```

---

# Bezbedan prvi start

Nova instalacija namerno počinje u bezbednom režimu:

```text
RF Master      OFF
Automation     OFF
PTT GPIO       DRY RUN
DSP Master     OFF
Output Gain    0 dB
Time Source    AUTO
```

To znači da instalacija ne bi trebalo sama da aktivira radio predajnik.

Pre uključivanja pravog PTT izlaza obavezno proverite povezivanje sa radio-stanicom.

---

# PTT

Podrazumevani PTT izlaz je:

```text
BCM GPIO17
Physical pin 11
```

Preporučeno je koristiti optokapler ili drugi odgovarajući izolacioni interfejs.

Ne povezujte Raspberry Pi GPIO direktno na nepoznat PTT ulaz radio-stanice.

Referentno povezivanje za Motorola CM140:

```text
Pin 3 - PTT
Pin 7 - GND
Pin 2 - External Mic Audio
```

---

# Audio

BIKEN koristi ALSA audio sistem.

Tipični alati:

```text
aplay
amixer
```

Audio se šalje prema radio-stanici preko odgovarajućeg audio izlaza Raspberry Pi uređaja.

---

# Voice Pack

U projektu je uključen lokalni srpski Voice Pack:

```text
voice-pack/sr_female_01/
```

Sistem tokom normalnog rada koristi unapred pripremljene WAV semplove.

Nije potreban online niti plaćeni TTS servis.

---

# GPS i PPS

GPS nije obavezan za Radio Announcer.

Default režim je:

```text
AUTO
```

U AUTO režimu sistem prvo pokušava:

```text
GPS_PPS
```

Ako GPS/PPS nije dostupan, automatski koristi:

```text
SYSTEM_NTP
```

odnosno sinhronizovani sistemski sat Raspberry Pi-ja.

Za obične automatske govorne najave dovoljno je da Raspberry Pi ima ispravno sinhronizovano vreme preko interneta.

---

# Opcioni GPS/PPS hardver

Referentna konfiguracija:

```text
GPS UART : /dev/serial0
PPS      : /dev/pps0
PPS GPIO : BCM18
```

BIKEN TDoA režim zadržava svoje precizne GPS/PPS zahteve.

---

# Systemd servis

Servis se zove:

```text
biken-v4.service
```

Status:

```bash
sudo systemctl status biken-v4.service
```

Restart:

```bash
sudo systemctl restart biken-v4.service
```

Live log:

```bash
sudo journalctl -u biken-v4.service -f
```

---

# Provera sistema

Pokrenite:

```bash
sudo bash verify.sh
```

Verifier proverava:

- BIKEN servis
- Python kod
- konfiguraciju
- Web API
- Voice Pack
- ALSA audio
- chrony/system clock
- GPS/PPS ako postoji

GPS/PPS nije obavezan za Radio Announcer.

---

# Struktura repozitorijuma

```text
app/
    biken_v4.py
    biken_v37_locked.py
    marker.wav

config/
    defaults/

plugins/
    audio_eq/
    compressor/
    plugin_host.py
    audio_presets.json

systemd/
    biken-v4.service

voice-pack/
    sr_female_01/

install.sh
verify.sh
VERSION
README.md
```

---

# Verzija

Aktuelna distributivna verzija nalazi se u fajlu:

```text
VERSION
```

Trenutni release candidate:

```text
1.0.0-rc2
```

---

# Bezbednost

Web interfejs je prvenstveno namenjen radu u lokalnoj i pouzdanoj mreži.

Ne preporučuje se direktno izlaganje porta:

```text
8891
```

javnom Internetu bez dodatne autentikacije, firewall-a ili sigurnog reverse proxy sistema.

---

# Radio propisi

Korisnik je odgovoran za:

- dozvoljene frekvencije
- dozvoljenu snagu predajnika
- radio dozvole
- duty-cycle ograničenja
- lokalne zakone i propise

---

# Autor

Crossfade RF Labs  
YU4DUI

GitHub:

```text
https://github.com/Crossfade79
```
