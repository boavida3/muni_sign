# muni_sign

A home arrival board for San Francisco Muni: a Raspberry Pi and a 64×32 LED panel showing
the next three trains at your stop, from the [511.org](https://511.org/open-data/transit)
real-time API. The text rotates between Korean, Portuguese and English.

<img src="docs/sign.jpg" width="480" alt="The sign showing the next J Church trains to downtown (Centro): 13, 32 and 52 minutes">

## Hardware

- Raspberry Pi 5
- 64×32 HUB75 LED panel (P5 outdoor, 1/8 scan)
- [Adafruit RGB Matrix Bonnet](https://www.adafruit.com/product/3211)
- 5V 4A power supply for the panel, plugged into the Bonnet's barrel jack (the Pi can't
  power the panel)

## Setup

```bash
git clone https://github.com/boavida3/muni_sign.git
cd muni_sign
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
python3 -m venv venv
venv/bin/pip install ./rpi-rgb-led-matrix

cp .env.example .env    # add your 511 API key and stop number
```

Get a free API key at [511.org/open-data/token](https://511.org/open-data/token). The stop
number is on the Muni stop sign. The LED library also needs `dtparam=audio=off` in
`/boot/firmware/config.txt`.

## Run

```bash
venv/bin/python muni_board.py --print   # test without the panel
sudo venv/bin/python muni_board.py      # on the panel
```

To start it at boot:

```bash
sed "s|/home/pi/muni_sign|$PWD|g" muni-board.service | sudo tee /etc/systemd/system/muni-board.service
sudo systemctl enable --now muni-board
```

Settings (line, colours, brightness, languages, arrow direction) are at the top of
`muni_board.py`.

## Licence

GPL-3.0. Uses [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix)
(GPL-2.0-or-later) and the [Galmuri](https://github.com/quiple/galmuri) fonts (SIL OFL 1.1).
