# TradeReader 3D — multi-trade PlanReader

TradeReader is a separate Streamlit entry point built from the proven PlanReader document, drawing-register, AI and 3D foundations without loading the Premier Brushworks painting estimator overlay.

## What is separate

- Entry point: `tradereader_app.py`
- Local/persistent data: set `PLANREADER_DATA_DIR=/var/data/tradereader` (or `TRADEREADER_DATA_DIR` when `PLANREADER_DATA_DIR` is not set)
- Optional login: `TRADEREADER_PASSWORD`
- No JobHub connection or JobHub writes
- No Premier Brushworks default painting rates
- AI rates remain zero unless explicitly supplied in a source or entered by the estimator

## Trade presets

Electrical, Plumbing, HVAC / Mechanical, Carpentry / Joinery, Plastering / Linings, Tiling, Flooring, Roofing, Concreting, Landscaping and Custom trade.

Each preset changes the AI scope focus, preferred take-off sections, expected units and typical scope exclusions. A custom trade can be named directly in the sidebar.

## Take-off method

TradeReader cross-references the selected drawings, schedules, details and specifications; separates inclusions/exclusions/provisional items/assumptions/RFIs; uses the unit appropriate to the work; keeps source references; and leaves unsupported quantities as `To measure` rather than inventing dimensions.

Selected drawing sets are analysed in batches of up to eight pages with a project-wide extracted-text basis.

## Run locally

```bash
python -m streamlit run tradereader_app.py
```

Or use `RUN_TRADEREADER_WINDOWS.bat`.

## Deploy separately on Render

Create a second Render web service. Do not replace the Premier Brushworks service.

- Build command: `pip install -r requirements.txt`
- Start command: `streamlit run tradereader_app.py --server.address 0.0.0.0 --server.port $PORT`
- Persistent disk mount: `/var/data`
- `PLANREADER_DATA_DIR=/var/data/tradereader`
- Configure `OPENAI_API_KEY` and/or `GEMINI_API_KEY`
- Optional `TRADEREADER_PASSWORD`

`render-tradereader.yaml` is provided as a separate-service template. The existing `render.yaml` remains the Premier Brushworks deployment.
