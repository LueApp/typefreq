# typefreq

typefreq is a Linux desktop typing analytics tool. It records word frequency, shows a dashboard, and can surface typo suggestions as lightweight desktop toasts.

The public Cloudflare Pages site is the main web UI. When the local desktop service is running, the page reads `http://127.0.0.1:8788` from the user's browser by default and displays the user's local analytics. Users can change the local service port on the web page. When the service is not detected, the same page shows setup guidance.

The data still stays on the user's machine. The local service binds to `127.0.0.1` by default and only allows configured public origins to read its API.

## Install

### With web2local

If [web2local](https://web2local-bridge.lue-app.com/) is running, open the public
site and use **Install with web2local**. The page detects web2local, adds the
current site origin to web2local's graylist, then calls web2local's `/deploy`
with a generated installer script. web2local shows the script filename, SHA-256,
destination, and `bash` command before it writes and runs anything.

The web2local installer downloads the source package, extracts app files to:

```text
~/.local/lib/typefreq
```

and runs `install.sh` with the selected local service port and CORS origins.
Because web2local commands do not have terminal stdin, this path does not try to
request a sudo password. If Ubuntu packages or `input` group membership still
need first-time setup, use the configured terminal installer below once.

### Terminal installer

Download the configured installer from the public site, then run it:

```bash
bash typefreq-install-8788.sh
```

The configured installer downloads the package to
`~/.local/lib/typefreq` and passes the selected port and the current
public site origin to `install.sh`, so the local service allows the deployed
Cloudflare page to read it.

You can also download the source package directly, extract it, and run the installer:

```bash
tar -xf typefreq-latest.tar
cd typefreq
./install.sh 8788
```

Or clone the repo:

```bash
git clone https://github.com/LueApp/typefreq.git
cd typefreq
./install.sh
```

The installer creates a Python virtual environment, installs dependencies, writes a systemd user service, enables autostart, and starts the app when your session has permission to read keyboard input.

On Ubuntu, the installer also attempts to install the common apt dependencies and add your user to the `input` group. If that group permission is added during installation, sign out and back in once so the desktop session receives the new permission.

## Linux Requirements

typefreq needs access to `/dev/input/event*`, so your user must be in the `input` group. The installer handles this on Ubuntu when possible. It also installs or checks for:

```bash
sudo apt install python3-venv python3-dev python3-tk xdotool libnotify-bin python3-gi gir1.2-atspi-2.0
```

`xdotool`, `libnotify-bin`, and AT-SPI bindings are helpful for better typo toast positioning and fallback notifications.

## Use

After installation, open:

```text
https://typefreq.lue-app.com
```

From the dashboard you can:

- View today's words, unique words, typos, and events.
- Compare top words by day, week, month, year, or all time.
- Review recent typos.
- Add custom words so names and domain terms are not flagged.
- Pause and resume tracking.

If the public page cannot connect, it will show the install and troubleshooting guide. The local fallback dashboard remains available at:

```text
http://127.0.0.1:8788
```

## Service Commands

```bash
systemctl --user status typefreq.service
systemctl --user restart typefreq.service
journalctl --user -u typefreq -f
```

## Configure

Runtime settings are environment variables read by `typefreq/config.py`.

Common options:

- `TYPEFREQ_HOST` and `TYPEFREQ_PORT` control the local dashboard bind address.
- `TYPEFREQ_LANG` chooses the spell-check language.
- `TYPEFREQ_DATA` and `TYPEFREQ_DB` choose where local analytics data is stored.
- `TYPEFREQ_OVERLAY_POSITION` controls where typo toasts appear.

For migration from the old `keyfreq` name, legacy `KEYFREQ_*` variables are still honored when the matching `TYPEFREQ_*` variable is not set. If `~/.local/share/keyfreq` already exists and the new data directory does not, typefreq keeps using the existing data directory and database.

The default dashboard bind is `127.0.0.1`, so private typing data is not exposed to the network.

To use a non-default port:

```bash
./install.sh 8799
```

Then set the same port in the public web page's "Local port" field and click Apply.

## Public Web UI And CORS

The public page talks to the local service from the user's browser. The service therefore sends CORS and Private Network Access headers for a restricted origin list.

Defaults include:

- `https://typefreq.lue-app.com`
- `https://keyfreq.lue-app.com`
- `http://localhost:4321`
- `http://127.0.0.1:4321`
- `http://localhost:4325`
- `http://127.0.0.1:4325`

Configure with:

- `TYPEFREQ_PUBLIC_SITE=https://your-domain.example`
- `TYPEFREQ_ALLOWED_ORIGINS=https://your-domain.example,http://localhost:4321`

## Deploy The Public Website

This repo also contains an Astro site configured for Cloudflare Workers Assets.

Cloudflare settings:

- Worker name: `typefreq`
- Production domain: `typefreq.lue-app.com`
- Optional migration alias: `keyfreq.lue-app.com`
- Node version: `20` or newer
- Optional environment variable: `SITE_URL=https://typefreq.lue-app.com`

Local commands:

```bash
npm install
npm run dev
npm run build
npm run preview
npm run deploy
```

`npm run build` generates `public/downloads/typefreq-latest.tar` before Astro builds the site, so the public page can serve the tool download directly. `npm run deploy` publishes the built site with Wrangler. The generated public site tries to connect to the user's local typefreq service first; if that connection fails, it renders the setup guide.
