# Amazon Live Listing Scanner

A small Android-friendly PWA for scanning **public Amazon product-page content** for terms such as:

- organic
- bio
- eco
- LU-BIO-04

## What it checks

It extracts public page content including:

- Product title
- Bullet points
- Product description
- Product details / overview
- A+ content when exposed in HTML
- Other visible page text

It does **not** have access to Amazon's private Seller Central backend attributes.

## Run locally

Python 3.11+ recommended.

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Android use

Once deployed to an HTTPS host (Render, Railway, Fly.io, VPS, etc.), open the URL in Chrome on Android and choose:

**Chrome menu → Add to Home screen**

It will then launch like an app.

## Important reliability note

Amazon sometimes serves CAPTCHA / robot-check pages to automated requests. The app detects this and reports `blocked`.

For reliable higher-volume scanning, the next version should optionally route requests through a legitimate web-data/proxy API provider. The frontend and output format can stay the same.

## Deploy command

Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```
