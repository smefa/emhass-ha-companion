# Install

## What you need

- Home Assistant 2026.7 or newer.
- EMHASS itself, already installed and running — either the add-on or a
  container. EMHASS Companion talks to it; it doesn't include it.
- **EMHASS version 0.17.9 or newer.**
- [HACS](https://hacs.xyz/), to install the integration.

Nothing else is required. No battery, no solar panels and no dynamic
electricity tariff are all valid setups — you tell the integration what you
actually have during setup.

## Steps

1. **Install and start EMHASS**, if you haven't already — the add-on, or a
   container.
2. **Add this repository to HACS** as a custom repository (category:
   *Integration*), then install **EMHASS Companion** from HACS.
3. **Restart Home Assistant.**
4. Go to **Settings → Devices & Services → Add Integration**, and search for
   **EMHASS Companion**.

If you're running the EMHASS add-on, its address is filled in for you on the
first screen. Continue to **[Setup](setup.md)** for what each question after
that means.
