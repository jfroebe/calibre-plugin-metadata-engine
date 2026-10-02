# Calibre Metadata Engine

A **Calibre metadata-source plugin** that connects Calibre to [metadata-engine](https://github.com/jfroebe/metadata-engine). This repository contains the Calibre plugin; it is not a metadata-engine plugin.

## Author and assistance

**Author:** Jason Froebe

Development assistance was provided by ChatGPT because there simply is not enough time in a 24-hour day to do it all.

## About metadata-engine

**metadata-engine will be released to the public soon.**

metadata-engine is a generic, adapter- and plugin-based system designed to simplify the use of public metadata sources. It provides a common integration layer for applications while handling concerns such as built-in rate limiting and scaling.

## Warranty and responsible use

**There is NO warranty, explicit, implicit, or otherwise. Use this software entirely at your own risk.**

Always obtain permission from metadata sources before using them.

- In many cases, permission is granted by agreeing to the provider's Terms of Service and obtaining an API key.
- In other cases, permission may simply require asking the provider directly.
- If a metadata provider says no, respect that decision and do not use their service.

## Download and install

The ready-to-install artifact is **Calibre-Metadata-Engine.zip** in this repository.

**Direct download:** [Calibre-Metadata-Engine.zip](https://raw.githubusercontent.com/jfroebe/calibre-plugin-metadata-engine/main/Calibre-Metadata-Engine.zip)

Download that ZIP and keep it zipped.

If the direct link does not download correctly in your browser:

1. Open [Calibre-Metadata-Engine.zip in the repository](https://github.com/jfroebe/calibre-plugin-metadata-engine/blob/main/Calibre-Metadata-Engine.zip).
2. Use GitHub's **Download raw file** button.
3. Save the file as `Calibre-Metadata-Engine.zip`.
4. Do not extract it before loading it into Calibre.

Calibre's documented plugin installer accepts a local ZIP path; it does not document installation directly from an arbitrary GitHub URL. The supported GitHub flow is therefore: **download the plugin ZIP from GitHub, then load that ZIP into Calibre**.

### Calibre GUI

1. Download [`Calibre-Metadata-Engine.zip`](https://raw.githubusercontent.com/jfroebe/calibre-plugin-metadata-engine/main/Calibre-Metadata-Engine.zip).
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


## Documentation and code quality

Repository documentation and source code are kept sanitized and validated before generating the installable plugin ZIP.

The build process checks for:

- Python syntax errors in plugin source files
- accidental literal escape sequences in Markdown
- unresolved merge-conflict markers
- common trailing-whitespace issues
- missing required plugin files

The generated `Calibre-Metadata-Engine.zip` always includes the current `README.md`, so the documentation distributed with the plugin matches the repository documentation.
