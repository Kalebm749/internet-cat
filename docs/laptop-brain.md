# Laptop Brain Server

Build spec for the laptop side of Internet Cat. The laptop is a **plain Ollama server** on the home network. It holds no game logic and no state. The Pi (see `pi-architecture.md`) sends it prompts and gets back JSON and embeddings.

Keeping the laptop "dumb" means:
- Swapping the brain (another GPU, a hosted API) never touches game code.
- The creature's life lives in one place (the Pi's database).

---

## 1. Hardware

- **HP Omen 15**, **GTX 1060 Mobile, 6GB VRAM** (confirm with `nvidia-smi`), **Ubuntu**.
- Runs 24/7, plugged in, on the same network as the Pi. **Use wired Ethernet if possible**: it's more reliable than Wi-Fi for an always-on server.
- The Omen has hybrid graphics (Intel + NVIDIA). Ollama uses the NVIDIA GPU through CUDA either way. `ollama ps` must show `100% GPU`.

---

## 2. What it serves

| Use | Endpoint (Ollama) | Model |
|---|---|---|
| Brain (actor) | `POST /api/chat` with `format` = JSON schema | chat model |
| Narrator | `POST /api/chat` with `format` = JSON schema | chat model (same one) |
| Sleep (consolidation, tendencies, preferences) | `POST /api/chat` | chat model |
| Memory embeddings | `POST /api/embed` | `nomic-embed-text` |
| Health check | `GET /api/tags` | — |

Use **one chat model for everything** to start, so only one big model has to fit in VRAM.

---

## 3. Model choice

Pick a 4–8B instruct model quantized to about Q4 (Ollama's default tags), so it fits in ~5GB plus the embedding model.

Candidates to benchmark:
- `qwen3:8b`
- `llama3.1:8b`
- `gemma3:4b` (lighter, faster, the fallback if VRAM is tight)

Check what's current on the Ollama library before pulling, since newer small models come out often.

**How to pick:** write a small `bench/` script (it can live in the Pi repo and run from either machine) that sends 20 saved brain prompts and 20 saved narrator prompts to each model and records:
- whether the JSON was valid (should be 100% with `format`)
- seconds per call
- the narrator's `rejected_changes` rate (lower = better at sticking to the real world)
- the diary text, so you can read it and judge character voice and variety by hand

---

## 4. Setup

### NVIDIA driver (Ubuntu)
```
sudo ubuntu-drivers install      # installs the recommended proprietary driver
sudo reboot
nvidia-smi                       # should list the GTX 1060 with 6144MiB
```

### Install Ollama
```
curl -fsSL https://ollama.com/install.sh | sh
```
It installs as the `ollama` systemd service and starts on boot.

### Pull models
```
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

### Server settings (environment variables)
| Variable | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `0.0.0.0:11434` | Accept connections from the Pi, not just this laptop |
| `OLLAMA_KEEP_ALIVE` | `-1` | Keep models loaded forever, so there's no reload delay each tick |
| `OLLAMA_MAX_LOADED_MODELS` | `2` | Chat model and embedding model loaded together |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time. The Pi calls in sequence anyway, and it saves VRAM. |

Set them with a systemd override:
```
sudo systemctl edit ollama
```
```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
```
```
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

### Network
- Give the laptop a **fixed IP**: a DHCP reservation in the router is easiest. Put that IP in the Pi's `settings.yaml`.
- Firewall: allow 11434 **from the local network only** (adjust the subnet to match yours):
  ```
  sudo ufw allow from 192.168.1.0/24 to any port 11434 proto tcp
  sudo ufw allow OpenSSH        # if you use SSH, so enabling ufw doesn't lock you out
  sudo ufw enable
  ```
- **Don't** port-forward 11434 on the router. Ollama has no authentication.
- Optional: `sudo apt install openssh-server` so you can manage it from another machine with the lid closed.

### Power (24/7, Ubuntu)
- **Never sleep:**
  ```
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  ```
- **Lid closed = keep running.** In `/etc/systemd/logind.conf` set:
  ```
  HandleLidSwitch=ignore
  HandleLidSwitchExternalPower=ignore
  HandleLidSwitchDocked=ignore
  ```
  then `sudo systemctl restart systemd-logind`.
- **No surprise reboots from updates:** in `/etc/apt/apt.conf.d/50unattended-upgrades` keep `Unattended-Upgrade::Automatic-Reboot "false";` (or set a reboot time during the creature's night). The Pi's offline handling covers any reboots.
- **Battery:** a laptop sitting at 100% charge 24/7 wears the battery, and old batteries can swell. Check the battery now and then, and if the BIOS has a charge limit or "battery care" option, turn it on. Otherwise consider removing the battery.
- **Heat:** keep it on a hard surface with the vents clear. A stand or cooling pad helps if it runs with the lid closed.

---

## 5. Verify from the Pi

```
curl http://<laptop-ip>:11434/api/tags
curl http://<laptop-ip>:11434/api/chat -d '{"model":"qwen3:8b","messages":[{"role":"user","content":"say hi as a cat"}],"stream":false}'
curl http://<laptop-ip>:11434/api/embed -d '{"model":"nomic-embed-text","input":"a sunny windowsill"}'
```
All three should answer within a few seconds once the models are loaded.

---

## 6. Optional extras (not v1)

- **Watchdog:** a scheduled task that restarts Ollama if `/api/tags` stops answering.
- **GPU stats:** expose `nvidia-smi` output so the website can show "brain temperature."
- **Larger brain later:** if a better GPU or a hosted API shows up, only `settings.yaml` on the Pi changes.
