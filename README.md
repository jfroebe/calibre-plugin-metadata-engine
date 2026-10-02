# Calibre Metadata Engine

A **Calibre metadata-source plugin** that connects Calibre to [metadata-engine](https://github.com/jfroebe/metadata-engine). This repository contains the Calibre plugin; it is not a metadata-engine plugin.

## Download and install

The ready-to-install artifact is **Calibre-Metadata-Engine.zip** in this repository. Download that ZIP from GitHub and keep it zipped.

Calibre's documented plugin installer accepts a local ZIP path; it does not document installation directly from an arbitrary GitHub URL. The supported GitHub flow is therefore: **download the plugin ZIP from GitHub, then load that ZIP into Calibre**.

### Calibre GUI

1. Download `Calibre-Metadata-Engine.zip` from this repository.
2. Open **Calibre → Preferences → Plugins**.
3. Click **Load plugin from file**.
4. Select the downloaded ZIP.
5. Confirm Calibre's third-party plugin warning.
6. Restart Calibre.
7. Open **Preferences → Metadata download**.
8. Enable **Metadata Engine**, select it, and choose **Configure selected source**.
9. Enter your metadata-engine URL, for example `http://192.168.0.59:8790`.
10. Click **Test connection**, choose provider mode, and save.

### Command line

After downloading the ZIP:

```bash
calibre-customize -a Calibre-Metadata-Engine.zip
```

Verify installation:

```bash
calibre-customize -l
```

Look for **Metadata Engine**.

### Upgrade

Download the newest ZIP, then:

```bash
calibre-customize -r "Metadata Engine"
calibre-customize -a Calibre-Metadata-Engine.zip
```

Restart Calibre afterward.

## Configuration GUI

The plugin provides a native Qt configuration dialog with:

- metadata-engine server URL
- optional Bearer token
- HTTPS certificate verification
- live **Test connection**
- provider discovery from `/plugins`
- automatic or selected-provider mode
- maximum results
- per-provider request limit
- optional source tagging

## Test metadata lookup

```bash
fetch-ebook-metadata \
  --allowed-plugin "Metadata Engine" \
  --title "The Hobbit" \
  --authors "J. R. R. Tolkien" \
  --verbose
```

ISBN:

```bash
fetch-ebook-metadata \
  --allowed-plugin "Metadata Engine" \
  --isbn 9780547928227 \
  --verbose
```

## metadata-engine API compatibility

The plugin currently uses metadata-engine 2.6.x routes:

- `GET /health`
- `GET /plugins`
- `GET /plugins/{plugin_id}/search`

It discovers enabled metadata-engine providers, queries them, deduplicates candidates, maps results to Calibre metadata, and downloads covers from returned `cover_url` values.

## Development

The plugin source is `__init__.py` and `config.py` at the repository root.

To build and install a development checkout:

```bash
calibre-customize -b .
```

The GitHub Actions workflow rebuilds `Calibre-Metadata-Engine.zip` whenever the plugin source changes. The installable ZIP contains `__init__.py` and `config.py` at its root.
