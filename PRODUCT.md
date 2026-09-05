# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are operators and editors running a local Chinese content workflow for WeChat official account publishing. They arrive with news sources, topics, drafts, image candidates, and publishing checks that need review and manual control.

## Product Purpose

The product is a local AI content workbench for collecting technology news, generating original topic packages, producing article drafts, managing images, exporting WeChat/Heybox-ready content, and reviewing publication records. Success means an editor can move from collection to publishable draft with clear state, recoverable actions, and visible evidence.

## Positioning

The product combines source collection, topic clustering, original-writing guardrails, image planning, platform export, and local data persistence in one operator-facing workflow instead of treating them as isolated scripts.

## Operating Context

The app runs locally from `python run_server.py` and serves a Flask web UI at `http://127.0.0.1:5050`. The main workflow lives in the dashboard, with supporting topic and draft pages. Data and generated assets are stored under `data/`, while prompts and sources are configured under `config/`.

## Capabilities and Constraints

Confirmed capabilities include RSS/web/JSON/changelog collection, keyword and source-package selection, topic generation and manual editing, draft generation and editing, cover/image candidate workflows, WeChat/Heybox export support, publication records, SQLite backfill/status, and changelog display. The UI must preserve existing form controls, `data-*` hooks, API endpoints, and local file-backed workflows.

Open decisions: user roles beyond a single local editor, multi-user permissions, deployment target, and formal accessibility standard have not been specified.

## Brand Commitments

The current design authority is `DESIGN.md`, which defines a Steep-inspired warm-paper analytics style: editorial serif headings, restrained monochrome surfaces, black pill actions, soft cards, and rare peach emphasis.

## Evidence on Hand

Available evidence includes `README.md`, `DESIGN.md`, Flask routes in `app/server.py`, templates in `templates/`, CSS in `static/style.css`, and sample local data/assets under `data/`. No real customer claims, testimonials, or public brand assets are present and future work should not fabricate them.

## Product Principles

- Keep the workflow task-first: collection, topic selection, drafting, imagery, and publishing state must be faster to scan than the surrounding decoration.
- Preserve editorial control: generated content remains inspectable, editable, and recoverable before publication.
- Make status visible: counts, readiness, failures, disabled actions, and selected variants should be apparent without reading logs.
- Use restrained product craft: visual warmth should clarify hierarchy, not compete with dense operational content.
